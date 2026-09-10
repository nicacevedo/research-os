"""The deterministic orchestrator.

This is ordinary Python. It decides what happens next, what may run, what the
budget allows, and what state the run is in. Models are called from here as
bounded workers and cannot influence any of that: no model output is ever mapped
onto a run state, and every gate is re-checked locally before the run is allowed
to finish.

The run ends at READY_FOR_HUMAN with a diff on a branch. It never merges, never
pushes, and never touches scientific acceptance.
"""

from __future__ import annotations

import hashlib
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from pydantic import ValidationError

from research_os.automation.analyst import (
    ANALYST_SCHEMA,
    build_analyst_prompt,
    parse_analyst_report,
    render_analyst_data,
    snapshot_evidence,
    snapshot_state,
)
from research_os.automation.checks import run_acceptance_command, tail
from research_os.automation.command_policy import authorize_planner_commands
from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.context import ContextPacket, build_context, render_context
from research_os.automation.executor import (
    build_coder_prompt,
    build_repair_prompt,
    collect_evidence,
)
from research_os.automation.filescope import (
    assert_contained_symlinks,
    outbound_symlinks,
)
from research_os.automation.gitutil import (
    current_branch,
    has_commits,
    head_commit,
    porcelain_status,
)
from research_os.automation.models import (
    TIMESTAMP_FORMAT,
    AcceptanceCommand,
    Access,
    AnalystReport,
    AutomationRun,
    Budget,
    CommandResult,
    DependencyArtifact,
    ModelInvocation,
    ReviewOutcome,
    ReviewVerdict,
    Role,
    RoleSetting,
    RunState,
    WorkOrder,
    WorkOrderStatus,
    assert_transition,
    utc_now,
)
from research_os.automation.planner import (
    MODEL_CALLS_PER_ANALYSIS_TASK,
    MODEL_CALLS_PER_CODING_TASK,
    PLAN_SCHEMA,
    build_planner_prompt,
    parse_plan,
    plan_to_work_orders,
    validate_plan,
)
from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    probe_registry,
)
from research_os.automation.reviewer import (
    REVIEW_SCHEMA,
    build_reviewer_prompt,
    parse_review,
    render_review_findings,
)
from research_os.automation.store import RunStore, make_run_id
from research_os.automation.worktree import (
    assert_isolated,
    create_worktree,
    release_worktree,
)
from research_os.errors import (
    AnalystOutputError,
    AutomationError,
    BudgetExceededError,
    PreflightError,
    ProviderInvocationError,
    ProviderUnavailableError,
    SnapshotMutationError,
    SymlinkScopeError,
)

PLANNER_TIMEOUT_SECONDS = 600
REVIEWER_TIMEOUT_SECONDS = 600
DEFAULT_WORK_ORDER_TIMEOUT_SECONDS = 1800
DEFAULT_ANALYST_TIMEOUT_SECONDS = 900

# One coder invocation plus one reviewer invocation per write work order.
MODEL_CALLS_PER_WORK_ORDER = MODEL_CALLS_PER_CODING_TASK


def minimum_model_calls(orders: list[WorkOrder]) -> int:
    """Return the fewest model calls these work orders could possibly need.

    A bounded repair is deliberately not counted. It is optional, so requiring
    budget for one up front would refuse plans that never need it; the repair
    path checks the live budget itself before it spends anything.
    """

    return sum(
        MODEL_CALLS_PER_ANALYSIS_TASK
        if order.role is Role.ANALYST
        else MODEL_CALLS_PER_CODING_TASK
        for order in orders
    )


#: A Git observation the controller injects itself. It is deliberately outside
#: the planner acceptance-command grammar: no planner may name ``git``, and this
#: command never passes through ``authorize_planner_commands`` because it did
#: not come from a model.
WHITESPACE_CHECK = AcceptanceCommand(
    argv=["git", "diff", "--check", "HEAD"],
    description="controller-added: reject whitespace damage in the diff",
    required=True,
)

#: Where read-only workers are invoked from, relative to the run directory.
#:
#: The planner and reviewer have no tools and receive everything they reason
#: about in the context packet, so there is no reason for their process to sit
#: in the researcher's repository. It runs in runtime-owned space instead, and
#: the canonical path appears only as data in the packet.
READ_ONLY_CWD_PARTS: tuple[str, ...] = ("context",)

#: The work-order statuses that actually satisfy a dependency.
#:
#: A coding order is usable once it has executed and, if it got that far,
#: passed its checks. An analysis order is usable once it has produced a
#: validated report. Nothing else counts, so a dependent order never runs on
#: findings that were never produced.
SATISFIED_DEPENDENCY_STATUSES: frozenset[WorkOrderStatus] = frozenset(
    {
        WorkOrderStatus.EXECUTED,
        WorkOrderStatus.CHECKS_PASSED,
        WorkOrderStatus.REVIEWED,
        WorkOrderStatus.ANALYZED,
    }
)


