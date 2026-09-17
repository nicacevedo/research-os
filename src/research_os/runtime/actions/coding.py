"""Autonomous code change, behind the runtime's contracts.

This handler is thin on purpose, and the reason is the most important design
decision in this module: **it delegates to
:class:`research_os.automation.controller.AutomationController` rather than
reimplementing it.** That controller already does the whole lifecycle --

```text
frozen base SHA -> isolated worktree -> builder -> format/lint/type checks
-> targeted tests -> independent review -> one bounded repair
-> an isolated branch with uncommitted changes, for a person to review
```

The last step says "uncommitted" deliberately. There is no ``git commit``
anywhere in this system -- ``gitutil`` has no commit helper -- so the run ends
with a worktree on its own branch holding changes nobody has committed. An
earlier version of this docstring and of the policy rationale said "candidate
commit", which an independent review pointed out is not what happens, and which
would have sent a researcher to look at a branch with nothing on it.

-- with scope enforcement taken from the observed diff, a command policy that
authorises whole argv vectors rather than program names, symlink containment,
uv-lockfile restoration, and a reviewer moved off the builder's provider family
where one is available. All of that is covered by roughly a thousand tests. A
second implementation living behind a nicer interface would be a second set of
bugs, and the interesting ones would be the isolation bugs.

What the runtime adds is the three things the v1 layer does not have:

**Durable idempotency.** A worktree created and then lost to a crash is a
worktree nobody will clean up and a branch that blocks the next attempt. The
creation goes through the invocation ledger with a key derived from the run, the
cycle and the base commit, and the reconciler asks Git whether the branch
already exists.

**Serialisation against other workers.** The repository mutation lock is held
while the worktree is created, so two cycles on one repository cannot race. It
is released before the builder runs, because holding a lock for the length of a
coding task would serialise the whole runtime on one repository.

**Nothing is integrated.** The v1 controller stops at ``READY_FOR_HUMAN`` and
never merges or pushes. The runtime keeps that: integration is an `A2` action,
and it is one the person performs.

**And a boundary this module cannot move, stated plainly.** The pipeline runs
the project's acceptance commands, and it runs them *after* the builder has
written files in scope. So ``pytest`` imports and executes Python a model wrote
one step earlier, with the researcher's environment and credentials. Worktree
isolation protects the canonical checkout from the *builder*; it is not an OS
sandbox, and `SECURITY.md` has always said so.

What R5 changes is that this now happens without a person deciding to run it,
which is a real escalation of the same risk. An independent review traced the
consequence: executed test code could commit to the canonical `.research/`,
push, or clear an approval gate.

This module cannot fix that -- a sandbox is the fix, and it is
infrastructure this deployment does not have. What it does instead is *detect*
it: :func:`canonical_fingerprint` hashes the canonical capsule and every Git ref
before the pipeline runs and again afterwards, and a difference fails the action
as ``POLICY_REFUSED`` with the paths named. That turns a silent escape into a
loud one. It does not prevent the write, and it detects nothing that happens
outside the repository.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from research_os.automation import gitutil
from research_os.automation.models import Budget, RunState
from research_os.errors import BudgetExceededError, ResearchOSError
from research_os.runtime.actions.base import ActionOutcome, charge_delegated_spend
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.locks import RepositoryBusyError, repository_lock
from research_os.runtime.spend import DelegatedSpendAuthority
from research_os.sandbox import SandboxError, SandboxMode

LOG = logging.getLogger("research_os.runtime.actions.coding")


def owned_ref_prefix(automation_run_id: str) -> str:
    """The ref namespace one automation run is entitled to create.

    ``worktree.branch_name`` is ``automation/<run id>/<task id>``, lowercased and
    deterministic, so a run's branches live under exactly one prefix and that
    prefix is computable *before* the pipeline starts. Which is what makes it
    usable as an exemption rather than as an excuse: the namespace is named in
    advance from the reserved identity, and a ref outside it is still an escape,
    including one under another run's namespace.
    """

    return f"refs/heads/automation/{automation_run_id.lower()}/"


def canonical_fingerprint(
    repo: Path, *, owned_ref_prefixes: tuple[str, ...] = ()
) -> dict[str, str]:
    """Hash the canonical capsule and every Git ref this run must not touch.

    The two things a coding run must not change. Refs rather than just HEAD,
    because a push or a branch write is as much of an escape as a commit, and
    ``show-ref`` lists all of them in one call.

    **One ref per key, not one hash of all of them.** The first version hashed
    ``show-ref``'s whole output into a single ``<git-refs>`` entry, and that was
    wrong in two directions at once. It could not say *which* ref moved, so a
    real escape was reported as the opaque string ``<git-refs>``; and, worse, it
    could not distinguish a ref the run was entitled to create from one it was
    not -- so it flagged the pipeline's own worktree branch.

    That second half was not a theoretical weakness. ``AutomationController``
    creates a Git worktree on a new branch in the canonical repository, because
    that is what worktree isolation *is*. Every real coding run therefore ended
    with a ref the fingerprint had not seen, and the handler failed it as
    ``POLICY_REFUSED`` -- "the coding run changed the canonical checkout" --
    with the branch, the diff and a perfectly good review already on disk. The
    runtime's only code-writing capability could not succeed. It survived
    review because the only test that drove the handler substituted a controller
    that creates no worktree, so the guard had never met a real pipeline.

    ``owned_ref_prefixes`` is the narrow, named exemption: refs under a prefix
    this run reserved before it started. Anything else -- a new branch outside
    it, a moved existing ref, a deleted one -- still fails, and now says which.

    One deliberate blind spot: ``.research/runtime/`` is excluded. It is
    gitignored scratch space that the capsule specification reserves and nothing
    parses, so a write there is not a scientific-state change. It does mean a
    command that writes only to that directory is not detected, which is the
    correct trade and is recorded here so it is not a surprise.

    Cheap: a capsule is a few dozen small YAML files and a repository has tens
    of refs.
    """

    import hashlib

    found: dict[str, str] = {}
    capsule = repo / ".research"
    if capsule.is_dir():
        for path in sorted(capsule.rglob("*")):
            if path.is_file() and "runtime" not in path.relative_to(capsule).parts:
                found[str(path.relative_to(repo))] = hashlib.sha256(
                    path.read_bytes()
                ).hexdigest()
    try:
        refs = gitutil.git(["show-ref"], cwd=repo, check=False)
    except ResearchOSError as exc:  # pragma: no cover - a repo with no refs
        found["<git-refs>"] = f"unreadable: {exc}"
        return found
    for line in (refs.stdout or "").splitlines():
        parts = line.split(maxsplit=1)
        if len(parts) != 2:
            continue
        sha, ref = parts[0].strip(), parts[1].strip()
        if any(ref.startswith(prefix) for prefix in owned_ref_prefixes):
            continue
        found[f"<git-ref> {ref}"] = sha
    return found


def _describe_drift(before: dict[str, str], after: dict[str, str]) -> str:
    changed = sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
    return ", ".join(changed[:12]) + ("..." if len(changed) > 12 else "")


def _controller(context: CycleContext, *, autonomy: str, authority: Any = None) -> Any:
    """Build a v1 automation controller with this machine's providers.

    ``authority`` wraps every adapter so each model call this pipeline makes
    reserves runtime budget before it happens and settles after. The controller
    is unchanged and unaware: it was handed a registry and calls ``invoke`` on
    it, which is exactly why the registry is where the budget belongs. A
    reservation the ledger refuses raises ``BudgetExceededError``, which this
    controller already treats as a terminal run failure.

    The one thing the runtime overrides is containment. At high autonomy
    nobody is watching, and the pipeline runs a project's acceptance commands
    after a write-enabled worker has edited files in scope -- so pytest
    executes Python a model wrote one step earlier. Running that with the
    researcher's environment, unattended, is the exposure docs/RUNTIME.md
    §16 describes; requiring containment is the answer, and refusing to run when
    containment is unavailable is what "requiring" has to mean.

    At low and medium the researcher's own sandbox.mode decides,
    because there a person is at the keyboard and "contain where possible,
    record the absence otherwise" is a legitimate position.
    """

    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config
    from research_os.automation.controller import AutomationController

    del context
    providers = provider_registry()
    if authority is not None:
        providers = authority.wrap(providers)
    return AutomationController(
        providers=providers,
        config=load_config(),
        sandbox_mode=SandboxMode.REQUIRED if autonomy == "high" else None,
    )


def failure_class_for(reason: str | None) -> FailureClass:
    """Classify an automation run that did not reach READY_FOR_HUMAN.

    The reason string is the controller's own, and it names the acceptance
    command that failed when that is what happened -- which is why a check
    failure becomes ``DETERMINISTIC_CHECK_FAILED`` (repair) rather than
    ``CODE_EXCEPTION``. Getting this wrong means retrying a red test, which
    produces the same red.

    Public so ``tests/test_runtime_coding.py`` can assert the mapping directly
    rather than through a whole pipeline run.
    """

    reason = (reason or "").lower()
    if "budget" in reason:
        return FailureClass.BUDGET_EXHAUSTED
    if "scope" in reason or "symlink" in reason:
        return FailureClass.POLICY_REFUSED
    if "acceptance command" in reason or "check" in reason:
        return FailureClass.DETERMINISTIC_CHECK_FAILED
    if "provider" in reason:
        return FailureClass.PROVIDER_UNAVAILABLE
    return FailureClass.CODE_EXCEPTION


def reserved_automation_run_id(*, reservation_key: str) -> str:
    """The automation run id one reservation key always produces.

    ``make_run_id`` is deterministic in project, goal, *second* and attempt, and
    the second is exactly what a retry does not reproduce. So the timestamp is
    derived from the key instead -- a fixed, obviously-not-a-real-time stamp,
    because an id that looks like it records when the run started but is
    recomputable later would be worse than one that plainly does not. The run's
    own record carries the real time.
    """

    import hashlib

    digest = hashlib.sha256(
        f"reserved-automation-run-v1\n{reservation_key}".encode()
    ).hexdigest()
    return f"RUN-19700101T000000Z-{digest[:8]}"


def _reconcile_worktree(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Ask the automation run store whether a previous attempt already ran.

    This is what makes the coding action safe to interrupt. The reserved run id
    is re-derived here from the same durable inputs the handler used -- the run,
    the cycle, the base commit and the goal -- rather than read out of the plan.

    Re-derivation is the point. An earlier version read
    ``plan["_reserved_run_id"]``, which nothing ever set, so this returned
    ``None`` unconditionally and the crash window it existed to close was open:
    a crash between the worktree being created and the ledger recording it left
    an orphan branch, and the retry created a second automation run for one
    logical task. A reconciler that depends on state the crashed attempt was
    supposed to have left behind is a reconciler that does not work after a
    crash.

    ``None`` means the run directory is not there, so the effect did not take
    hold and the action may proceed.
    """

    from research_os.automation.store import RunStore

    del context
    repo = Path(state["repo_path"])
    goal = str(
        plan.get("rationale") or plan.get("parameters", {}).get("goal") or ""
    ).strip()
    if not goal:
        return None
    try:
        base_commit = gitutil.head_commit(repo)
    except ResearchOSError:
        return None
    key = idempotency_key(
        "coding.run", state["run_id"], state["cycle_index"], base_commit, goal
    )
    reserved = reserved_automation_run_id(reservation_key=key)
    try:
        store = RunStore.open(reserved)
        run = store.load()
    except ResearchOSError:
        return None
    return {
        "ok": run.state is RunState.READY_FOR_HUMAN,
        "detail": f"recovered automation run {reserved} in state {run.state}",
        "data": _run_payload(run, store),
        "_store_directory": str(store.directory),
        "_failure_reason": run.failure_reason,
    }


