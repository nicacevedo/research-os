"""One bounded research cycle, as a resumable graph.

```text
hydrate_project_state          A0, read-only, no model
        |
plan_one_action                one model call; chooses ONE action
        |
validate_plan                  deterministic; refuses anything not permitted
        |
   +----+--------------------------+
   |                              |
perform_action              prepare_decision_packet    <- A2 was planned
   |                              |
deterministic_check         await_decision             <- interrupt() ONLY
   |                              |
review                      apply_decision             <- acts on the answer
   |                              |
   +-----------+------------------+
               |
          conclude               terminal state, or a recommended successor
```

Three properties are structural rather than conventional, and each was put here
by something that was measured rather than assumed.

**The human gate is three nodes.** LangGraph re-executes an interrupted node
from its beginning when the interrupt is answered. A side effect placed before
``interrupt()`` is therefore emitted twice -- reproduced on the pinned version,
recorded in ``ARCHITECTURE.md`` §12a. So ``await_decision`` contains nothing but
``interrupt()``: the packet is prepared before it, the effect is applied after
it, and replaying the middle node costs nothing.

**Every side effect goes through the ledger.** A process killed inside a node
re-runs that node on resume -- also reproduced -- so ``perform_action`` never
acts directly. It asks :class:`~research_os.runtime.idempotency.InvocationLedger`
to act at most once for a key derived from the action's identity, and a retry
gets the first attempt's result back.

**The cycle cannot extend itself.** ``conclude`` sets a terminal state and a
*recommendation*. Opening a successor cycle is the control plane's decision, in
a new thread with recorded lineage, bounded by
``max_cycles_per_objective``. There is no edge back to the top of this graph.
"""

from __future__ import annotations

import hashlib
import json
import logging
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.runtime import Runtime
from langgraph.types import interrupt

from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.graphs.state import (
    CycleContext,
    CycleState,
    note,
)
from research_os.runtime.idempotency import (
    UnreconciledInvocationError,
    idempotency_key,
)
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.kernel import Frontier, ScientificAuthorityError
from research_os.runtime.models import TerminalState
from research_os.runtime.policy import (
    ActionKind,
    AutonomyLevel,
    PolicyRefusedError,
    ScientificGateError,
    authorize,
    level_for,
    policy_for,
)
from research_os.runtime.prompts import PLANNER, SCIENTIFIC_REVIEWER
from research_os.runtime.registry import ACTION_HANDLERS, ActionOutcome

LOG = logging.getLogger("research_os.runtime.graphs.cycle")


# ---------------------------------------------------------------- A0 nodes --
def hydrate_project_state(
    state: CycleState, runtime: Runtime[CycleContext]
) -> dict[str, Any]:
    """Reconstruct what this project currently knows, from its files.

    No model, no network, no database read of scientific state. The frontier is
    derived from Git-tracked capsule files by ordinary Python, which makes it
    free enough to recompute at the start of every cycle and deterministic
    enough that two cycles disagreeing about it are disagreeing about the files.
    """

    context = runtime.context
    report = context.kernel.validate()
    frontier = context.kernel.frontier()
    return {
        "frontier": {
            "summary": frontier.summary(),
            "open_questions": list(frontier.open_questions),
            "actionable_hypotheses": list(frontier.actionable_hypotheses),
            "hypotheses_without_tests": list(frontier.hypotheses_without_tests),
            "claims_awaiting_review": list(frontier.claims_awaiting_review),
            "claims_with_stale_review": list(frontier.claims_with_stale_review),
            "contested_claims": list(frontier.contested_claims),
            "evidence_gaps": list(frontier.evidence_gaps),
            "pending_experiments": list(frontier.pending_experiments),
            "validation_errors": list(frontier.validation_errors),
            "empty": frontier.empty,
        },
        "capsule_valid": report.ok,
        "notes": note(
            state,
            f"hydrated: {sum(frontier.summary().values())} open items, "
            f"capsule {'valid' if report.ok else 'has errors'}",
        ),
    }


