"""The bounded cycle: persistence, crash recovery, interrupt, and no duplicates.

Everything here that claims to test a crash crashes a real child process with
``os._exit``. A simulated exception would run the ``finally`` blocks whose
absence is the entire failure mode, and would therefore pass while the system
was broken.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.checkpoints import ensure_tables, prune, state_size_bytes
from research_os.runtime.cycles import (
    permitted_actions,
    resume_cycle,
    should_continue,
    start_cycle,
)
from research_os.runtime.db import Database
from research_os.runtime.graphs import build_cycle_graph
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.models import (
    ApprovalStatus,
    InvocationStatus,
    RunStatus,
    TerminalState,
)
from research_os.runtime.policy import ActionKind
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

SCRIPT = Path(__file__).parent / "runtime_scripts" / "run_cycle.py"
REPO_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def graph_env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    artifacts = tmp_path / "artifacts"
    RuntimeStore(runtime_db).upsert_project(
        project_id="alpha-project", repo_path=str(repo)
    )
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts": artifacts,
        "config": make_config(pg_dsn, artifacts),
    }


def _run_child(
    env: dict[str, Any],
    mode: str,
    *,
    effects: Path,
    run_id: str = "",
    crash_in: str = "",
    action: str = str(ActionKind.INSPECT_REPOSITORY),
    replay_mode: str = "reconcilable",
    expect_returncode: int = 0,
) -> dict[str, Any]:
    child_env = dict(os.environ)
    child_env["EFFECTS_LOG"] = str(effects)
    child_env["CYCLE_ACTION"] = action
    child_env["REPLAY_MODE"] = replay_mode
    if crash_in:
        child_env["CRASH_IN_NODE"] = crash_in
    else:
        child_env.pop("CRASH_IN_NODE", None)
    child_env["PYTHONPATH"] = str(REPO_ROOT)
    completed = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            mode,
            env["dsn"],
            str(env["repo"]),
            str(env["artifacts"]),
            run_id,
        ],
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
        env=child_env,
        cwd=REPO_ROOT,
    )
    assert completed.returncode == expect_returncode, (
        f"stdout={completed.stdout}\nstderr={completed.stderr}"
    )
    if expect_returncode != 0:
        return {}
    return json.loads(completed.stdout.strip().splitlines()[-1])


# ------------------------------------------------------------ plain running --
def test_a_cycle_runs_to_a_named_terminal_state(graph_env: dict[str, Any]) -> None:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="find out whether X",
        models=router,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.terminal_state is TerminalState.DONE_FOR_NOW
    assert result.pending_approval_id is None
    assert result.recommendation in {"START_NEXT_CYCLE", "DONE_FOR_NOW"}
    assert router.requests_for("planner"), "the planner was never consulted"


def test_an_empty_frontier_needs_no_planner_call(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """A model call guaranteed to be wasted is one this build does not make."""

    quiet = make_capsule(tmp_path / "quiet", project_id="quiet-project", empty=True)
    RuntimeStore(graph_env["db"]).upsert_project(
        project_id="quiet-project", repo_path=str(quiet)
    )
    router = ScriptedRouter(answers={"scientific_reviewer": review_answer()})
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="quiet-project",
        repo_path=quiet,
        objective="nothing outstanding",
        models=router,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.recommendation == "DONE_FOR_NOW"
    assert router.requests_for("planner") == []


def test_an_unknown_action_is_refused_rather_than_interpreted(
    graph_env: dict[str, Any],
) -> None:
    """Guessing what a planner meant is how an unreviewed side effect happens."""

    router = ScriptedRouter(answers={"planner": plan_answer("rewrite_the_universe")})
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert result.terminal_state is TerminalState.DONE_FOR_NOW
    assert any("not an action this build knows" in n for n in result.notes)


def test_a_planner_that_returns_nothing_usable_ends_the_cycle_cleanly(
    graph_env: dict[str, Any],
) -> None:
    router = ScriptedRouter(fail_roles={"planner"})
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert any("planner failed" in n for n in result.notes)


# --------------------------------------------------------- state discipline --
def test_the_checkpointed_state_carries_references_not_bytes(
    graph_env: dict[str, Any],
) -> None:
    """A checkpoint holding a PDF grows by megabytes per literature fetch."""

    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.VALIDATE_CAPSULE)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    size = state_size_bytes(result.state)
    assert size < 64 * 1024, f"one cycle's state serialised to {size} bytes"
    for ref in result.state.get("artifacts", []):
        assert set(ref) <= {"artifact_id", "media_type", "role", "size_bytes"}
        assert len(ref["artifact_id"]) == 64


def test_runtime_context_is_not_checkpointed(graph_env: dict[str, Any]) -> None:
    """Live service handles must reach a node without being serialised.

    Asserted by the cycle running at all: :class:`CycleContext` holds a
    connection pool and a ``threading.Lock`` inside it, neither of which any
    serialiser would accept.
    """

    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    assert result.status is RunStatus.SUCCEEDED
    assert "db" not in result.state
    assert "models" not in result.state


# ------------------------------------------------------------ crash / resume --
def test_a_process_killed_inside_a_node_resumes_without_repeating_its_effect(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """The measured LangGraph behaviour, and the ledger that makes it safe.

    A process killed inside ``perform_action`` re-runs that node on resume --
    reproduced on the pinned version and recorded in ARCHITECTURE.md §12a. The
    handler here appends a line per invocation, so a duplicated side effect is
    a second line. There must be exactly one.
    """

    effects = tmp_path / "effects.log"
    _run_child(
        graph_env,
        "start",
        effects=effects,
        crash_in="perform_action",
        expect_returncode=9,
    )
    assert effects.read_text().splitlines() == ["action"], (
        "the side effect did not happen"
    )

    store = RuntimeStore(graph_env["db"])
    runs = store.list_runs(project_id="alpha-project")
    assert len(runs) == 1
    run = runs[0]
    assert run.status is RunStatus.RUNNING, "a crashed cycle must not look finished"

    ledger = InvocationLedger(graph_env["db"])
    stranded = ledger.list_for_run(run.run_id)
    assert len(stranded) == 1
    assert stranded[0].status is InvocationStatus.IN_FLIGHT

    # The daemon's recovery: age out the abandoned invocation, then resume.
    assert len(ledger.abandon_stale(older_than_seconds=0)) == 1

    finished = _run_child(graph_env, "resume", effects=effects, run_id=run.run_id)
    assert finished["status"] == "SUCCEEDED"
    assert effects.read_text().splitlines() == ["action"], (
        "the side effect was emitted a second time on resume"
    )
    assert any("reused earlier result" in note for note in finished["notes"])


def test_a_finished_cycle_cannot_be_resumed(graph_env: dict[str, Any]) -> None:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    from research_os.runtime.cycles import CycleError

    with pytest.raises(CycleError, match="nothing to resume"):
        resume_cycle(
            config=graph_env["config"],
            db=graph_env["db"],
            run_id=result.run.run_id,
            repo_path=graph_env["repo"],
            models=router,
        )


# ------------------------------------------------------------- human gate ----
def test_an_a2_action_stops_at_a_gate_with_a_prepared_packet(
    graph_env: dict[str, Any],
) -> None:
    """A gate that asks "approve?" wastes the researcher's attention."""

    router = ScriptedRouter(
        answers={"planner": plan_answer(str(ActionKind.ACCEPT_CLAIM))}
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    assert result.status is RunStatus.WAITING_HUMAN
    assert result.terminal_state is TerminalState.WAITING_FOR_SCIENTIFIC_DECISION
    assert result.pending_approval_id

    store = RuntimeStore(graph_env["db"])
    approval = store.get_approval(result.pending_approval_id)
    assert approval is not None
    assert approval.status is ApprovalStatus.PENDING
    packet = approval.packet
    for key in (
        "decision_required",
        "why_it_matters",
        "current_evidence",
        "alternatives",
        "uncertainty",
    ):
        assert key in packet, f"the decision packet is missing {key}"
    assert len(packet["alternatives"]) >= 2
    assert all("consequence" in option for option in packet["alternatives"])


def test_an_a2_action_is_never_performed_by_the_runtime_at_all(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """Not merely "not before the gate". Not after it either.

    All eight scientific-authority actions are ones the person performs, so
    there is no state of the world in which the runtime executes one. The child
    process registers a handler for it anyway, and the handler must never run.
    """

    effects = tmp_path / "effects.log"
    effects.touch()
    first = _run_child(
        graph_env, "start", effects=effects, action=str(ActionKind.ACCEPT_CLAIM)
    )
    store = RuntimeStore(graph_env["db"])
    store.record_decision(
        first["pending_approval_id"],
        granted=True,
        decision={"granted": True},
        decided_by="researcher@host",
    )
    _run_child(
        graph_env,
        "answer",
        effects=effects,
        run_id=first["run_id"],
        action=str(ActionKind.ACCEPT_CLAIM),
    )
    assert effects.read_text() == ""


def test_an_a2_action_is_not_performed_before_the_gate(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """The action must not have happened by the time the person is asked."""

    effects = tmp_path / "effects.log"
    effects.touch()
    outcome = _run_child(
        graph_env,
        "start",
        effects=effects,
        action=str(ActionKind.ACCEPT_CLAIM),
    )
    assert outcome["status"] == "WAITING_HUMAN"
    assert effects.read_text() == "", "the gated action ran before anyone authorised it"


def test_answering_the_gate_resumes_in_a_fresh_process_and_acts_once(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """Interrupt, decide, resume -- across process boundaries, effect once."""

    effects = tmp_path / "effects.log"
    effects.touch()
    first = _run_child(
        graph_env, "start", effects=effects, action=str(ActionKind.ACCEPT_CLAIM)
    )
    assert first["status"] == "WAITING_HUMAN"
    approval_id = first["pending_approval_id"]

    store = RuntimeStore(graph_env["db"])
    store.record_decision(
        approval_id,
        granted=True,
        decision={"granted": True, "choice": "approve"},
        decided_by="researcher",
    )

    second = _run_child(
        graph_env,
        "answer",
        effects=effects,
        run_id=first["run_id"],
        action=str(ActionKind.ACCEPT_CLAIM),
    )
    assert second["status"] == "SUCCEEDED"

    # The decision was recorded and the action was NOT performed -- even though
    # the child process deliberately registered a handler for it. `accept_claim`
    # is human-executed: approval unlocks a recorded decision and the command to
    # run, never an execution. An earlier version reached the handler lookup
    # instead and would have run whatever was registered.
    assert effects.read_text() == "", (
        "a human-executed action ran inside the runtime after approval"
    )
    assert any("You perform it" in note for note in second["notes"])
    assert any("researchctl review" in note for note in second["notes"])

    applied = store.get_approval(approval_id)
    assert applied is not None
    assert applied.status is ApprovalStatus.GRANTED
    assert applied.applied_at is not None


def test_a_declined_decision_ends_the_cycle_without_the_action(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """Declining is a legitimate outcome, not a failure."""

    effects = tmp_path / "effects.log"
    effects.touch()
    first = _run_child(
        graph_env, "start", effects=effects, action=str(ActionKind.ACCEPT_CLAIM)
    )
    store = RuntimeStore(graph_env["db"])
    store.record_decision(
        first["pending_approval_id"],
        granted=False,
        decision={"granted": False, "choice": "decline"},
        decided_by="researcher",
    )
    second = _run_child(
        graph_env,
        "answer",
        effects=effects,
        run_id=first["run_id"],
        action=str(ActionKind.ACCEPT_CLAIM),
    )
    assert second["status"] == "SUCCEEDED"
    assert second["terminal_state"] == str(TerminalState.DONE_FOR_NOW)
    assert effects.read_text() == "", "a declined action was performed anyway"


def test_asking_twice_reuses_one_approval_row(graph_env: dict[str, Any]) -> None:
    """A crash in the packet node re-runs it; it must not open a second question."""

    store = RuntimeStore(graph_env["db"])
    run = store.create_run(project_id="alpha-project", objective="o")
    first, created_first = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key=f"{run.run_id}:0:accept_claim",
    )
    second, created_second = store.request_approval(
        run_id=run.run_id,
        project_id="alpha-project",
        kind="accept_claim",
        question="Authorise accept_claim?",
        packet={"decision_required": "accept_claim"},
        interrupt_key=f"{run.run_id}:0:accept_claim",
    )
    assert created_first and not created_second
    assert first.approval_id == second.approval_id
    assert len(store.list_approvals(run_id=run.run_id)) == 1


# ------------------------------------------------------------- boundaries ----
def test_the_interrupt_node_performs_no_side_effect(graph_env: dict[str, Any]) -> None:
    """Asserted structurally, because a replay makes any effect here happen twice.

    ``await_decision`` may read, and it may call ``interrupt``. It may not write
    anything, because LangGraph re-executes it from the beginning when the
    interrupt is answered.
    """

    import ast
    import inspect

    from research_os.runtime.graphs import cycle

    source = inspect.getsource(cycle.await_decision)
    tree = ast.parse(source.lstrip())
    forbidden = {
        "put_text",
        "put_bytes",
        "put_file",
        "record_event",
        "request_approval",
        "record_decision",
        "mark_approval_applied",
        "set_run_status",
        "enqueue",
        "reserve",
        "reserve_all",
        "settle",
        "run",
        "complete",
        "submit",
    }
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    offenders = sorted(called & forbidden)
    assert offenders == [], (
        f"await_decision calls {offenders}; a side effect before interrupt() is "
        f"replayed when the interrupt is answered"
    )


def test_the_graph_has_no_edge_back_to_its_own_start(graph_env: dict[str, Any]) -> None:
    """A cycle recommends a successor; it never becomes one."""

    graph = build_cycle_graph()
    compiled = graph.compile()
    edges = compiled.get_graph().edges
    from_conclude = {edge.target for edge in edges if edge.source == "conclude"}
    assert from_conclude == {"__end__"}, f"conclude leads to {from_conclude}"


def test_permitted_actions_narrow_with_autonomy() -> None:
    high = set(permitted_actions("high"))
    medium = set(permitted_actions("medium"))
    low = set(permitted_actions("low"))
    assert low <= medium <= high
    assert str(ActionKind.ASSESS_FRONTIER) in low
    # An A2 action is plannable at every level -- that is how the gate is reached --
    # but performing it needs a recorded decision.
    assert str(ActionKind.ACCEPT_CLAIM) in low


# ------------------------------------------------------------ continuation ----
def test_continuation_stops_at_the_configured_ceiling(
    graph_env: dict[str, Any],
) -> None:
    """Three independent bounds; this is the lineage one."""

    config = make_config(
        graph_env["dsn"],
        graph_env["artifacts"],
        settings={"max_cycles_per_objective": 2},
    )
    store = RuntimeStore(graph_env["db"])
    first = store.create_run(project_id="alpha-project", objective="o")
    second = store.create_run(
        project_id="alpha-project",
        objective="o",
        parent_run_id=first.run.run_id if hasattr(first, "run") else first.run_id,
        cycle_index=1,
    )
    from research_os.runtime.cycles import CycleResult

    result = CycleResult(
        run=second,
        status=RunStatus.SUCCEEDED,
        terminal_state=TerminalState.DONE_FOR_NOW,
        pending_approval_id=None,
        recommendation="START_NEXT_CYCLE",
        notes=(),
        state={},
    )
    proceed, why = should_continue(db=graph_env["db"], config=config, result=result)
    assert proceed is False
    assert "ceiling" in why


def test_continuation_stops_when_the_cycle_says_done(graph_env: dict[str, Any]) -> None:
    store = RuntimeStore(graph_env["db"])
    run = store.create_run(project_id="alpha-project", objective="o")
    from research_os.runtime.cycles import CycleResult

    result = CycleResult(
        run=run,
        status=RunStatus.SUCCEEDED,
        terminal_state=TerminalState.DONE_FOR_NOW,
        pending_approval_id=None,
        recommendation="DONE_FOR_NOW",
        notes=(),
        state={},
    )
    proceed, why = should_continue(
        db=graph_env["db"], config=graph_env["config"], result=result
    )
    assert proceed is False
    assert "DONE_FOR_NOW" in why


# ------------------------------------------------------------- retention -----
def test_checkpoints_for_old_finished_cycles_are_pruned(
    graph_env: dict[str, Any],
) -> None:
    """Without this the checkpoint table is append-only forever."""

    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    result = start_cycle(
        config=graph_env["config"],
        db=graph_env["db"],
        project_id="alpha-project",
        repo_path=graph_env["repo"],
        objective="o",
        models=router,
    )
    thread = result.run.thread_id
    assert thread

    with graph_env["db"].tx() as conn:
        present = conn.execute(
            "select count(*) as n from checkpoints where thread_id = %s", (thread,)
        ).fetchone()
    assert present["n"] > 0

    # A recently finished cycle is left alone.
    assert prune(graph_env["db"], graph_env["dsn"], retention_days=30) == ()

    with graph_env["db"].tx() as conn:
        conn.execute(
            "update research_runs set finished_at = now() - interval '90 days' "
            "where run_id = %s",
            (result.run.run_id,),
        )
    assert prune(graph_env["db"], graph_env["dsn"], retention_days=30) == (thread,)
    with graph_env["db"].tx() as conn:
        after = conn.execute(
            "select count(*) as n from checkpoints where thread_id = %s", (thread,)
        ).fetchone()
    assert after["n"] == 0


def test_an_effect_whose_outcome_cannot_be_established_is_escalated_not_repeated(
    graph_env: dict[str, Any], tmp_path: Path
) -> None:
    """The other half of the idempotency contract.

    When a crash leaves a visible effect whose outcome nothing can check, the
    honest answer is "I do not know" -- not "probably it failed, try again".
    Guessing wrong here submits the same experiment to a cluster twice, so the
    cycle ends in a state that names the invocation for a person to inspect.
    """

    effects = tmp_path / "effects.log"
    _run_child(
        graph_env,
        "start",
        effects=effects,
        crash_in="perform_action",
        replay_mode="unreconcilable",
        expect_returncode=9,
    )
    assert effects.read_text().splitlines() == ["action"]

    store = RuntimeStore(graph_env["db"])
    run = store.list_runs(project_id="alpha-project")[0]
    ledger = InvocationLedger(graph_env["db"])
    assert len(ledger.abandon_stale(older_than_seconds=0)) == 1

    resumed = _run_child(
        graph_env,
        "resume",
        effects=effects,
        run_id=run.run_id,
        replay_mode="unreconcilable",
    )
    assert resumed["terminal_state"] == str(TerminalState.FATAL_INFRASTRUCTURE_ERROR)
    assert resumed["status"] == str(RunStatus.FAILED)
    assert effects.read_text().splitlines() == ["action"], (
        "an unknowable outcome was resolved by repeating the side effect"
    )
    assert any("cannot be established" in note for note in resumed["notes"])