def _loaded_run(store: Any) -> Any:
    """The automation run as persisted, or ``None`` if it cannot be read.

    Used on the failure path, where the controller raised and there is no return
    value. Reading must not be able to replace the original exception, so every
    failure here is swallowed: a charge that cannot be computed is a lost charge,
    and a lost charge is better than a lost error.
    """

    try:
        return store.load()
    except Exception as exc:  # noqa: BLE001 - must not mask the cause
        LOG.warning("could not read the automation run to charge its spend: %s", exc)
        return None


def _run_payload(run: Any, store: Any) -> dict[str, Any]:
    """The small, structured record of a coding run that goes into graph state.

    Deliberately not the diff. The diff is an artifact; a checkpoint that
    carried it would grow by the size of every change the runtime ever made.
    """

    orders = [
        {
            "task_id": order.task_id,
            "title": order.title,
            "status": str(order.status),
            "changed_paths": list(order.changed_paths)[:40],
            "diff_path": order.diff_path,
            "checks_passed": order.required_checks_passed,
            "repair_attempts": order.repair_attempts,
            "branch": order.branch,
            "head_commit": order.head_commit,
        }
        for order in run.work_orders
    ]
    return {
        "automation_run_id": run.run_id,
        "state": str(run.state),
        "base_commit": run.base_commit,
        "base_branch": run.base_branch,
        "orders": orders,
        "reviews": [
            {
                "task_id": review.task_id,
                "verdict": str(review.verdict),
                "independence": str(review.independence),
                "independence_note": review.independence_note,
                "findings": [
                    {"severity": finding.severity, "message": finding.message}
                    for finding in review.findings
                ],
            }
            for review in run.reviews
        ],
        "model_calls_used": run.model_calls_used,
        "cost_usd": run.total_cost_usd(),
        "failure_reason": run.failure_reason,
        "run_directory": str(store.directory),
    }


