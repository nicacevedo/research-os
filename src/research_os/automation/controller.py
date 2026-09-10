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

import time
from datetime import UTC, datetime
from pathlib import Path

from research_os.automation.checks import run_acceptance_command
from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.context import ContextPacket, build_context, render_context
from research_os.automation.executor import build_coder_prompt, collect_evidence
from research_os.automation.gitutil import (
    current_branch,
    has_commits,
    head_commit,
    porcelain_status,
)
from research_os.automation.models import (
    TIMESTAMP_FORMAT,
    AcceptanceCommand,
    AutomationRun,
    Budget,
    ModelInvocation,
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
)
from research_os.automation.store import RunStore, make_run_id
from research_os.automation.worktree import (
    assert_isolated,
    create_worktree,
    release_worktree,
)
from research_os.errors import (
    AutomationError,
    BudgetExceededError,
    PreflightError,
    ProviderInvocationError,
    ProviderUnavailableError,
)

PLANNER_TIMEOUT_SECONDS = 600
REVIEWER_TIMEOUT_SECONDS = 600
DEFAULT_WORK_ORDER_TIMEOUT_SECONDS = 1800

# One coder invocation plus one reviewer invocation per work order.
MODEL_CALLS_PER_WORK_ORDER = 2

WHITESPACE_CHECK = AcceptanceCommand(
    argv=["git", "diff", "--check", "HEAD"],
    description="controller-added: reject whitespace damage in the diff",
    required=True,
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

        required = MODEL_CALLS_PER_WORK_ORDER * len(run.work_orders)
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
            cwd=Path(run.project_path),
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
            timeout_seconds=DEFAULT_WORK_ORDER_TIMEOUT_SECONDS,
        )
        run = store.save(
            run.model_copy(update={"work_orders": orders, "plan_summary": plan.summary})
        )
        store.append_event(
            "plan_accepted",
            tasks=[order.task_id for order in orders],
            summary=plan.summary,
        )
        return self._transition(store, run, RunState.PLAN_READY)

    def _execute_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        order = run.order(task_id)
        blocked = [
            dependency
            for dependency in order.dependencies
            if run.order(dependency).status
            not in {WorkOrderStatus.EXECUTED, WorkOrderStatus.CHECKS_PASSED}
        ]
        if blocked:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.BLOCKED,
                failure_reason=f"unmet dependencies: {', '.join(blocked)}",
            )
            raise AutomationError(f"{task_id} is blocked by {', '.join(blocked)}")

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
        packet = build_context(project_path=worktree, goal=run.goal)
        prompt = build_coder_prompt(order, context_text=render_context(packet))
        setting = RoleSetting(
            provider=order.provider,
            model=order.model,
            effort=run.roles["coder"].effort if "coder" in run.roles else None,
            read_only=False,
            tools=list(run.roles["coder"].tools) if "coder" in run.roles else [],
        )
        run = self._update_order(
            store,
            run,
            task_id,
            status=WorkOrderStatus.RUNNING,
            worktree_path=record.path,
            branch=record.branch,
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

    def _check_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
        order = run.order(task_id)
        worktree = Path(order.worktree_path or "")
        commands = [*order.acceptance_commands, WHITESPACE_CHECK]
        results = []
        for index, command in enumerate(commands, start=1):
            result = run_acceptance_command(
                command,
                cwd=worktree,
                timeout_seconds=run.budget.max_command_timeout_seconds,
                stdout_path=store.path("checks", task_id, f"{index:02d}.stdout.txt"),
                stderr_path=store.path("checks", task_id, f"{index:02d}.stderr.txt"),
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
            )
        run = self._update_order(store, run, task_id, check_results=results)
        store.write_json(
            f"checks/{task_id}/results.json",
            [item.model_dump(mode="json") for item in results],
        )
        failed = [item for item in results if item.required and not item.ok]
        if failed:
            reason = "required acceptance commands failed: " + ", ".join(
                item.display for item in failed
            )
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.CHECKS_FAILED,
                failure_reason=reason,
            )
            raise AutomationError(f"{task_id}: {reason}")
        return self._update_order(
            store, run, task_id, status=WorkOrderStatus.CHECKS_PASSED
        )

    def _review_order(
        self,
        store: RunStore,
        run: AutomationRun,
        task_id: str,
    ) -> AutomationRun:
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
            cwd=Path(run.project_path),
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
        store.write_json(f"reviews/{task_id}.json", outcome.model_dump(mode="json"))
        run = store.save(run.model_copy(update={"reviews": [*run.reviews, outcome]}))
        store.append_event(
            "review_recorded",
            task_id=task_id,
            verdict=str(outcome.verdict),
            findings=len(outcome.findings),
            independence=str(outcome.independence),
        )
        if outcome.verdict is ReviewVerdict.FAIL:
            run = self._update_order(
                store,
                run,
                task_id,
                status=WorkOrderStatus.FAILED,
                failure_reason=f"independent review returned FAIL: {outcome.summary}",
            )
            raise AutomationError(f"{task_id}: review returned FAIL")
        return self._update_order(store, run, task_id, status=WorkOrderStatus.REVIEWED)

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
        if not setting.read_only:
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
    for order in run.work_orders:
        if order.status is not WorkOrderStatus.REVIEWED:
            blockers.append(f"{order.task_id} is {order.status}, not reviewed")
        if not order.check_results:
            blockers.append(f"{order.task_id} ran no acceptance commands")
        elif not order.required_checks_passed:
            blockers.append(f"{order.task_id} has failing required checks")
    reviewed = {outcome.task_id for outcome in run.reviews}
    for order in run.work_orders:
        if order.task_id not in reviewed:
            blockers.append(f"{order.task_id} has no recorded review")
    for outcome in run.reviews:
        if outcome.verdict is ReviewVerdict.FAIL:
            blockers.append(f"{outcome.task_id} review verdict is FAIL")
    return blockers


def unresolved_findings(run: AutomationRun) -> list[tuple[str, str, str]]:
    """Return every reviewer finding a human still has to judge."""

    return [
        (outcome.task_id, finding.severity, finding.message)
        for outcome in run.reviews
        for finding in outcome.findings
    ]


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