class AutomationController:
    """Drives one automation run from a goal to READY_FOR_HUMAN."""

    def __init__(
        self,
        *,
        providers: dict[str, ProviderAdapter],
        config: AutomationConfig,
    ) -> None:
        self.providers = providers
        self.config = config

    # -- run creation ----------------------------------------------------

    def start(
        self,
        *,
        project_path: Path,
        goal: str,
        dry_run: bool = False,
        skip_planner: bool = False,
        budget: Budget | None = None,
    ) -> tuple[RunStore, AutomationRun]:
        """Preflight, build context, and plan. Never invokes a write worker."""

        goal = goal.strip()
        if not goal:
            raise PreflightError("a run goal must contain at least one character")

        preflight = self._preflight(project_path)
        resolved = self._resolve_roles()
        effective_budget = budget or self.config.budget

        created_at = utc_now()
        run = AutomationRun(
            run_id=make_run_id(
                project_path=str(preflight.root),
                goal=goal,
                created_at=created_at,
            ),
            project_path=str(preflight.root),
            goal=goal,
            base_commit=preflight.commit,
            base_branch=preflight.branch,
            created_at=created_at,
            updated_at=created_at,
            state=RunState.CREATED,
            dry_run=dry_run,
            budget=effective_budget,
            roles={name: setting for name, setting in resolved.roles.items()},
            independence=resolved.independence,
            independence_note=resolved.note,
        )
        store = RunStore.create(run)
        store.append_event(
            "run_created",
            project_path=run.project_path,
            base_commit=run.base_commit,
            base_branch=run.base_branch,
            goal=run.goal,
            dry_run=dry_run,
        )
        for substitution in resolved.substitutions:
            store.append_event("provider_substituted", detail=substitution)
        store.append_event(
            "independence_assessed",
            independence=str(resolved.independence),
            note=resolved.note,
        )

        try:
            run = self._transition(store, run, RunState.PREFLIGHTED)
            packet = build_context(project_path=preflight.root, goal=goal)
            run = self._archive_context(store, run, packet)
            if skip_planner:
                return store, run
            run = self._plan(store, run, packet, resolved)
        except AutomationError as exc:
            run = self._fail(store, run, str(exc))
            raise
        return store, run

    # -- execution -------------------------------------------------------

    def execute(self, store: RunStore) -> AutomationRun:
        """Dispatch the plan, check it deterministically, and have it reviewed."""

        run = store.load()
        if run.dry_run:
            raise AutomationError(
                f"{run.run_id} was created with --dry-run; start a new run "
                "without it to execute work"
            )
        if run.terminal:
            raise AutomationError(f"{run.run_id} is already {run.state}")
        if run.state is not RunState.PLAN_READY:
            raise AutomationError(
                f"{run.run_id} is {run.state}; only a PLAN_READY run can be executed"
            )
        if not run.work_orders:
            raise AutomationError(f"{run.run_id} has no work orders to execute")

        required = minimum_model_calls(list(run.work_orders))
        remaining = run.budget.max_model_calls - run.model_calls_used
        if remaining < required:
            reason = (
                f"model-call budget cannot cover this plan: {len(run.work_orders)} "
                f"work orders need {required} calls and only {remaining} of "
                f"{run.budget.max_model_calls} remain"
            )
            self._fail(store, run, reason)
            raise BudgetExceededError(reason)

        try:
            run = self._transition(store, run, RunState.EXECUTING)
            for order in list(run.work_orders):
                run = self._execute_order(store, run, order.task_id)
            run = self._transition(store, run, RunState.CHECKING)
            for order in list(run.work_orders):
                run = self._check_order(store, run, order.task_id)
            run = self._transition(store, run, RunState.REVIEWING)
            for order in list(run.work_orders):
                run = self._review_order(store, run, order.task_id)
            run = self._finish(store, run)
        except AutomationError as exc:
            if not store.load().terminal:
                run = self._fail(store, store.load(), str(exc))
            raise
        return run

    def cancel(self, store: RunStore, *, reason: str) -> AutomationRun:
        run = store.load()
        if run.terminal:
            raise AutomationError(f"{run.run_id} is already {run.state}")
        run = run.model_copy(update={"failure_reason": reason})
        run = self._transition(store, run, RunState.CANCELLED, reason=reason)
        return store.save(run.model_copy(update={"finished_at": utc_now()}))

    def cleanup(self, store: RunStore) -> tuple[AutomationRun, tuple[str, ...]]:
        """Remove this run's worktrees. Branches and run records are kept."""

        run = store.load()
        removed: list[str] = []
        records = []
        for record in run.worktrees:
            if record.removed_at is not None:
                records.append(record)
                continue
            updated = release_worktree(record, repository=Path(run.project_path))
            records.append(updated)
            removed.append(record.path)
            store.append_event(
                "worktree_removed",
                task_id=record.task_id,
                path=record.path,
                branch=record.branch,
            )
        run = store.save(run.model_copy(update={"worktrees": records}))
        return run, tuple(removed)

    # -- phases ----------------------------------------------------------

    def _plan(
        self,
        store: RunStore,
        run: AutomationRun,
        packet: ContextPacket,
        resolved: ResolvedRoles,
    ) -> AutomationRun:
        run = self._transition(store, run, RunState.PLANNING)
        planner = resolved.roles["planner"]
        prompt = build_planner_prompt(
            goal=run.goal,
            context_text=render_context(packet),
            budget=run.budget,
            allowed_programs=self.config.allowed_check_programs,
            project_path=Path(run.project_path),
        )
        run, invocation, result = self._invoke(
            store,
            run,
            role=Role.PLANNER,
            setting=planner,
            prompt=prompt,
            cwd=store.path(*READ_ONLY_CWD_PARTS),
            timeout_seconds=PLANNER_TIMEOUT_SECONDS,
            json_schema=PLAN_SCHEMA,
        )
        if not result.ok:
            raise ProviderInvocationError(
                f"planner invocation failed: {invocation.error or 'unknown error'}"
            )
        plan = parse_plan(structured=result.structured, text=result.text)
        validate_plan(
            plan,
            budget=run.budget,
            allowed_programs=self.config.allowed_check_programs,
        )
        store.write_json("plan/plan.json", plan.model_dump(mode="json"))
        orders = plan_to_work_orders(
            plan,
            project_path=Path(run.project_path),
            base_commit=run.base_commit,
            coder=resolved.roles["coder"],
            analyst=resolved.roles.get("analyst"),
            timeout_seconds=DEFAULT_WORK_ORDER_TIMEOUT_SECONDS,
            analyst_timeout_seconds=DEFAULT_ANALYST_TIMEOUT_SECONDS,
        )
        run = store.save(
            run.model_copy(update={"work_orders": orders, "plan_summary": plan.summary})
        )
        store.append_event(
            "plan_accepted",
            tasks=[order.task_id for order in orders],
            roles=[str(order.role) for order in orders],
            summary=plan.summary,
            minimum_model_calls=minimum_model_calls(orders),
        )
        return self._transition(store, run, RunState.PLAN_READY)

    def _execute_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        run = self._assert_dependencies_met(store, run, task_id)
        if run.order(task_id).role is Role.ANALYST:
            return self._execute_analysis_order(store, run, task_id)
        return self._execute_coding_order(store, run, task_id)

    def _assert_dependencies_met(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        """Block a work order whose dependencies have not actually succeeded.

        A dependency is met only in a status that means the work behind it is
        done: executed or checked for a coding task, analysed for an analysis
        task. Anything else - pending, running, failed, blocked - blocks the
        dependent order rather than letting it run on absent findings.
        """

        order = run.order(task_id)
        blocked = [
            dependency
            for dependency in order.dependencies
            if run.order(dependency).status not in SATISFIED_DEPENDENCY_STATUSES
        ]
        if not blocked:
            return run
        detail = ", ".join(
            f"{dependency} is {run.order(dependency).status}" for dependency in blocked
        )
        run = self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.BLOCKED,
            failure_reason=f"unmet dependencies: {detail}",
        )
        store.append_event(
            "dependency_unmet",
            task_id=task_id,
            dependencies=list(blocked),
            detail=detail,
        )
        raise AutomationError(f"{task_id} is blocked by {', '.join(blocked)}")

    def _execute_analysis_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        """Run one bounded, read-only analysis inside a pinned Git snapshot.

        The snapshot is created here, pinned to the work order's base commit,
        and is never the researcher's checkout. The worker is given exactly the
        read-only file tools its role declares. Its output is parsed and
        archived as data; before any of it is believed, the snapshot is
        compared with what it was before the invocation, and a run whose
        analyst changed anything fails outright.
        """

        order = run.order(task_id)
        record = create_worktree(
            run_id=run.run_id,
            task_id=task_id,
            repository=Path(run.project_path),
            base_commit=order.base_commit,
        )
        run = store.save(run.model_copy(update={"worktrees": [*run.worktrees, record]}))
        store.append_event(
            "worktree_created",
            task_id=task_id,
            path=record.path,
            branch=record.branch,
            base_commit=record.base_commit,
            purpose="analysis snapshot",
        )
        assert_isolated(record, canonical_repository=Path(run.project_path))

        snapshot = Path(record.path)
        self._assert_symlinks_contained(
            store,
            run,
            task_id,
            snapshot,
            stage="before the analyst was invoked",
        )
        setting = self._analyst_setting(run, order)
        before = snapshot_state(
            head=head_commit(snapshot),
            status=porcelain_status(snapshot),
        )
        run = self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.RUNNING,
            worktree_path=record.path,
            branch=record.branch,
        )
        if not before["clean"]:
            return self._fail_snapshot(
                store,
                run,
                task_id,
                before=before,
                after=before,
                detail=(
                    "the analysis snapshot was already dirty before the worker "
                    "ran, so no read-only guarantee could be established"
                ),
            )

        packet = build_context(project_path=snapshot, goal=run.goal)
        prompt = build_analyst_prompt(order, context_text=render_context(packet))
        run, invocation, result = self._invoke(
            store,
            run,
            role=Role.ANALYST,
            setting=setting,
            prompt=prompt,
            cwd=snapshot,
            timeout_seconds=order.timeout_seconds,
            json_schema=ANALYST_SCHEMA,
            task_id=task_id,
        )
        run = self._update_order(
            store,
            run,
            task_id,
            invocation_ids=[
                *run.order(task_id).invocation_ids,
                invocation.invocation_id,
            ],
        )
        store.append_event(
            "analyst_invoked",
            task_id=task_id,
            invocation_id=invocation.invocation_id,
            snapshot_path=record.path,
            snapshot_commit=before["head"],
            read_paths=list(order.read_paths),
            tools=list(setting.tools),
            access=str(setting.access),
        )

        # The mutation gate runs before anything the worker said is believed,
        # and regardless of whether the invocation succeeded: a worker that
        # failed or timed out could still have changed the snapshot.
        after = snapshot_state(
            head=head_commit(snapshot),
            status=porcelain_status(snapshot),
        )
        store.write_text(
            f"analysis/{task_id}.snapshot.json",
            snapshot_evidence(before=before, after=after),
        )
        if after["head"] != before["head"] or not after["clean"]:
            return self._fail_snapshot(
                store,
                run,
                task_id,
                before=before,
                after=after,
                detail="the analysis worker changed the snapshot it was reading",
            )
        store.append_event(
            "analyst_snapshot_verified",
            task_id=task_id,
            snapshot_commit=after["head"],
            head_unchanged=True,
            clean=True,
        )

        if not result.ok:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=invocation.error or "analyst invocation failed",
            )
            raise ProviderInvocationError(
                f"{task_id}: analyst invocation failed: "
                f"{invocation.error or 'unknown error'}"
            )

        try:
            report = parse_analyst_report(
                structured=result.structured,
                text=result.text,
                task_id=task_id,
                provider=setting.provider,
                model=invocation.model or setting.model,
                invocation_id=invocation.invocation_id,
                snapshot_commit=after["head"],
            )
        except AnalystOutputError as exc:
            store.append_event(
                "analyst_output_rejected",
                task_id=task_id,
                invocation_id=invocation.invocation_id,
                detail=str(exc),
            )
            self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=f"analyst output is not usable: {exc}",
            )
            raise
        report = report.model_copy(
            update={"raw_output_path": invocation.raw_output_path}
        )
        analysis_path = store.write_json(
            f"analysis/{task_id}.json", report.model_dump(mode="json")
        )
        store.append_event(
            "analyst_output_validated",
            task_id=task_id,
            invocation_id=invocation.invocation_id,
            artifact=analysis_path,
            findings=len(report.findings),
            evidence=len(report.evidence),
            uncertainties=len(report.uncertainties),
        )
        return self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.ANALYZED,
            analysis_path=analysis_path,
            head_commit=after["head"],
        )

    def _fail_snapshot(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        *,
        before: dict[str, object],
        after: dict[str, object],
        detail: str,
    ) -> NoReturn:
        """Record a snapshot-read violation and stop. Nothing is restored.

        Quietly resetting the snapshot and carrying on would hide the only
        evidence that a read-only boundary did not hold, so the work order and
        the run both fail and the violation is ledgered.
        """

        reason = (
            f"{detail}: HEAD was {before['head']} and is now {after['head']}; "
            f"working tree {'clean' if after['clean'] else 'dirty'}"
        )
        store.append_event(
            "analyst_snapshot_violation",
            task_id=task_id,
            head_before=before["head"],
            head_after=after["head"],
            clean_before=before["clean"],
            clean_after=after["clean"],
            status_after=after["status"],
            detail=detail,
        )
        self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.FAILED,
            failure_reason=reason,
        )
        raise SnapshotMutationError(f"{task_id}: {reason}")

    def _analyst_setting(self, run: AutomationRun, order: WorkOrder) -> RoleSetting:
        """Return the analysis role, refusing anything but a snapshot reader."""

        setting = run.roles.get("analyst")
        if setting is None:
            raise AutomationError(
                f"{order.task_id} is an analysis work order but this run has no "
                "analyst role configured"
            )
        if setting.access is not Access.SNAPSHOT_READ:
            raise AutomationError(
                f"the analyst role declares {setting.access} access; an "
                "analysis work order is only ever dispatched to a snapshot_read "
                "role with read-only file tools"
            )
        return setting.model_copy(
            update={"provider": order.provider, "model": order.model}
        )

    def _execute_coding_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        order = run.order(task_id)
        record = create_worktree(
            run_id=run.run_id,
            task_id=task_id,
            repository=Path(run.project_path),
            base_commit=order.base_commit,
        )
        run = store.save(run.model_copy(update={"worktrees": [*run.worktrees, record]}))
        store.append_event(
            "worktree_created",
            task_id=task_id,
            path=record.path,
            branch=record.branch,
            base_commit=record.base_commit,
        )
        assert_isolated(record, canonical_repository=Path(run.project_path))

        worktree = Path(record.path)
        self._assert_symlinks_contained(
            store,
            run,
            task_id,
            worktree,
            stage="before the writer was invoked",
        )
        packet = build_context(project_path=worktree, goal=run.goal)
        dependency_data, artifacts = self._dependency_data(store, run, order)
        prompt = build_coder_prompt(
            order,
            context_text=render_context(packet),
            dependency_data=dependency_data,
        )
        setting = self._coder_setting(run, order)
        run = self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.RUNNING,
            worktree_path=record.path,
            branch=record.branch,
            dependency_artifacts=artifacts,
        )
        for artifact in artifacts:
            store.append_event(
                "dependency_input_used",
                task_id=task_id,
                source_task_id=artifact.task_id,
                source_role=str(artifact.role),
                artifact=artifact.path,
                sha256=artifact.sha256,
            )
        run, invocation, result = self._invoke(
            store,
            run,
            role=Role.CODER,
            setting=setting,
            prompt=prompt,
            cwd=worktree,
            timeout_seconds=order.timeout_seconds,
            task_id=task_id,
        )
        run = self._update_order(
            store,
            run,
            task_id,
            invocation_ids=[
                *run.order(task_id).invocation_ids,
                invocation.invocation_id,
            ],
        )
        if not result.ok:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=invocation.error or "coder invocation failed",
            )
            raise ProviderInvocationError(
                f"{task_id}: coder invocation failed: "
                f"{invocation.error or 'unknown error'}"
            )

        evidence = collect_evidence(order, worktree=worktree)
        if not evidence.contained:
            self._fail_order_on_symlinks(
                store,
                run,
                task_id,
                evidence.outbound_symlinks,
                stage="while collecting execution evidence",
            )
        diff_path = store.write_text(f"execution/{task_id}/diff.patch", evidence.diff)
        store.write_text(f"execution/{task_id}/diff.stat.txt", evidence.diff_stat)
        store.append_event(
            "execution_observed",
            task_id=task_id,
            changed_paths=list(evidence.changed_paths),
            head_commit=evidence.head_commit,
        )
        run = self._update_order(
            store,
            run,
            task_id,
            changed_paths=list(evidence.changed_paths),
            diff_path=diff_path,
            head_commit=evidence.head_commit,
        )
        if evidence.scope_violations:
            store.append_event(
                "scope_violation",
                task_id=task_id,
                paths=list(evidence.scope_violations),
            )
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=(
                    "changed paths outside the authorised scope: "
                    + ", ".join(evidence.scope_violations)
                ),
            )
            raise AutomationError(
                f"{task_id} changed paths outside its scope: "
                + ", ".join(evidence.scope_violations)
            )
        if not evidence.produced_changes:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason="the coding worker produced no changes",
            )
            raise AutomationError(f"{task_id} produced no changes")
        return self._update_order(store, run, task_id, status=WorkOrderStatus.EXECUTED)

    @staticmethod
    def _coder_setting(run: AutomationRun, order: WorkOrder) -> RoleSetting:
        """Return the write role for one coding order.

        Built in one place so a repair invocation cannot be given a tool set
        the first attempt did not have: both paths call this, and both get the
        provider and model recorded on the work order together with the tools
        the configured role declares.
        """

        configured = run.roles.get("coder")
        return RoleSetting(
            provider=order.provider,
            model=order.model,
            effort=configured.effort if configured is not None else None,
            read_only=False,
            access=Access.ISOLATED_WRITE,
            tools=list(configured.tools) if configured is not None else [],
        )

    def _dependency_data(
        self,
        store: RunStore,
        run: AutomationRun,
        order: WorkOrder,
    ) -> tuple[str | None, list[DependencyArtifact]]:
        """Load the archived, validated output of this order's dependencies.

        Only the parsed artifact is read, and it is re-validated on the way in,
        so what reaches a downstream prompt has passed the same schema as when
        it was archived. No provider session, transcript, or free-form prior
        output is carried across: a work order's inputs are the deterministic
        order itself plus these named artifacts, and nothing else.
        """

        blocks: list[str] = []
        artifacts: list[DependencyArtifact] = []
        for dependency in order.dependencies:
            source = run.order(dependency)
            if source.role is not Role.ANALYST or not source.analysis_path:
                continue
            target = store.path(*source.analysis_path.split("/"))
            raw = target.read_text(encoding="utf-8")
            try:
                report = AnalystReport.model_validate_json(raw)
            except ValidationError as exc:
                raise AnalystOutputError(
                    f"the archived analysis for {dependency} at "
                    f"{source.analysis_path} is not a valid report: {exc}"
                ) from exc
            blocks.append(
                render_analyst_data(report, artifact_path=source.analysis_path)
            )
            artifacts.append(
                DependencyArtifact(
                    task_id=dependency,
                    role=Role.ANALYST,
                    path=source.analysis_path,
                    sha256=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
                )
            )
        return ("\n\n".join(blocks) or None), artifacts

    def _check_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        """Run every acceptance command, allowing at most one bounded repair.

        An analysis order is skipped: it changed nothing, so there is nothing
        to check, and the controller never runs a command on its behalf.
        """

        if run.order(task_id).role is Role.ANALYST:
            return run
        self._authorize_checks(store, run, task_id)
        run, failed = self._run_all_checks(store, run, task_id, attempt=1)
        if not failed:
            return self._update_order(
                store, run, task_id, status=WorkOrderStatus.CHECKS_PASSED
            )

        reason = _failed_check_reason(failed)
        if not self._repair_available(store, run, task_id, trigger=reason):
            return self._fail_checks(store, run, task_id, failed)

        run = self._repair(
            store,
            run,
            task_id,
            reason=(
                "A required acceptance command the controller ran against your "
                "change failed."
            ),
            failed_checks=failed,
        )
        run, failed = self._run_all_checks(store, run, task_id, attempt=2)
        if failed:
            store.append_event(
                "repair_budget_exhausted",
                task_id=task_id,
                repair_attempts=run.order(task_id).repair_attempts,
                trigger="required_check_failure",
                detail=(
                    "required checks failed again after the single bounded "
                    "repair; there is no second attempt"
                ),
            )
            return self._fail_checks(store, run, task_id, failed)
        return self._update_order(
            store, run, task_id, status=WorkOrderStatus.CHECKS_PASSED
        )

    def _authorize_checks(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> None:
        """Refuse to run an acceptance command the policy does not authorise."""

        try:
            authorize_planner_commands(
                run.order(task_id).acceptance_commands,
                self.config.allowed_check_programs,
            )
        except AutomationError as exc:
            self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.CHECKS_FAILED,
                failure_reason=str(exc),
            )
            store.append_event(
                "command_refused",
                task_id=task_id,
                detail=str(exc),
            )
            raise

    def _run_all_checks(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        *,
        attempt: int,
    ) -> tuple[AutomationRun, list[CommandResult]]:
        """Run every acceptance command once and return the failures.

        Every required command runs on every attempt. A repair is never
        credited by re-running only what failed: the controller re-establishes
        the whole result, so a fix that breaks a check that previously passed
        is caught.
        """

        order = run.order(task_id)
        worktree = Path(order.worktree_path or "")
        suffix = "" if attempt == 1 else f".repair{attempt - 1}"
        commands = [*order.acceptance_commands, WHITESPACE_CHECK]
        results: list[CommandResult] = []
        for index, command in enumerate(commands, start=1):
            result = run_acceptance_command(
                command,
                cwd=worktree,
                timeout_seconds=run.budget.max_command_timeout_seconds,
                stdout_path=store.path(
                    "checks", task_id, f"{index:02d}{suffix}.stdout.txt"
                ),
                stderr_path=store.path(
                    "checks", task_id, f"{index:02d}{suffix}.stderr.txt"
                ),
            )
            results.append(result)
            store.append_event(
                "command_executed",
                task_id=task_id,
                command=result.display,
                cwd=result.cwd,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
                required=result.required,
                duration_ms=result.duration_ms,
                attempt=attempt,
            )
        run = self._update_order(store, run, task_id, check_results=results)
        payload = [item.model_dump(mode="json") for item in results]
        store.write_json(f"checks/{task_id}/results.json", payload)
        store.write_json(f"checks/{task_id}/attempt-{attempt}.json", payload)
        return run, [item for item in results if item.required and not item.ok]

    def _fail_checks(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        failed: list[CommandResult],
    ) -> NoReturn:
        reason = _failed_check_reason(failed)
        self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.CHECKS_FAILED,
            failure_reason=reason,
        )
        raise AutomationError(f"{task_id}: {reason}")

    def _review_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        """Have one finished coding order reviewed, with at most one repair.

        ``FAIL`` is terminal: the run stops, and no repair is attempted, so a
        reviewer cannot drive the controller round a loop by refusing to pass.
        ``PASS_WITH_REPAIR`` spends the single repair if it is still available,
        after which every deterministic check and the reviewer both run again.
        A second ``PASS_WITH_REPAIR`` is not a third attempt: the order is
        reviewed and the unresolved findings go to the human.
        """

        if run.order(task_id).role is Role.ANALYST:
            return run
        run, outcome = self._invoke_reviewer(store, run, task_id)
        if outcome.verdict is ReviewVerdict.FAIL:
            self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=f"independent review returned FAIL: {outcome.summary}",
            )
            raise AutomationError(f"{task_id}: review returned FAIL")
        if outcome.verdict is ReviewVerdict.PASS:
            return self._update_order(
                store, run, task_id, status=WorkOrderStatus.REVIEWED
            )

        trigger = f"reviewer returned PASS_WITH_REPAIR: {outcome.summary}"
        if not self._repair_available(store, run, task_id, trigger=trigger):
            return self._update_order(
                store, run, task_id, status=WorkOrderStatus.REVIEWED
            )

        run = self._repair(
            store,
            run,
            task_id,
            reason=(
                "The deterministic checks passed, but the independent reviewer "
                "returned PASS_WITH_REPAIR and asked for the findings below to "
                "be repaired."
            ),
            failed_checks=[],
            reviewer_findings=render_review_findings(outcome),
        )
        run, failed = self._run_all_checks(store, run, task_id, attempt=2)
        if failed:
            store.append_event(
                "repair_budget_exhausted",
                task_id=task_id,
                repair_attempts=run.order(task_id).repair_attempts,
                trigger="reviewer_pass_with_repair",
                detail=(
                    "the repair broke a required check; there is no second "
                    "repair attempt"
                ),
            )
            return self._fail_checks(store, run, task_id, failed)
        run = self._update_order(
            store, run, task_id, status=WorkOrderStatus.CHECKS_PASSED
        )

        run = self._transition(store, run, RunState.REVIEWING)
        run, second = self._invoke_reviewer(store, run, task_id)
        if second.verdict is ReviewVerdict.FAIL:
            self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=(
                    f"independent review returned FAIL after the repair: "
                    f"{second.summary}"
                ),
            )
            raise AutomationError(f"{task_id}: review returned FAIL")
        if second.verdict is ReviewVerdict.PASS_WITH_REPAIR:
            store.append_event(
                "repair_budget_exhausted",
                task_id=task_id,
                repair_attempts=run.order(task_id).repair_attempts,
                trigger="reviewer_pass_with_repair",
                unresolved_findings=len(second.findings),
                detail=(
                    "the reviewer asked for a repair again after the single "
                    "bounded attempt; the remaining findings go to the human"
                ),
            )
        return self._update_order(store, run, task_id, status=WorkOrderStatus.REVIEWED)

    def _invoke_reviewer(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> tuple[AutomationRun, ReviewOutcome]:
        """Run one independent review over the frozen packet and record it."""

        order = run.order(task_id)
        setting = run.roles["reviewer"]
        diff = ""
        if order.diff_path:
            diff = store.path(*order.diff_path.split("/")).read_text(encoding="utf-8")
        worker_report = self._worker_report(store, order)
        packet = build_context(
            project_path=Path(order.worktree_path or run.project_path),
            goal=run.goal,
        )
        prompt = build_reviewer_prompt(
            order,
            diff=diff,
            check_results=list(order.check_results),
            worker_report=worker_report,
            context_text=render_context(packet),
        )
        run, invocation, result = self._invoke(
            store,
            run,
            role=Role.REVIEWER,
            setting=setting,
            prompt=prompt,
            cwd=store.path(*READ_ONLY_CWD_PARTS),
            timeout_seconds=REVIEWER_TIMEOUT_SECONDS,
            json_schema=REVIEW_SCHEMA,
            task_id=task_id,
        )
        if not result.ok:
            raise ProviderInvocationError(
                f"{task_id}: reviewer invocation failed: "
                f"{invocation.error or 'unknown error'}"
            )
        outcome = parse_review(
            structured=result.structured,
            text=result.text,
            task_id=task_id,
            provider=setting.provider,
            model=invocation.model or setting.model,
            independence=run.independence,
            independence_note=run.independence_note or "",
            invocation_id=invocation.invocation_id,
        )
        outcome = outcome.model_copy(
            update={"raw_output_path": invocation.raw_output_path}
        )
        attempt = 1 + sum(1 for item in run.reviews if item.task_id == task_id)
        suffix = "" if attempt == 1 else f".{attempt}"
        store.write_json(
            f"reviews/{task_id}{suffix}.json", outcome.model_dump(mode="json")
        )
        run = store.save(run.model_copy(update={"reviews": [*run.reviews, outcome]}))
        store.append_event(
            "review_recorded",
            task_id=task_id,
            verdict=str(outcome.verdict),
            findings=len(outcome.findings),
            independence=str(outcome.independence),
            attempt=attempt,
        )
        return run, outcome

    # -- the single bounded repair ---------------------------------------

    def _repair_available(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        *,
        trigger: str,
    ) -> bool:
        """Return whether this order may still make its one repair attempt.

        Records the refusal when it may not, so a run that stopped without
        repairing says why in the ledger rather than looking as though repair
        was never considered.
        """

        order = run.order(task_id)
        allowed = run.budget.max_repair_attempts
        if order.repair_attempts < allowed:
            return True
        store.append_event(
            "repair_budget_exhausted",
            task_id=task_id,
            repair_attempts=order.repair_attempts,
            max_repair_attempts=allowed,
            trigger=trigger,
            detail=(
                "no repair attempt remains for this work order"
                if allowed
                else "this run allows no repair attempt"
            ),
        )
        return False

    def _repair(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        *,
        reason: str,
        failed_checks: list[CommandResult],
        reviewer_findings: str | None = None,
    ) -> AutomationRun:
        """Make the one bounded repair attempt, in the same isolated worktree.

        Nothing about the order is widened for it. The worktree, the allowed
        paths, the forbidden paths, the acceptance commands, and the tool set
        are the ones the plan produced; the repair is given more *evidence* -
        the diff so far, the exact failed command and its output, the reviewer
        findings, the analyst findings - and no more *authority*. It spends a
        model call from the ordinary budget and increments the repair counter,
        so it cannot recur.

        The run re-enters EXECUTING for the attempt and returns to CHECKING,
        because that is what actually happens: a write-enabled worker runs
        again, and every deterministic check is then re-established.
        """

        order = run.order(task_id)
        worktree = Path(order.worktree_path or "")
        store.append_event(
            "repair_started",
            task_id=task_id,
            attempt=order.repair_attempts + 1,
            max_repair_attempts=run.budget.max_repair_attempts,
            trigger="required_check_failure" if failed_checks else "reviewer_verdict",
            reason=reason,
            failed_commands=[item.display for item in failed_checks],
            allowed_paths=list(order.allowed_paths),
            model_calls_used=run.model_calls_used,
        )
        run = self._transition(
            store, run, RunState.EXECUTING, reason="bounded repair", task_id=task_id
        )

        self._assert_symlinks_contained(
            store,
            run,
            task_id,
            worktree,
            stage="before the repair worker was invoked",
        )
        diff = ""
        if order.diff_path:
            diff = store.path(*order.diff_path.split("/")).read_text(encoding="utf-8")
        dependency_data, _ = self._dependency_data(store, run, order)

        # What the worktree already looks like, before the repair worker runs.
        #
        # By this point the controller has run the acceptance commands itself,
        # and those leave byproducts behind: compiled bytecode, caches,
        # coverage files. Those paths sit outside the work order's scope but
        # they are not the repair's doing, so the check below judges the repair
        # on what it actually introduced. Anything new is still refused, so
        # this narrows attribution rather than enforcement.
        baseline = set(collect_evidence(order, worktree=worktree).changed_paths)

        packet = build_context(project_path=worktree, goal=run.goal)
        prompt = build_repair_prompt(
            order,
            reason=reason,
            diff=diff,
            failed_checks=failed_checks,
            check_output=self._check_output(failed_checks),
            reviewer_findings=reviewer_findings,
            dependency_data=dependency_data,
            context_text=render_context(packet),
        )
        run, invocation, result = self._invoke(
            store,
            run,
            role=Role.CODER,
            setting=self._coder_setting(run, order),
            prompt=prompt,
            cwd=worktree,
            timeout_seconds=order.timeout_seconds,
            task_id=task_id,
        )
        attempt = run.order(task_id).repair_attempts + 1
        run = self._update_order(
            store,
            run,
            task_id,
            invocation_ids=[
                *run.order(task_id).invocation_ids,
                invocation.invocation_id,
            ],
            repair_attempts=attempt,
            repair_reason=reason,
        )
        if not result.ok:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=invocation.error or "repair invocation failed",
            )
            raise ProviderInvocationError(
                f"{task_id}: repair invocation failed: "
                f"{invocation.error or 'unknown error'}"
            )

        evidence = collect_evidence(run.order(task_id), worktree=worktree)
        if not evidence.contained:
            self._fail_order_on_symlinks(
                store,
                run,
                task_id,
                evidence.outbound_symlinks,
                stage="while collecting repair evidence",
            )
        diff_path = store.write_text(f"execution/{task_id}/diff.patch", evidence.diff)
        store.write_text(f"execution/{task_id}/diff.stat.txt", evidence.diff_stat)
        store.write_text(
            f"execution/{task_id}/diff.repair{attempt}.patch", evidence.diff
        )
        introduced = tuple(
            item for item in evidence.scope_violations if item not in baseline
        )
        # Report what the repair actually touched. A path that already differed
        # before it ran *and* was never in its scope is a byproduct of the
        # controller's own acceptance commands, and listing it as a changed
        # file would tell the reviewer this worker edited something it did not.
        # Everything in scope, and everything newly introduced, is kept.
        byproducts = set(baseline) & set(evidence.scope_violations)
        attributed = [item for item in evidence.changed_paths if item not in byproducts]
        run = self._update_order(
            store,
            run,
            task_id,
            changed_paths=attributed,
            diff_path=diff_path,
            head_commit=evidence.head_commit,
        )
        if introduced:
            store.append_event(
                "scope_violation",
                task_id=task_id,
                paths=list(introduced),
                stage="repair",
            )
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=(
                    "the repair changed paths outside the authorised scope: "
                    + ", ".join(introduced)
                ),
            )
            raise AutomationError(
                f"{task_id} repair changed paths outside its scope: "
                + ", ".join(introduced)
            )
        store.append_event(
            "repair_completed",
            task_id=task_id,
            attempt=attempt,
            invocation_id=invocation.invocation_id,
            changed_paths=attributed,
            head_commit=evidence.head_commit,
            model_calls_used=run.model_calls_used,
        )
        return self._transition(store, run, RunState.CHECKING)

    def _check_output(self, failed: list[CommandResult]) -> str:
        """Return the captured output of the failed checks, verbatim but bounded."""

        if not failed:
            return "(every required acceptance command passed)"
        blocks: list[str] = []
        for item in failed:
            blocks.append(f"$ {item.display}")
            blocks.append(f"exit_code: {item.exit_code}  timed_out: {item.timed_out}")
            for label, stored in (
                ("stdout", item.stdout_path),
                ("stderr", item.stderr_path),
            ):
                captured = _read_captured(stored)
                blocks.append(f"--- {label} ---")
                blocks.append(tail(captured) if captured else "(empty)")
            if item.error:
                blocks.append(f"--- error ---\n{item.error}")
        return "\n".join(blocks)

    def _finish(self, store: RunStore, run: AutomationRun) -> AutomationRun:
        """Re-check every gate locally before declaring the run ready."""

        blockers = ready_for_human_blockers(run)
        if blockers:
            raise AutomationError(
                "run cannot become READY_FOR_HUMAN: " + "; ".join(blockers)
            )
        run = self._transition(store, run, RunState.READY_FOR_HUMAN)
        run = store.save(run.model_copy(update={"finished_at": utc_now()}))
        store.append_event(
            "ready_for_human", tasks=[o.task_id for o in run.work_orders]
        )
        return run

    # -- primitives ------------------------------------------------------

    def _invoke(
        self,
        store: RunStore,
        run: AutomationRun,
        *,
        role: Role,
        setting: RoleSetting,
        prompt: str,
        cwd: Path,
        timeout_seconds: int,
        json_schema: dict | None = None,
        task_id: str | None = None,
    ) -> tuple[AutomationRun, ModelInvocation, InvocationResult]:
        """Spend one model call, if the budget still allows it, and record it."""

        self.assert_budget(run)
        adapter = self.providers.get(setting.provider)
        if adapter is None:
            raise ProviderUnavailableError(
                f"no adapter for provider {setting.provider!r}"
            )
        if setting.access is Access.CONTEXT_ONLY:
            self._assert_read_only_cwd(run, cwd)
        elif setting.access is Access.SNAPSHOT_READ:
            # Both halves apply: out of the canonical checkout, and inside the
            # snapshot registered for this task and nothing else.
            self._assert_read_only_cwd(run, cwd)
            self._assert_snapshot_isolation(run, cwd, task_id)
        else:
            self._assert_write_isolation(run, cwd, task_id)

        invocation_id = f"INV-{len(run.invocations) + 1:04d}"
        prompt_path = store.write_text(f"prompts/{invocation_id}.txt", prompt)
        started = utc_now()
        monotonic = time.monotonic()
        request = InvocationRequest(
            role=role,
            prompt=prompt,
            cwd=cwd,
            read_only=setting.read_only,
            timeout_seconds=timeout_seconds,
            model=setting.model,
            effort=setting.effort,
            access=setting.access,
            tools=tuple(setting.tools),
            json_schema=json_schema,
        )
        result = adapter.invoke(request)
        duration_ms = int((time.monotonic() - monotonic) * 1000)
        ended = utc_now()

        stdout_path = store.write_text(
            f"logs/{invocation_id}.stdout.txt", result.stdout
        )
        stderr_path = store.write_text(
            f"logs/{invocation_id}.stderr.txt", result.stderr
        )
        raw_path = store.write_text(
            f"model_outputs/{invocation_id}.txt", result.text or ""
        )
        if result.structured is not None:
            store.write_json(f"model_outputs/{invocation_id}.json", result.structured)

        invocation = ModelInvocation(
            invocation_id=invocation_id,
            run_id=run.run_id,
            task_id=task_id,
            role=role,
            provider=setting.provider,
            model=result.resolved_model or setting.model,
            effort=setting.effort,
            read_only=setting.read_only,
            cwd=str(cwd),
            started_at=started,
            ended_at=ended,
            duration_ms=duration_ms,
            timeout_seconds=timeout_seconds,
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            prompt_path=prompt_path,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            raw_output_path=raw_path,
            provider_session_id=result.session_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_cost_usd=result.total_cost_usd,
            error=result.error,
        )
        run = store.save(
            run.model_copy(
                update={
                    "invocations": [*run.invocations, invocation],
                    "model_calls_used": run.model_calls_used + 1,
                }
            )
        )
        store.append_event(
            "provider_invoked",
            invocation_id=invocation_id,
            role=str(role),
            provider=setting.provider,
            model=invocation.model,
            read_only=setting.read_only,
            access=str(setting.access),
            tools=list(request.tools),
            cwd=str(cwd),
            task_id=task_id,
            exit_code=result.exit_code,
            timed_out=result.timed_out,
            duration_ms=invocation.duration_ms,
            total_cost_usd=result.total_cost_usd,
            permission_denials=result.permission_denials,
            error=result.error,
            model_calls_used=run.model_calls_used,
            model_call_budget=run.budget.max_model_calls,
        )
        return run, invocation, result

    def assert_budget(self, run: AutomationRun) -> None:
        """Raise unless one more model call is within every declared limit.

        Called immediately before every invocation, so an exhausted budget stops
        the run instead of being noticed afterwards in the ledger.
        """

        if run.model_calls_used >= run.budget.max_model_calls:
            raise BudgetExceededError(
                f"model-call budget exhausted: {run.model_calls_used} of "
                f"{run.budget.max_model_calls} used"
            )
        limit = run.budget.max_wall_clock_seconds
        if limit is not None:
            elapsed = _elapsed_seconds(run.created_at, utc_now())
            if elapsed > limit:
                raise BudgetExceededError(
                    f"wall-clock budget exhausted: {elapsed}s elapsed of {limit}s"
                )

    def _assert_write_isolation(
        self,
        run: AutomationRun,
        cwd: Path,
        task_id: str | None,
    ) -> None:
        """Refuse any write invocation that is not inside this task's worktree."""

        if task_id is None:
            raise AutomationError("a write-enabled invocation must belong to a task")
        record = next((item for item in run.worktrees if item.task_id == task_id), None)
        if record is None:
            raise AutomationError(
                f"no isolated worktree is registered for {task_id}; refusing to "
                "run a write-enabled worker"
            )
        assert_isolated(record, canonical_repository=Path(run.project_path))
        if cwd.resolve() != Path(record.path).resolve():
            raise AutomationError(
                f"write-enabled worker would run in {cwd}, not its worktree "
                f"{record.path}"
            )
        assert_contained_symlinks(Path(record.path))

    def _assert_snapshot_isolation(
        self,
        run: AutomationRun,
        cwd: Path,
        task_id: str | None,
    ) -> None:
        """Refuse a snapshot-read invocation outside this task's own snapshot.

        The mirror of the write-isolation check, and for the same reason: a
        reader pointed at the researcher's checkout would be reading live,
        possibly uncommitted state instead of the pinned commit the run is
        reasoning about, and the before-and-after comparison that proves it
        changed nothing would be meaningless.
        """

        if task_id is None:
            raise AutomationError("a snapshot-read invocation must belong to a task")
        record = next((item for item in run.worktrees if item.task_id == task_id), None)
        if record is None:
            raise AutomationError(
                f"no isolated snapshot is registered for {task_id}; refusing to "
                "run a snapshot-read worker"
            )
        assert_isolated(record, canonical_repository=Path(run.project_path))
        if cwd.resolve() != Path(record.path).resolve():
            raise AutomationError(
                f"snapshot-read worker would run in {cwd}, not its snapshot "
                f"{record.path}"
            )
        assert_contained_symlinks(Path(record.path))

    @staticmethod
    def _assert_read_only_cwd(run: AutomationRun, cwd: Path) -> None:
        """Refuse to start a read-only worker inside the canonical repository.

        A read-only worker has no tools, so this is not what stops it writing.
        It is the second half of the same idea: a process with nothing to act
        with also has no reason to be standing in the researcher's checkout, and
        keeping it out means a future change to its tool set cannot silently
        inherit that position.
        """

        canonical = Path(run.project_path).resolve()
        resolved = cwd.resolve()
        if resolved == canonical or canonical in resolved.parents:
            raise AutomationError(
                f"a read-only worker would run in {resolved}, inside the "
                f"canonical repository {canonical}; read-only workers run from "
                "runtime-owned space and receive the project as context"
            )

    def _assert_symlinks_contained(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        worktree: Path,
        *,
        stage: str,
    ) -> None:
        """Fail the order unless every symlink in the worktree stays inside it."""

        escaping = outbound_symlinks(worktree)
        if escaping:
            self._fail_order_on_symlinks(store, run, task_id, escaping, stage=stage)

    def _fail_order_on_symlinks(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        escaping: tuple[str, ...],
        *,
        stage: str,
    ) -> NoReturn:
        """Record an outbound-symlink refusal and stop the order."""

        reason = (
            f"symlinks resolving outside the isolated worktree, detected {stage}: "
            + ", ".join(escaping)
        )
        store.append_event(
            "symlink_scope_violation",
            task_id=task_id,
            stage=stage,
            paths=list(escaping),
        )
        self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.FAILED,
            failure_reason=reason,
        )
        raise SymlinkScopeError(f"{task_id}: {reason}")

    def _transition(
        self,
        store: RunStore,
        run: AutomationRun,
        target: RunState,
        **fields: object,
    ) -> AutomationRun:
        assert_transition(run.state, target)
        updated = store.save(run.model_copy(update={"state": target}))
        store.append_event(
            "state_changed",
            previous=str(run.state),
            state=str(target),
            **fields,
        )
        return updated

    def _fail(
        self,
        store: RunStore,
        run: AutomationRun,
        reason: str,
    ) -> AutomationRun:
        if run.terminal:
            return run
        run = run.model_copy(
            update={"failure_reason": reason, "finished_at": utc_now()}
        )
        return self._transition(store, run, RunState.FAILED, reason=reason)

    def _update_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
        **fields: object,
    ) -> AutomationRun:
        orders = [
            item.model_copy(update=fields) if item.task_id == task_id else item
            for item in run.work_orders
        ]
        return store.save(run.model_copy(update={"work_orders": orders}))

    def _archive_context(
        self,
        store: RunStore,
        run: AutomationRun,
        packet: ContextPacket,
    ) -> AutomationRun:
        store.write_json("context/context.json", packet.model_dump(mode="json"))
        store.write_text("context/context.md", render_context(packet))
        store.append_event(
            "context_built",
            files=[item.path for item in packet.files],
            objects=len(packet.objects),
            capsule_present=packet.capsule_present,
        )
        return store.save(
            run.model_copy(
                update={
                    "project_id": packet.project_id,
                    "capsule_present": packet.capsule_present,
                }
            )
        )

    def _resolve_roles(self) -> ResolvedRoles:
        probes = probe_registry(self.providers)
        return resolve_roles(self.config, probes)

    @staticmethod
    def _worker_report(store: RunStore, order: WorkOrder) -> str | None:
        for invocation_id in reversed(order.invocation_ids):
            path = store.path("model_outputs", f"{invocation_id}.txt")
            if path.is_file():
                text = path.read_text(encoding="utf-8").strip()
                if text:
                    return text
        return None

    def _preflight(self, project_path: Path) -> _Preflight:
        from research_os.automation.gitutil import repository_root

        try:
            root = repository_root(project_path)
        except Exception as exc:
            raise PreflightError(
                f"{project_path} is not inside a Git repository: {exc}"
            ) from exc
        if not has_commits(root):
            raise PreflightError(
                f"{root} has no commits yet; an automation run needs a base "
                "commit to branch its worktrees from. Make an initial commit "
                "first."
            )
        status = porcelain_status(root)
        if status:
            raise PreflightError(
                f"{root} has uncommitted changes; an automation run must start "
                "from a clean tree so its worktrees have an unambiguous base. "
                "Commit or stash first."
            )
        return _Preflight(
            root=root,
            commit=head_commit(root),
            branch=current_branch(root),
        )