def _archive(context: CycleContext, run: Any, store: Any) -> tuple[Any, ...]:
    """Put each order's diff into the artifact store, by content hash.

    So that "what did the runtime change" is answerable from a hash months
    later, and so that the diff itself never travels in graph state or a prompt.
    """

    refs = []
    for order in run.work_orders:
        if not order.diff_path:
            continue
        path = store.path(*Path(order.diff_path).parts)
        if not path.is_file():
            continue
        refs.append(
            context.artifacts.put_file(
                path,
                media_type="text/x-diff",
                role=f"diff:{order.task_id}",
                producer=f"automation:{run.run_id}",
            )
        )
    return tuple(refs)


def run_coding_task(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Plan, build, check and independently review one bounded code change.

    The whole v1 pipeline, dispatched once. Returns the worktree path, the
    branch, the observed diff as an artifact, and the reviewer's verdict.
    Commits nothing and integrates nothing.
    """

    repo = Path(state["repo_path"])
    goal = str(
        plan.get("rationale") or plan.get("parameters", {}).get("goal") or ""
    ).strip()
    if not goal:
        return ActionOutcome.failed(
            "a coding task needs a goal; the plan supplied neither a rationale nor "
            "parameters.goal",
            failure_class=FailureClass.POLICY_REFUSED,
        )

    # Defaults to "high" when the state does not say. Fail closed: an absent
    # autonomy setting must not be the thing that quietly stops containment
    # being required, and every production path seeds it -- `cycles._execute`
    # puts the run's own setting into the initial state.
    authority = DelegatedSpendAuthority(
        budgets=context.budgets,
        run_id=str(state["run_id"]),
        project_id=str(state["project_id"]),
        work_id=state.get("work_id"),
        action="edit_in_worktree",
    )
    controller = _controller(
        context,
        autonomy=str(state.get("autonomy") or "high"),
        authority=authority,
    )
    budget = Budget(
        max_model_calls=int(plan.get("parameters", {}).get("max_model_calls", 8)),
        max_write_work_orders=1,
        max_work_orders=2,
    )

    try:
        base_commit = gitutil.head_commit(repo)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not read {repo}: {exc}", failure_class=FailureClass.GIT_CONFLICT
        )

    key = idempotency_key(
        "coding.run", state["run_id"], state["cycle_index"], base_commit, goal
    )
    # The automation run's identity, decided *before* the controller is called.
    #
    # `AutomationController.start` has taken `reserved_run_id` since v1.0.0,
    # precisely to close this window, and this handler was not passing it. The
    # consequence was that `_reconcile_worktree` read
    # `plan["_reserved_run_id"]`, which nothing ever set, so it returned None
    # unconditionally -- and the crash window the reconciler existed to close
    # was open. A crash between the worktree being created and the ledger
    # recording it left an orphan branch and a retry that created a second
    # automation run for one logical task.
    #
    # Derived from the idempotency key, which contains the run, the cycle, the
    # base commit and the goal and nothing per-attempt, so a retry computes the
    # same id.
    reserved_run_id = reserved_automation_run_id(reservation_key=key)

    # The one ref namespace this run is allowed to add to, named before it
    # starts. `reserved_run_id` is derived from the idempotency key, so a retry
    # exempts exactly the same namespace its predecessor used -- and a crashed
    # attempt's branch is therefore not read as an escape by the attempt that
    # adopts it.
    owned = (owned_ref_prefix(reserved_run_id),)

    def perform() -> dict[str, Any]:
        # What the canonical checkout looks like before anything runs. Compared
        # afterwards, because the acceptance commands execute code the builder
        # just wrote and nothing here can stop them reaching the repository.
        before = canonical_fingerprint(repo, owned_ref_prefixes=owned)

        # The repository lock covers preflight and worktree creation -- the part
        # that touches the canonical checkout. It is released before the builder
        # runs, because a coding task can take many minutes and holding the lock
        # would serialise every other cycle on this repository behind it.
        with repository_lock(context.db, str(repo)):
            store, _run = controller.start(
                project_path=repo,
                goal=goal,
                budget=budget,
                attempt=key[-8:],
                reserved_run_id=reserved_run_id,
            )
        # `finally`, because `AutomationController.execute` fails the run on
        # disk and then **re-raises**, so a charge placed after the call is
        # unreachable on every ordinary failure path -- a reviewer returning
        # FAIL, required acceptance commands failing, an order producing no
        # changes, any `ProviderInvocationError`. All of those happen *after*
        # model calls. A final adversarial review executed it: two invocations
        # made, `spent=0` on both dimensions, zero `model_calls` rows, and
        # `CODE_EXCEPTION` maps to REPAIR so the uncharged spend repeats per
        # attempt. The proposal path was given exactly this repair and this one
        # was not, while the changelog claimed both.
        #
        # The invocations are read from the *store* rather than from `final`,
        # because on the raising path there is no `final` -- `_fail` has already
        # persisted the run, invocations included.
        try:
            final = controller.execute(store)
        finally:
            charge_delegated_spend(
                state,
                context,
                getattr(_loaded_run(store), "invocations", ()),
                action="edit_in_worktree",
                authority=authority,
            )

        after = canonical_fingerprint(repo, owned_ref_prefixes=owned)
        if after != before:
            drift = _describe_drift(before, after)
            LOG.error(
                "a coding run changed the canonical checkout of %s: %s", repo, drift
            )
            return {
                "ok": False,
                "detail": (
                    f"the coding run changed the canonical checkout, which it must "
                    f"never do: {drift}. Something executed during the acceptance "
                    f"commands reached outside the worktree. The candidate branch "
                    f"is left in place for inspection and nothing here will act on "
                    f"it."
                ),
                "data": {**_run_payload(final, store), "canonical_drift": drift},
                "_store_directory": str(store.directory),
                "_failure_reason": "canonical checkout modified",
                "_escaped": True,
            }
        return {
            "ok": final.state is RunState.READY_FOR_HUMAN,
            "detail": (
                f"automation run {final.run_id} reached {final.state}"
                if final.state is RunState.READY_FOR_HUMAN
                else f"automation run {final.run_id} failed: {final.failure_reason}"
            ),
            "data": _run_payload(final, store),
            "_store_directory": str(store.directory),
            "_failure_reason": final.failure_reason,
        }

    try:
        outcome = context.ledger.run(
            key=key,
            kind="coding.run",
            run_id=state["run_id"],
            request={"goal": goal, "base_commit": base_commit},
            perform=perform,
            reconcile=lambda _invocation: _reconcile_worktree(state, context, plan),
        )
    except SandboxError as exc:
        # The policy required containment and this host cannot provide it. A
        # policy refusal rather than a code failure, because there is nothing
        # to repair and retrying would ask the same question of the same
        # kernel. The message names the blocker and the one-line change that
        # would let the run proceed.
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.CAPABILITY_DENIED
        )
    except BudgetExceededError as exc:
        # The runtime budget refused a call before the provider was asked, or
        # the v1 controller's own ceiling stopped the pipeline. Terminal either
        # way: retrying spends the attempt budget on a question whose answer
        # cannot have changed.
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )
    except RepositoryBusyError as exc:
        # Not a failure: another cycle is mutating this repository. The work item
        # goes back on the queue rather than consuming an attempt.
        return ActionOutcome.failed(str(exc), failure_class=FailureClass.GIT_CONFLICT)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"the coding pipeline failed: {exc}",
            failure_class=FailureClass.CODE_EXCEPTION,
        )

    result = outcome.result
    payload = dict(result.get("data") or {})
    artifacts: tuple[Any, ...] = ()
    directory = result.get("_store_directory")
    if directory:
        from research_os.automation.store import RunStore

        try:
            store = RunStore(Path(directory))
            artifacts = _archive(context, store.load(), store)
        except ResearchOSError as exc:
            LOG.debug("could not archive diffs from %s: %s", directory, exc)

    if not result.get("ok"):
        reason = str(result.get("_failure_reason") or result.get("detail") or "unknown")
        return ActionOutcome.failed(
            str(result.get("detail") or reason),
            # An escape is a policy refusal, not a code failure: it must not be
            # repaired and retried, because repairing it would run the same
            # escaping code again.
            failure_class=(
                FailureClass.POLICY_REFUSED
                if result.get("_escaped")
                else failure_class_for(reason)
            ),
            data=payload,
            artifacts=artifacts,
        )

    return ActionOutcome.succeeded(
        str(result.get("detail") or "the coding pipeline finished"),
        data=payload,
        artifacts=artifacts,
    )