def plan_one_action(
    state: CycleState, runtime: Runtime[CycleContext]
) -> dict[str, Any]:
    """Choose exactly one next action.

    One action, not a sequence. A plan of ten steps is a plan that is wrong from
    step three onwards once the first result arrives, and a cycle that replans
    after each action is both cheaper and more honest about what it knows.

    A budget that is already exhausted short-circuits here rather than after the
    call: asking a model what to do next when there is nothing left to do it
    with is the one model call guaranteed to be wasted.
    """

    context = runtime.context
    frontier = state.get("frontier", {})

    exhausted = context.budgets.exhausted_dimensions(
        run_id=state["run_id"], project_id=state["project_id"]
    )
    if exhausted:
        names = ", ".join(
            f"{scope}:{dimension}" for scope, _sid, dimension in exhausted
        )
        return {
            "plan_refusal": f"budget exhausted ({names})",
            "notes": note(state, f"no plan: budget exhausted ({names})"),
        }

    if frontier.get("empty"):
        return {
            "plan": {
                "action": str(ActionKind.ASSESS_FRONTIER),
                "rationale": "The frontier is empty; nothing is outstanding.",
                "addresses": [],
                "expected_information_gain": "low",
            },
            "notes": note(state, "frontier empty; planning an assessment only"),
        }

    prompt = PLANNER.render(
        fields={
            "objective": state["objective"],
            "permitted_actions": ", ".join(context.permitted_actions),
        },
        blocks={
            "frontier": [json.dumps(frontier, indent=2, sort_keys=True)],
            "repository": [state["repo_path"]],
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=PLANNER.role,
                capability=PLANNER.capability,
                prompt=prompt,
                prompt_version=PLANNER.identity,
                criticality=PLANNER.criticality,
                independence=PLANNER.independence,
                independence_group=f"plan:{state['run_id']}",
                json_schema=PLANNER.output_schema,
            )
        )
    except BudgetExhaustedError as exc:
        return {
            "plan_refusal": f"budget exhausted: {exc}",
            "notes": note(state, f"no plan: {exc}"),
        }

    if not response.ok or response.structured is None:
        return {
            "plan_refusal": f"the planner returned no usable plan: {response.error}",
            "notes": note(state, f"planner failed: {response.error}"),
        }
    plan = dict(response.structured)
    return {
        "plan": plan,
        "notes": note(state, f"planned: {plan.get('action')}"),
    }