class _Preflight:
    """The deterministic facts a run is allowed to start from."""

    __slots__ = ("branch", "commit", "root")

    def __init__(self, *, root: Path, commit: str, branch: str | None) -> None:
        self.root = root
        self.commit = commit
        self.branch = branch


def ready_for_human_blockers(run: AutomationRun) -> list[str]:
    """Return every reason this run may not be declared READY_FOR_HUMAN.

    The single gate, evaluated from persisted state rather than from what the
    controller believes happened. A failed run has blockers by construction, so
    it can never reach the ready state.
    """

    blockers: list[str] = []
    if run.state is RunState.FAILED:
        blockers.append("the run has already failed")
    if run.state is RunState.CANCELLED:
        blockers.append("the run was cancelled")
    if not run.work_orders:
        blockers.append("the run has no work orders")
    reviewed = {outcome.task_id for outcome in run.reviews}
    for order in run.work_orders:
        if order.role is Role.ANALYST:
            # An analysis order changed nothing, so there is no diff to check
            # or review. What it must have is a validated, archived report.
            if order.status is not WorkOrderStatus.ANALYZED:
                blockers.append(f"{order.task_id} is {order.status}, not analyzed")
            if not order.analysis_path:
                blockers.append(f"{order.task_id} archived no validated analysis")
            continue
        if order.status is not WorkOrderStatus.REVIEWED:
            blockers.append(f"{order.task_id} is {order.status}, not reviewed")
        if not order.check_results:
            blockers.append(f"{order.task_id} ran no acceptance commands")
        elif not order.required_checks_passed:
            blockers.append(f"{order.task_id} has failing required checks")
        if order.task_id not in reviewed:
            blockers.append(f"{order.task_id} has no recorded review")
    for outcome in run.reviews:
        if outcome.verdict is ReviewVerdict.FAIL:
            blockers.append(f"{outcome.task_id} review verdict is FAIL")
    return blockers


