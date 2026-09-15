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
from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.locks import RepositoryBusyError, repository_lock

LOG = logging.getLogger("research_os.runtime.actions.coding")


def canonical_fingerprint(repo: Path) -> dict[str, str]:
    """Hash the canonical capsule and every Git ref.

    The two things a coding run must not touch. Refs rather than just HEAD,
    because a push or a branch write is as much of an escape as a commit, and
    ``show-ref`` lists all of them in one call.

    Cheap: a capsule is a few dozen small YAML files.
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
        found["<git-refs>"] = hashlib.sha256(
            (refs.stdout or "").encode("utf-8")
        ).hexdigest()
    except ResearchOSError as exc:  # pragma: no cover - a repo with no refs
        found["<git-refs>"] = f"unreadable: {exc}"
    return found


def _describe_drift(before: dict[str, str], after: dict[str, str]) -> str:
    changed = sorted(
        key for key in set(before) | set(after) if before.get(key) != after.get(key)
    )
    return ", ".join(changed[:12]) + ("..." if len(changed) > 12 else "")


def _controller(context: CycleContext) -> Any:
    """Build a v1 automation controller with this machine's providers."""

    from research_os.automation.commands import provider_registry
    from research_os.automation.config import load_config
    from research_os.automation.controller import AutomationController

    return AutomationController(providers=provider_registry(), config=load_config())


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


def _reconcile_worktree(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Ask Git whether a previous attempt's automation run already exists.

    This is what makes the coding action safe to interrupt: the branch name is
    derived from the run and task ids, so its presence is evidence the worktree
    was created. Returning ``None`` means it was not, and the action may proceed.
    """

    from research_os.automation.store import RunStore

    reserved = str(plan.get("_reserved_run_id") or "")
    if not reserved:
        return None
    try:
        store = RunStore.open(reserved)
        run = store.load()
    except ResearchOSError:
        return None
    return {
        "ok": run.state is RunState.READY_FOR_HUMAN,
        "detail": f"recovered automation run {reserved} in state {run.state}",
        "data": _run_payload(run, store),
    }


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

    controller = _controller(context)
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

    def perform() -> dict[str, Any]:
        # What the canonical checkout looks like before anything runs. Compared
        # afterwards, because the acceptance commands execute code the builder
        # just wrote and nothing here can stop them reaching the repository.
        before = canonical_fingerprint(repo)

        # The repository lock covers preflight and worktree creation -- the part
        # that touches the canonical checkout. It is released before the builder
        # runs, because a coding task can take many minutes and holding the lock
        # would serialise every other cycle on this repository behind it.
        with repository_lock(context.db, str(repo)):
            store, _run = controller.start(
                project_path=repo, goal=goal, budget=budget, attempt=key[-8:]
            )
        final = controller.execute(store)

        after = canonical_fingerprint(repo)
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