def validate_plan(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """Refuse anything not in the closed action set or not permitted here.

    Deterministic, and deliberately unhelpful: a planner that invents a verb
    gets a refusal, not a best-effort interpretation. An unknown action is the
    one case where guessing what was meant could produce an unreviewed side
    effect.
    """

    context = runtime.context
    if state.get("plan_refusal"):
        return {}
    plan = state.get("plan") or {}
    raw = str(plan.get("action", "")).strip()
    try:
        action = ActionKind(raw)
    except ValueError:
        return {
            "plan_refusal": (
                f"{raw!r} is not an action this build knows. Permitted here: "
                f"{', '.join(context.permitted_actions)}"
            ),
            "notes": note(state, f"plan refused: unknown action {raw!r}"),
        }
    if raw not in context.permitted_actions:
        return {
            "plan_refusal": (
                f"{action} is not permitted for this cycle at autonomy "
                f"{state['autonomy']}"
            ),
            "notes": note(state, f"plan refused: {action} not permitted"),
        }
    policy = policy_for(action)
    if action not in ACTION_HANDLERS and not policy.human_executes:
        return {
            "plan_refusal": f"{action} has no handler in this build",
            "notes": note(state, f"plan refused: {action} unimplemented"),
        }
    try:
        authorize(action, autonomy=state["autonomy"])
    except ScientificGateError:
        # Not a refusal. This is the path to the human gate.
        return {"notes": note(state, f"{action} needs a scientific decision")}
    except PolicyRefusedError as exc:
        return {
            "plan_refusal": str(exc),
            "notes": note(state, f"plan refused: {exc}"),
        }
    return {"notes": note(state, f"plan validated: {action}")}


# ---------------------------------------------------------------- A1 nodes --
def perform_action(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """Perform the one planned action, at most once ever.

    The ledger key is derived from the run, the cycle and the action -- nothing
    per-attempt -- so a crash inside this node, which LangGraph resolves by
    re-running the node, finds the completed invocation and reuses its result
    instead of acting again.
    """

    context = runtime.context
    plan = state.get("plan") or {}
    action = ActionKind(str(plan["action"]))
    registered = ACTION_HANDLERS[action]

    key = idempotency_key(
        f"cycle.{action}", state["run_id"], state["cycle_index"], plan.get("action")
    )

    def perform() -> dict[str, Any]:
        outcome: ActionOutcome = registered.handler(state, context, plan)
        return {
            "ok": outcome.ok,
            "detail": outcome.detail,
            "data": outcome.data,
            "artifacts": [
                {
                    "artifact_id": ref.artifact_id,
                    "media_type": ref.media_type,
                    "role": ref.role,
                    "size_bytes": ref.size_bytes,
                }
                for ref in outcome.artifacts
            ],
            "failure_class": str(outcome.failure_class)
            if outcome.failure_class
            else None,
        }

    def reconcile(_invocation: Any) -> dict[str, Any] | None:
        """Resolve an attempt whose outcome the crash left unknown.

        A replay-safe action simply runs again -- that is what replay-safe
        means. Anything else asks its own reconciler to look at the world. An
        action with neither gets ``None`` from nowhere: the ledger raises
        :class:`UnreconciledInvocationError` rather than guessing, and the
        branch below turns that into a terminal state a person can act on.
        """

        if registered.replay_safe:
            return None
        return (
            registered.reconcile(state, context, plan)
            if registered.reconcile is not None
            else None
        )

    try:
        outcome = context.ledger.run(
            key=key,
            kind=f"cycle.{action}",
            run_id=state["run_id"],
            perform=perform,
            request={"action": str(action), "cycle_index": state["cycle_index"]},
            reconcile=reconcile
            if (registered.replay_safe or registered.reconcile)
            else None,
        )
    except UnreconciledInvocationError as exc:
        return {
            "terminal_state": str(TerminalState.FATAL_INFRASTRUCTURE_ERROR),
            "notes": note(
                state,
                f"{action} was interrupted and its outcome cannot be established: {exc}",
            ),
        }

    result = outcome.result
    artifacts = list(state.get("artifacts", []))
    artifacts.extend(result.get("artifacts", []))
    return {
        "action_result": result,
        "action_reused": outcome.reused,
        "artifacts": artifacts,
        "notes": note(
            state,
            f"{action}: {'reused earlier result' if outcome.reused else result.get('detail', 'done')}",
        ),
    }


def deterministic_check(
    state: CycleState, runtime: Runtime[CycleContext]
) -> dict[str, Any]:
    """Check what can be checked without a model.

    Capsule validation, artifact integrity, and whether the action reported
    success. A model is not asked whether a file parses.
    """

    context = runtime.context
    result = state.get("action_result") or {}
    report = context.kernel.validate()
    missing = [
        ref["artifact_id"]
        for ref in state.get("artifacts", [])
        if not context.artifacts.exists(ref["artifact_id"])
    ]
    passed = bool(result.get("ok")) and report.ok and not missing
    return {
        "check_result": {
            "passed": passed,
            "capsule_ok": report.ok,
            "capsule_errors": sorted({finding.code for finding in report.errors}),
            "missing_artifacts": missing,
            "action_ok": bool(result.get("ok")),
        },
        "notes": note(state, f"checks {'passed' if passed else 'failed'}"),
    }


def review(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """Obtain an independent structured review of what this cycle produced.

    Skipped for actions that produced nothing to review -- an assessment, an
    index rebuild -- because a review of nothing costs a critical-tier model
    call and returns no information.

    The reviewer cannot approve anything. Its verdict is advice for a human,
    recorded as provenance; acceptance remains a human act recorded through the
    kernel's own review command.
    """

    context = runtime.context
    plan = state.get("plan") or {}
    result = state.get("action_result") or {}
    action = str(plan.get("action", ""))

    if not result.get("data"):
        return {"notes": note(state, "nothing to review")}

    prompt = SCIENTIFIC_REVIEWER.render(
        fields={"claim_or_hypothesis": f"the outcome of {action}"},
        blocks={
            "specification": [json.dumps(plan, indent=2, sort_keys=True)],
            "results": [json.dumps(result.get("data"), indent=2, sort_keys=True)],
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=SCIENTIFIC_REVIEWER.role,
                capability=SCIENTIFIC_REVIEWER.capability,
                prompt=prompt,
                prompt_version=SCIENTIFIC_REVIEWER.identity,
                criticality=SCIENTIFIC_REVIEWER.criticality,
                independence=SCIENTIFIC_REVIEWER.independence,
                independence_group=f"review:{state['run_id']}:{state['cycle_index']}",
                json_schema=SCIENTIFIC_REVIEWER.output_schema,
            )
        )
    except BudgetExhaustedError as exc:
        return {"notes": note(state, f"review skipped: {exc}")}

    if not response.ok or response.structured is None:
        return {
            "review": {"verdict": "cannot_assess", "error": response.error},
            "notes": note(state, f"review unavailable: {response.error}"),
        }
    verdict = dict(response.structured)
    verdict["independence"] = str(response.independence)
    verdict["independence_note"] = response.independence_note
    return {
        "review": verdict,
        "notes": note(
            state, f"review: {verdict.get('verdict')} ({response.independence})"
        ),
    }


# ------------------------------------------------------- the human gate ----
def _approve_consequence(action: ActionKind) -> str:
    """What actually happens on approval, said plainly.

    For every `A2` action in this build the answer is "you do it next", and
    saying so in the packet is the difference between a gate a researcher trusts
    and one they rubber-stamp.
    """

    policy = policy_for(action)
    if policy.human_executes:
        return f"You then perform it yourself: {policy.follow_up}"
    return f"The runtime then performs {action}."


def prepare_decision_packet(
    state: CycleState, runtime: Runtime[CycleContext]
) -> dict[str, Any]:
    """Assemble everything a person needs, before anyone is interrupted.

    A gate that asks "approve?" wastes the researcher's attention. This node
    does the work: what is being decided, why it matters now, what the evidence
    currently says, what the alternatives are, what happens after each choice,
    and a recommendation where one is defensible.

    Side effects here are safe -- this node is not the one that interrupts, so
    it is not replayed by an answered interrupt. The approval row is created
    with a unique ``interrupt_key`` so that a *crash* here, which does re-run
    the node, finds the existing row rather than opening a second question.
    """

    context = runtime.context
    plan = state.get("plan") or {}
    action = str(plan.get("action", ""))
    frontier = state.get("frontier", {})

    packet = {
        "decision_required": action,
        "why_it_matters": plan.get("rationale", ""),
        "objective": state["objective"],
        "current_evidence": {
            "claims_awaiting_review": frontier.get("claims_awaiting_review", []),
            "contested_claims": frontier.get("contested_claims", []),
            "evidence_gaps": frontier.get("evidence_gaps", []),
        },
        "alternatives": [
            {
                "choice": "approve",
                "consequence": (
                    f"The decision is recorded. {_approve_consequence(action)}"
                ),
            },
            {
                "choice": "decline",
                "consequence": (
                    "The cycle ends without this action. The frontier keeps the item, "
                    "so a later cycle may propose it again."
                ),
            },
        ],
        "uncertainty": plan.get("expected_information_gain", "unknown"),
        "recommendation": None,
        "recommendation_rationale": (
            "The runtime does not recommend on scientific-authority decisions. "
            "It prepares the evidence and stops."
        ),
    }
    approval, created = context.store.request_approval(
        run_id=state["run_id"],
        project_id=state["project_id"],
        kind=action,
        question=f"Authorise {action}?",
        packet=packet,
        thread_id=f"cycle:{state['run_id']}",
        interrupt_key=f"{state['run_id']}:{state['cycle_index']}:{action}",
    )
    return {
        "decision_packet": packet,
        "approval_id": approval.approval_id,
        "notes": note(
            state,
            f"decision packet {'prepared' if created else 'already prepared'} for {action}",
        ),
    }


def await_decision(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """Interrupt, and do nothing else.

    This node's emptiness is the design. LangGraph re-executes an interrupted
    node from its beginning when the interrupt is answered, so anything else
    placed here would happen twice -- reproduced on the pinned version and
    recorded in ``ARCHITECTURE.md`` §12a.

    On resume the recorded decision is read back from PostgreSQL rather than
    trusted from the resume payload, so the authoritative answer is the one a
    person actually recorded.
    """

    context = runtime.context
    approval_id = state.get("approval_id", "")

    answer = interrupt(
        {
            "kind": "scientific_decision",
            "approval_id": approval_id,
            "question": state.get("decision_packet", {}).get("decision_required", ""),
            "packet": state.get("decision_packet", {}),
        }
    )

    # The resume payload is a *wake-up*, not a verdict. Whoever resumed the
    # thread -- the control plane, the CLI, a test -- does not get to say what
    # was decided; the approvals table does, because that is where a person's
    # decision is recorded and the only place it can be audited from.
    #
    # The first version fell back to `granted=bool(answer)` when no approval row
    # could be read, which made any truthy resume value an approval. Now the
    # absence of a recorded decision is recorded as not granted, and
    # `apply_decision` refuses on it.
    recorded = context.store.get_approval(approval_id) if approval_id else None
    if recorded is None:
        return {
            "decision": {
                "granted": False,
                "status": "UNRECORDED",
                "detail": (
                    f"no recorded decision for {approval_id or '(no approval id)'}; "
                    f"the runtime does not infer one from a resume payload"
                ),
                "resume_payload": str(answer),
            }
        }
    decision = dict(recorded.decision or {})
    decision["status"] = str(recorded.status)
    decision["granted"] = str(recorded.status) in {"GRANTED", "APPLIED"}
    decision["decided_by"] = recorded.decided_by
    return {"decision": decision}


def apply_decision(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """Act on the recorded decision, exactly once.

    ``mark_approval_applied`` returns ``True`` to one caller only, so a replayed
    resume cannot apply one approval twice. A declined decision is a legitimate
    outcome, not a failure: the cycle ends and the frontier keeps the item.
    """

    context = runtime.context
    decision = state.get("decision") or {}
    approval_id = state.get("approval_id", "")
    granted = bool(decision.get("granted"))

    if not approval_id:
        return {
            "terminal_state": str(TerminalState.WAITING_FOR_SCIENTIFIC_DECISION),
            "notes": note(state, "no approval to apply"),
        }

    # `mark_approval_applied` only succeeds for a row that has actually been
    # decided, so a PENDING approval fails here -- which is the protection. But
    # "already applied" and "never decided" are different facts and the first
    # version reported both with the same sentence, which would mislead anyone
    # auditing why a gated action did not happen.
    current = context.store.get_approval(approval_id)
    if current is None or str(current.status) == "PENDING":
        return {
            "terminal_state": str(TerminalState.WAITING_FOR_SCIENTIFIC_DECISION),
            "notes": note(
                state,
                f"{approval_id} has no recorded decision; the cycle stays waiting "
                f"rather than proceeding",
            ),
        }
    claimed = context.store.mark_approval_applied(approval_id)
    if not claimed:
        return {
            "decision_applied": True,
            "notes": note(
                state, f"{approval_id} was already applied; not applying it again"
            ),
        }
    if not granted:
        return {
            "decision_applied": True,
            "terminal_state": str(TerminalState.DONE_FOR_NOW),
            "notes": note(
                state, "decision declined; ending the cycle without the action"
            ),
        }

    plan = state.get("plan") or {}
    action = ActionKind(str(plan["action"]))
    registered = ACTION_HANDLERS.get(action)
    if registered is None:
        return {
            "decision_applied": True,
            "terminal_state": str(TerminalState.WAITING_FOR_SCIENTIFIC_DECISION),
            "notes": note(
                state, f"{action} was approved but has no handler in this build"
            ),
        }
    key = idempotency_key(
        f"approved.{action}", state["run_id"], state["cycle_index"], approval_id
    )

    def perform() -> dict[str, Any]:
        outcome: ActionOutcome = registered.handler(state, context, plan)
        return {"ok": outcome.ok, "detail": outcome.detail, "data": outcome.data}

    try:
        applied = context.ledger.run(
            key=key,
            kind=f"approved.{action}",
            run_id=state["run_id"],
            perform=perform,
            reconcile=lambda _invocation: None,
        )
    except ScientificAuthorityError as exc:
        return {
            "decision_applied": True,
            "terminal_state": str(TerminalState.WAITING_FOR_SCIENTIFIC_DECISION),
            "notes": note(state, f"refused even with approval: {exc}"),
        }
    return {
        "decision_applied": True,
        "action_result": applied.result,
        "notes": note(state, f"applied the approved {action}"),
    }


# ------------------------------------------------------------- conclusion --
def conclude(state: CycleState, runtime: Runtime[CycleContext]) -> dict[str, Any]:
    """End this cycle in a named state, and recommend what should follow.

    Recommend, not decide. Opening a successor is the control plane's call, in a
    new thread with recorded lineage and a hard cap on chain length. There is no
    edge from here back to the top.
    """

    context = runtime.context
    if state.get("terminal_state"):
        return {"notes": note(state, f"concluded: {state['terminal_state']}")}

    refusal = state.get("plan_refusal", "")
    if "budget" in refusal.lower():
        return {
            "terminal_state": str(TerminalState.BUDGET_EXHAUSTED),
            "next_recommendation": "BUDGET_EXHAUSTED",
            "notes": note(state, "concluded: budget exhausted"),
        }
    if refusal:
        return {
            "terminal_state": str(TerminalState.DONE_FOR_NOW),
            "next_recommendation": "BLOCKED",
            "notes": note(state, f"concluded: nothing permitted to do ({refusal})"),
        }

    frontier = context.kernel.frontier()
    digest = frontier_digest(frontier)
    check = state.get("check_result") or {}
    if not check.get("passed", True):
        return {
            "frontier_digest": digest,
            "terminal_state": str(TerminalState.DONE_FOR_NOW),
            "next_recommendation": "START_NEXT_CYCLE",
            "notes": note(
                state, "concluded: checks failed; a repair cycle is warranted"
            ),
        }
    if frontier.empty:
        return {
            "frontier_digest": digest,
            "terminal_state": str(TerminalState.DONE_FOR_NOW),
            "next_recommendation": "DONE_FOR_NOW",
            "notes": note(state, "concluded: the frontier is empty"),
        }
    return {
        "frontier_digest": digest,
        "terminal_state": str(TerminalState.DONE_FOR_NOW),
        "next_recommendation": "START_NEXT_CYCLE",
        "notes": note(state, "concluded: frontier still has work"),
    }


# -------------------------------------------------------------- assembly ----
def frontier_digest(frontier: Frontier) -> str:
    """A stable hash of what is outstanding.

    Compared between a cycle and its parent so continuation can tell "we
    learned something" from "we ran again". The first real pilot ran seven
    chained cycles over an identical frontier before its ceiling stopped it --
    fifteen model calls for one assessment's worth of information.

    Derived from the *ids*, sorted, not from counts: two frontiers with the same
    shape and different members are different frontiers.
    """

    payload = json.dumps(
        {
            "open_questions": sorted(frontier.open_questions),
            "actionable_hypotheses": sorted(frontier.actionable_hypotheses),
            "hypotheses_without_tests": sorted(frontier.hypotheses_without_tests),
            "claims_awaiting_review": sorted(frontier.claims_awaiting_review),
            "claims_with_stale_review": sorted(frontier.claims_with_stale_review),
            "contested_claims": sorted(frontier.contested_claims),
            "evidence_gaps": sorted(frontier.evidence_gaps),
            "pending_experiments": sorted(frontier.pending_experiments),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _after_validation(state: CycleState) -> str:
    """Route to the human gate, straight to the end, or on to the action."""

    if state.get("plan_refusal"):
        return "conclude"
    plan = state.get("plan") or {}
    try:
        action = ActionKind(str(plan.get("action", "")))
    except ValueError:  # pragma: no cover - validate_plan already refused it
        return "conclude"
    if level_for(action) is AutonomyLevel.A2:
        return "prepare_decision_packet"
    return "perform_action"


def build_cycle_graph() -> StateGraph:
    """Assemble the bounded cycle. Compiled by the caller with a checkpointer."""

    graph = StateGraph(CycleState, context_schema=CycleContext)
    graph.add_node("hydrate_project_state", hydrate_project_state)
    graph.add_node("plan_one_action", plan_one_action)
    graph.add_node("validate_plan", validate_plan)
    graph.add_node("perform_action", perform_action)
    graph.add_node("deterministic_check", deterministic_check)
    graph.add_node("review", review)
    graph.add_node("prepare_decision_packet", prepare_decision_packet)
    graph.add_node("await_decision", await_decision)
    graph.add_node("apply_decision", apply_decision)
    graph.add_node("conclude", conclude)

    graph.add_edge(START, "hydrate_project_state")
    graph.add_edge("hydrate_project_state", "plan_one_action")
    graph.add_edge("plan_one_action", "validate_plan")
    graph.add_conditional_edges(
        "validate_plan",
        _after_validation,
        {
            "conclude": "conclude",
            "prepare_decision_packet": "prepare_decision_packet",
            "perform_action": "perform_action",
        },
    )
    graph.add_edge("perform_action", "deterministic_check")
    graph.add_edge("deterministic_check", "review")
    graph.add_edge("review", "conclude")
    # The three-node gate. Nothing between prepare and interrupt, nothing
    # between interrupt and apply.
    graph.add_edge("prepare_decision_packet", "await_decision")
    graph.add_edge("await_decision", "apply_decision")
    graph.add_edge("apply_decision", "conclude")
    graph.add_edge("conclude", END)
    return graph