def final_reviews(run: AutomationRun) -> list[ReviewOutcome]:
    """Return the last recorded review per task, in work-order order.

    A task that was repaired has more than one review. The one that describes
    the code a human is about to read is the last, so that is the one reported;
    the earlier verdicts stay in the ledger and in ``run.reviews``.
    """

    latest: dict[str, ReviewOutcome] = {}
    for outcome in run.reviews:
        latest[outcome.task_id] = outcome
    return list(latest.values())


def unresolved_findings(run: AutomationRun) -> list[tuple[str, str, str]]:
    """Return every reviewer finding a human still has to judge.

    Taken from the final review of each task. A finding the repair resolved is
    not something the human still has to judge, and a finding the reviewer
    repeated after the repair is.
    """

    return [
        (outcome.task_id, finding.severity, finding.message)
        for outcome in final_reviews(run)
        for finding in outcome.findings
    ]


def _failed_check_reason(failed: list[CommandResult]) -> str:
    return "required acceptance commands failed: " + ", ".join(
        item.display for item in failed
    )


def _read_captured(stored: str | None) -> str:
    """Return a captured stdout or stderr file, or empty when unavailable."""

    if not stored:
        return ""
    try:
        return Path(stored).read_text(encoding="utf-8")
    except OSError:
        return ""


def _elapsed_seconds(start: str, end: str) -> int:
    """Return whole seconds between two runtime timestamps.

    The runtime format has second resolution and is always UTC, so both sides
    are parsed as UTC rather than as naive local time.
    """

    try:
        first = datetime.strptime(start, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
        second = datetime.strptime(end, TIMESTAMP_FORMAT).replace(tzinfo=UTC)
    except ValueError:
        return 0
    return max(0, int((second - first).total_seconds()))
