"""The orchestrator: one goal, a typed plan, and the controllers that already exist.

This layer owns no worker. Every task kind is dispatched to a controller that was
built and tested on its own -- the automation controller for code and analysis,
the proposal controller for scientific reasoning, the experiment controller for
execution, the paper controller for prose -- and this module decides only what
runs, in what order, against which budget, and when to stop for a person.

That is what "build on it, do not duplicate it" means in practice. Scope
enforcement, worktree isolation, the outbound-symlink gate, the command policy,
the single bounded repair, and the claim discipline are not reimplemented here; a
code task becomes an automation run with a supplied plan and gets all of them.

Two things this layer does own.

**Failing closed on kinds.** Dispatch is a dictionary keyed by :class:`TaskKind`
with no default branch. A kind with no handler is refused at planning time, and
if one ever reached dispatch it would raise rather than be guessed at.

**Stopping.** The run stops for a human at every checkpoint the plan names, and
before anything it was not authorised to spend. It resumes only from states where
continuing is unambiguous.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.context import build_context, render_context
from research_os.automation.controller import (
    AutomationController,
    elapsed_seconds,
)
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import (
    Access,
    Budget,
    ModelInvocation,
    Role,
    RoleSetting,
    utc_now,
)
from research_os.automation.planner import (
    PlanDocument,
    PlannedCommand,
    PlannedRole,
    PlannedTask,
)
from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    probe_registry,
)
from research_os.errors import (
    AutomationError,
    BudgetExceededError,
    ExperimentError,
    InsightError,
    LiteratureError,
    PaperError,
    ProposalError,
    ProviderInvocationError,
    ProviderUnavailableError,
    ResearchError,
    ResearchPlanError,
    ResearchStateError,
)
from research_os.experiment.config import ExperimentConfig
from research_os.experiment.config import default_config as default_experiment_config
from research_os.experiment.controller import ExperimentBudget, ExperimentController
from research_os.experiment.models import ExecutorKind
from research_os.insights.commands import insights_for
from research_os.literature.config import LiteratureConfig
from research_os.literature.config import default_config as default_literature_config
from research_os.literature.service import LiteratureService
from research_os.literature.store import LiteratureStore
from research_os.paper.controller import PaperController
from research_os.paper.models import SectionKind
from research_os.paper.packet import build_source_packet
from research_os.proposal.context import build_science_context, render_science_context
from research_os.proposal.controller import ProposalController
from research_os.research.models import (
    INTERRUPTIBLE_STATES,
    SATISFIED_STATUSES,
    HumanCheckpoint,
    ResearchBudget,
    ResearchRun,
    ResearchState,
    ResearchTask,
    TaskKind,
    TaskStatus,
    allowed_transitions,
)
from research_os.research.planner import (
    PLAN_SCHEMA,
    ResearchPlan,
    build_plan_correction_prompt,
    build_research_plan_prompt,
    parse_research_plan,
    to_tasks,
    validate_research_plan,
)
from research_os.research.store import ResearchStore, make_research_run_id
from research_os.runlock import run_lock

PLANNER_TIMEOUT_SECONDS = 900

#: The largest model-call budget one delegated automation run may be given.
#:
#: The automation ``Budget`` caps this field at 100 itself; a research budget can
#: be larger, so the handoff is clamped rather than allowed to fail validation.
MAX_DELEGATED_MODEL_CALLS = 100

#: How many model calls each task kind costs at minimum.
#:
#: Used to refuse a plan the run could never finish, before it starts spending. A
#: literature task costs no model call at all; a code task costs a coder and a
#: reviewer; a paper task costs a writer and a reviewer; a proposal costs a
#: proposal worker and an assessor. A checkpoint costs nothing, which is part of
#: why checkpoints are cheap to insert.
MINIMUM_CALLS: dict[TaskKind, int] = {
    TaskKind.LITERATURE: 0,
    TaskKind.ANALYSIS: 1,
    TaskKind.PROPOSAL: 2,
    TaskKind.CODE: 2,
    TaskKind.EXPERIMENT: 0,
    TaskKind.PAPER: 2,
    TaskKind.HUMAN_CHECKPOINT: 0,
}

#: Errors a task may fail with without the failure being a bug in this layer.
#:
#: Caught so the run's own record says which task failed and why, then re-raised
#: unchanged: a research run must not turn another layer's refusal into a success.
WORKER_ERRORS = (
    AutomationError,
    ExperimentError,
    InsightError,
    LiteratureError,
    PaperError,
    ProposalError,
    ResearchError,
)

#: What every task handler looks like from the dispatcher's side.
Handler = Callable[["ResearchStore", "ResearchRun", str], "ResearchRun"]


@dataclass
class ResearchController:
    """Drives one research goal through a typed plan to a human handoff."""

    providers: dict[str, ProviderAdapter]
    config: AutomationConfig
    literature_config: LiteratureConfig = field(
        default_factory=default_literature_config
    )
    experiment_config: ExperimentConfig = field(
        default_factory=default_experiment_config
    )
    literature_store: LiteratureStore | None = None

    # -- starting --------------------------------------------------------

    def start(
        self,
        *,
        project_path: Path,
        goal: str,
        budget: ResearchBudget | None = None,
        execute_experiments: bool = False,
    ) -> tuple[ResearchStore, ResearchRun]:
        """Plan a research run. Never executes any of it."""

        goal = goal.strip()
        if not goal:
            raise ResearchPlanError("a research run needs a goal")
        root = repository_root(project_path)
        resolved = self._resolve_roles()
        science = build_science_context(root)
        created_at = utc_now()
        run = ResearchRun(
            run_id=make_research_run_id(
                project_path=str(root), goal=goal, created_at=created_at
            ),
            project_id=science.project_id,
            project_path=str(root),
            base_commit=head_commit(root) if has_commits(root) else None,
            goal=goal,
            created_at=created_at,
            updated_at=created_at,
            budget=budget or ResearchBudget(),
            execute_experiments=execute_experiments,
            independence=str(resolved.independence),
            independence_note=resolved.note,
        )
        store = ResearchStore.create(run)
        run = store.load()
        store.append_event(
            "run_created",
            project_path=str(root),
            project_id=science.project_id,
            goal=goal,
            base_commit=run.base_commit,
            execute_experiments=execute_experiments,
            independence=str(resolved.independence),
        )
        try:
            run = self._plan(store, run, science, resolved)
        except (ResearchError, AutomationError, ProposalError) as exc:
            self._fail(store, store.load(), str(exc))
            raise
        return store, run

    def _plan(
        self,
        store: ResearchStore,
        run: ResearchRun,
        science: object,
        resolved: ResolvedRoles,
    ) -> ResearchRun:
        run = self._transition(store, run, ResearchState.PLANNING)
        declared = self.experiment_config.for_project(run.project_id).commands
        repository = build_context(project_path=Path(run.project_path), goal=run.goal)
        prompt = build_research_plan_prompt(
            goal=run.goal,
            budget=run.budget,
            science_context=render_science_context(science),
            repository_context=render_context(repository),
            declared_experiments=sorted(declared),
            insight_section=insights_for(run.goal, project_id=run.project_id),
            execute_experiments=run.execute_experiments,
            allowed_programs=self.config.allowed_check_programs,
        )
        run, plan = self._planned(
            store,
            run,
            setting=resolved.roles["planner"],
            prompt=prompt,
            declared=frozenset(declared),
        )
        tasks = to_tasks(plan)
        self._assert_budget_could_finish(run, tasks)
        run = store.save(
            run.model_copy(update={"tasks": tasks, "plan_summary": plan.summary})
        )
        store.write_json("plan/plan.json", plan.model_dump(mode="json"))
        store.append_event(
            "plan_accepted",
            tasks=[item.task_id for item in tasks],
            kinds=[str(item.kind) for item in tasks],
            summary=plan.summary,
            minimum_model_calls=sum(MINIMUM_CALLS[item.kind] for item in tasks),
        )
        return self._transition(store, run, ResearchState.PLAN_READY)

    def _planned(
        self,
        store: ResearchStore,
        run: ResearchRun,
        *,
        setting: RoleSetting,
        prompt: str,
        declared: frozenset[str],
    ) -> tuple[ResearchRun, ResearchPlan]:
        """Get one validated plan, allowing at most one bounded re-ask.

        A planner can return something that satisfies the schema and is not a
        plan -- a provider's structured-output retry loop converges on the
        smallest valid object when a rich answer keeps missing the schema, and
        what arrives is a run's worth of placeholders. The validators catch it;
        this asks once more with the exact reason, because the alternative is a
        failed run the researcher has to restart by hand.

        One re-ask, charged against the same allowance everything else uses. A
        second failure ends the run.
        """

        attempt = prompt
        for correction in (False, True):
            run, invocation, result = self._invoke(
                store,
                run,
                role=Role.PLANNER,
                setting=setting,
                prompt=attempt,
                timeout_seconds=PLANNER_TIMEOUT_SECONDS,
                json_schema=PLAN_SCHEMA,
            )
            if not result.ok:
                raise ProviderInvocationError(
                    f"the research planner failed: "
                    f"{invocation.error or 'unknown error'}"
                )
            try:
                plan = parse_research_plan(
                    structured=result.structured, text=result.text
                )
                validate_research_plan(
                    plan, budget=run.budget, declared_experiments=declared
                )
            except ResearchPlanError as exc:
                store.append_event(
                    "plan_rejected",
                    invocation_id=invocation.invocation_id,
                    detail=str(exc),
                    corrected=correction,
                )
                if correction or run.budget.max_repair_attempts < 1:
                    raise
                if run.remaining_model_calls < 1:
                    raise
                attempt = build_plan_correction_prompt(prompt, reason=str(exc))
                store.append_event("plan_correction_started")
                continue
            if correction:
                store.append_event("plan_correction_accepted")
            return run, plan
        raise ResearchPlanError("the research planner produced no usable plan")

    @staticmethod
    def _assert_budget_could_finish(
        run: ResearchRun, tasks: list[ResearchTask]
    ) -> None:
        """Refuse a plan this run could never pay for.

        Checked before execution rather than discovered at the last task,
        because a run that stops half way has spent everything and produced a
        partial result nobody asked for.
        """

        required = sum(MINIMUM_CALLS[item.kind] for item in tasks)
        remaining = run.remaining_model_calls
        if required > remaining:
            raise ResearchPlanError(
                f"this plan needs at least {required} model calls and only "
                f"{remaining} of {run.budget.max_model_calls} remain. Either raise "
                "the budget or ask for less."
            )

    # -- executing -------------------------------------------------------

    def execute(self, store: ResearchStore) -> ResearchRun:
        """Run the plan until it finishes, stops for a human, or fails."""

        with run_lock(store.run_id, action="research run"):
            return self._execute(store)

    def _execute(self, store: ResearchStore) -> ResearchRun:
        run = store.load()
        if run.terminal:
            raise ResearchStateError(f"{run.run_id} is already {run.state}")
        if run.state in INTERRUPTIBLE_STATES:
            raise ResearchStateError(
                f"{run.run_id} is {run.state}, which means a process stopped "
                "while it was working. Recover it with 'researchctl research "
                f"resume {run.run_id}' -- that makes you decide what happens to "
                "the task that was in flight."
            )
        if not run.resumable:
            raise ResearchStateError(
                f"{run.run_id} is {run.state}; only a planned run, one waiting "
                "for a human, or a recovered one can be executed"
            )
        pending = run.pending_checkpoints
        if pending:
            raise ResearchStateError(
                f"{run.run_id} is waiting for you to answer {pending[0].task_id}: "
                f"{pending[0].question} Answer it with 'researchctl research answer'."
            )
        run = self._transition(store, run, ResearchState.EXECUTING)
        started = utc_now()
        try:
            for planned in list(run.tasks):
                task = run.task(planned.task_id)
                if task.terminal:
                    continue
                self._assert_time_remains(store, run, started, task.task_id)
                run = self._dispatch(store, run, task.task_id)
                if run.state is ResearchState.WAITING_FOR_HUMAN:
                    return run
        except WORKER_ERRORS as exc:
            current = store.load()
            if not current.terminal:
                self._fail(store, current, str(exc))
            raise
        return self._finish(store, run)

    def dispatch_table(self) -> dict[TaskKind, Handler]:
        """Return the one mapping from task kind to the worker that runs it.

        A method rather than a literal buried in ``_dispatch`` so a test can
        assert it covers :class:`TaskKind` exactly. Adding a kind without wiring
        it up should fail in the test suite, not in somebody's run.
        """

        return {
            TaskKind.LITERATURE: self._run_literature,
            TaskKind.ANALYSIS: self._run_analysis,
            TaskKind.PROPOSAL: self._run_proposal,
            TaskKind.CODE: self._run_code,
            TaskKind.EXPERIMENT: self._run_experiment,
            TaskKind.PAPER: self._run_paper,
            TaskKind.HUMAN_CHECKPOINT: self._reach_checkpoint,
        }

    def _dispatch(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Run one task through the controller that owns its kind.

        The table has no default branch. A kind with no handler cannot reach here
        -- the plan validator refused it -- and if one ever did, the lookup fails
        rather than guessing at what was meant.
        """

        handlers = self.dispatch_table()
        run = self._assert_dependencies_met(store, run, task_id)
        task = run.task(task_id)
        handler = handlers.get(task.kind)
        if handler is None:
            raise ResearchPlanError(
                f"{task_id} has kind {task.kind!r}, which this controller has no "
                "handler for. A research run dispatches only kinds it knows."
            )
        run = self._update(
            store, run, task_id, status=TaskStatus.RUNNING, started_at=utc_now()
        )
        store.append_event("task_started", task_id=task_id, kind=str(task.kind))
        try:
            return handler(store, run, task_id)
        except WORKER_ERRORS as exc:
            self._update(
                store,
                store.load(),
                task_id,
                status=TaskStatus.FAILED,
                finished_at=utc_now(),
                failure_reason=str(exc),
            )
            store.append_event("task_failed", task_id=task_id, reason=str(exc))
            raise

    def _assert_dependencies_met(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        task = run.task(task_id)
        blocked = [
            item
            for item in task.depends_on
            if run.task(item).status not in SATISFIED_STATUSES
        ]
        if not blocked:
            return run
        detail = ", ".join(f"{item} is {run.task(item).status}" for item in blocked)
        run = self._update(
            store,
            run,
            task_id,
            status=TaskStatus.BLOCKED,
            finished_at=utc_now(),
            failure_reason=f"unmet dependencies: {detail}",
        )
        store.append_event("dependency_unmet", task_id=task_id, detail=detail)
        raise ResearchPlanError(f"{task_id} is blocked: {detail}")

    # -- handlers --------------------------------------------------------

    def _run_literature(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Retrieve and index published work. Deterministic; costs no model call."""

        task = run.task(task_id)
        owned = self.literature_store is None
        literature = self.literature_store or LiteratureStore.open()
        try:
            service = LiteratureService(store=literature, config=self.literature_config)
            report = service.retrieve(task.query)
        finally:
            if owned:
                literature.close()
        keys = list(report.work_keys)
        store.append_event(
            "literature_retrieved",
            task_id=task_id,
            query=task.query,
            reached=list(report.reached),
            skipped=[item.provider for item in report.skipped],
            works=len(keys),
            complete=report.complete,
        )
        detail = (
            f"retrieved {len(keys)} work(s) from "
            f"{', '.join(report.reached) or 'no provider'}"
        )
        if report.skipped:
            detail += "; not reached: " + ", ".join(
                item.provider for item in report.skipped
            )
        return self._finish_task(
            store, run, task_id, detail=detail, literature_keys=keys[:20]
        )

    def _run_analysis(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """A read-only investigation, dispatched as an automation run."""

        task = run.task(task_id)
        plan = PlanDocument(
            summary=task.goal,
            tasks=[
                PlannedTask(
                    id="T-001",
                    title=task.title,
                    goal=task.goal,
                    role=PlannedRole.ANALYST,
                    read_only=True,
                    read_paths=list(task.read_paths),
                    completion_condition=(
                        "structured findings about the paths named in the plan"
                    ),
                )
            ],
        )
        return self._delegate_to_automation(store, run, task_id, plan)

    def _run_code(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """A write-enabled implementation, dispatched as an automation run.

        Delegated rather than reimplemented, so this task gets the worktree
        isolation, the scope enforcement from the observed diff, the command
        policy, the acceptance checks, the independent review, and the single
        bounded repair exactly as the automation controller provides them.
        """

        task = run.task(task_id)
        run = self._spend_write_task(store, run, task_id)
        plan = PlanDocument(
            summary=task.goal,
            tasks=[
                PlannedTask(
                    id="T-001",
                    title=task.title,
                    goal=task.goal,
                    role=PlannedRole.CODER,
                    read_only=False,
                    allowed_paths=list(task.allowed_paths),
                    acceptance_commands=[
                        PlannedCommand(argv=list(item), description="from the plan")
                        for item in task.acceptance_commands
                    ],
                    completion_condition="the acceptance commands pass",
                )
            ],
        )
        return self._delegate_to_automation(store, run, task_id, plan)

    def _delegate_to_automation(
        self,
        store: ResearchStore,
        run: ResearchRun,
        task_id: str,
        plan: PlanDocument,
    ) -> ResearchRun:
        """Hand one task to the automation controller and account for its spend.

        The inner run's model calls are charged to this run whether it succeeded
        or not, because a failed run spent them just the same. That is what the
        ``finally`` is for: a budget that only counts successes is not a budget.
        """

        task = run.task(task_id)
        writes = task.kind is TaskKind.CODE
        controller = AutomationController(providers=self.providers, config=self.config)
        budget = Budget(
            max_model_calls=min(
                MAX_DELEGATED_MODEL_CALLS, max(2, run.remaining_model_calls)
            ),
            max_write_work_orders=1 if writes else 0,
            max_work_orders=1,
            max_repair_attempts=run.budget.max_repair_attempts,
        )
        inner_store, inner = controller.start(
            project_path=Path(run.project_path),
            goal=task.goal,
            budget=budget,
            plan=plan,
        )
        store.append_event(
            "automation_run_started",
            task_id=task_id,
            automation_run_id=inner.run_id,
            directory=str(inner_store.directory),
            writes=writes,
        )
        try:
            inner = controller.execute(inner_store)
        finally:
            spent = inner_store.load().model_calls_used
            run = self._charge(store, run, spent)
            store.append_event(
                "automation_run_finished",
                task_id=task_id,
                automation_run_id=inner.run_id,
                state=str(inner_store.load().state),
                model_calls=spent,
            )
        detail = f"automation run {inner.run_id} finished {inner.state}"
        order = inner.work_orders[0] if inner.work_orders else None
        if order is not None and order.branch:
            detail += f"; branch {order.branch}"
        return self._finish_task(
            store,
            run,
            task_id,
            detail=detail,
            artifact_id=inner.run_id,
            model_calls=inner.model_calls_used,
        )

    def _run_proposal(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Turn what is known into structured proposals. Writes no capsule object."""

        task = run.task(task_id)
        self._assert_calls_remain(run, MINIMUM_CALLS[TaskKind.PROPOSAL])
        with_literature = bool(task.literature_keys) or any(
            run.task(item).kind is TaskKind.LITERATURE for item in task.depends_on
        )
        controller = ProposalController(
            providers=self.providers,
            config=self.config,
            literature_config=self.literature_config,
            literature_store=self.literature_store,
        )
        outcome = controller.propose(
            project_path=Path(run.project_path),
            goal=task.goal,
            with_literature=with_literature,
            retrieve=False,
        )
        run = self._charge(store, run, outcome.model_calls)
        verdict = str(outcome.assessment.verdict) if outcome.assessment else None
        store.append_event(
            "proposal_produced",
            task_id=task_id,
            proposal_id=outcome.proposal.proposal_id,
            items=len(outcome.proposal.items),
            verdict=verdict,
        )
        suffix = f"; assessment {verdict}" if verdict else ""
        return self._finish_task(
            store,
            run,
            task_id,
            detail=f"{len(outcome.proposal.items)} proposed item(s){suffix}",
            artifact_id=outcome.proposal.proposal_id,
            model_calls=outcome.model_calls,
        )

    def _run_experiment(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Run a declared experiment, or stop and say exactly what would have run."""

        task = run.task(task_id)
        controller = ExperimentController(
            config=self.experiment_config,
            budget=ExperimentBudget(
                local_runs=run.experiments_used,
                submissions=run.cluster_submissions_used,
            ),
        )
        # Resolve first, always. Knowing the exact argv and the executor before
        # deciding anything is what lets an unauthorised run report what it
        # would have done, and what lets the cluster budget be checked before a
        # job is submitted rather than after.
        command = controller.preview(
            project_id=run.project_id,
            task_name=task.experiment_task,
            parameters=dict(task.experiment_parameters),
        )
        if not run.execute_experiments:
            store.append_event(
                "experiment_previewed",
                task_id=task_id,
                command=list(command.argv),
                executor=str(command.executor),
            )
            return self._finish_task(
                store,
                run,
                task_id,
                status=TaskStatus.SKIPPED,
                detail=(
                    "not executed: this run was not authorised to spend compute. "
                    f"It would have run: {command.display}"
                ),
            )
        self._assert_compute_remains(run, task_id, command.executor)
        # No worktree argument: the experiment controller creates an isolated
        # one. Passing the researcher's checkout here is what an independent
        # reviewer found, and it put an experiment's cwd inside canonical
        # science.
        experiment_store, record, packet = controller.run(
            project_path=Path(run.project_path),
            task_name=task.experiment_task,
            parameters=dict(task.experiment_parameters),
            project_id=run.project_id,
            execute=True,
        )
        run = store.save(
            run.model_copy(
                update={
                    "experiments_used": controller.budget.local_runs,
                    "cluster_submissions_used": controller.budget.submissions,
                }
            )
        )
        store.append_event(
            "experiment_executed",
            task_id=task_id,
            experiment_run_id=record.run_id,
            state=str(record.state),
            usable=packet.usable if packet else None,
            directory=str(experiment_store.directory),
        )
        if packet is None:
            outcome = "it has not finished, so there is no candidate packet yet"
        elif packet.usable:
            outcome = "it produced a usable candidate evidence packet"
        else:
            outcome = "it did not produce a usable result"
        return self._finish_task(
            store,
            run,
            task_id,
            detail=f"{record.state}; {outcome}",
            artifact_id=record.run_id,
        )

    def _run_paper(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Draft one manuscript section from science a human already accepted."""

        task = run.task(task_id)
        self._assert_calls_remain(run, MINIMUM_CALLS[TaskKind.PAPER])
        # Assembled before the write task is charged. A packet that cannot be
        # built is not a writing attempt, and a run should not lose an
        # allowance to a project that has nothing accepted to write about.
        packet = build_source_packet(
            Path(run.project_path),
            claim_ids=task.claim_ids or None,
            literature_keys=task.literature_keys or None,
            literature_store=self.literature_store,
        )
        run = self._spend_write_task(store, run, task_id)
        controller = PaperController(providers=self.providers, config=self.config)
        outcome = controller.write(
            project_path=Path(run.project_path),
            section=SectionKind(task.section),
            instruction=task.goal,
            packet=packet,
            allowed_paths=list(task.allowed_paths),
        )
        run = self._charge(store, run, outcome.model_calls)
        store.append_event(
            "draft_produced",
            task_id=task_id,
            draft_id=outcome.draft.draft_id,
            grounded=(
                outcome.draft.grounding.grounded if outcome.draft.grounding else None
            ),
            ready_for_human=outcome.ready_for_human,
        )
        state = (
            "grounded and reviewed"
            if outcome.ready_for_human
            else "not ready: see the draft's findings"
        )
        return self._finish_task(
            store,
            run,
            task_id,
            detail=f"draft {outcome.draft.draft_id} is {state}",
            artifact_id=outcome.draft.draft_id,
            model_calls=outcome.model_calls,
        )

    def _reach_checkpoint(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Stop and wait. Nothing after this runs until a person answers."""

        task = run.task(task_id)
        checkpoint = HumanCheckpoint(task_id=task_id, question=task.question)
        run = store.save(
            run.model_copy(update={"checkpoints": [*run.checkpoints, checkpoint]})
        )
        run = self._update(
            store,
            run,
            task_id,
            status=TaskStatus.AWAITING_HUMAN,
            detail="waiting for the researcher",
        )
        store.append_event(
            "human_checkpoint_reached", task_id=task_id, question=task.question
        )
        return self._transition(store, run, ResearchState.WAITING_FOR_HUMAN)

    # -- the human's turn ------------------------------------------------

    def answer(
        self, store: ResearchStore, *, answer: str, proceed: bool = True
    ) -> ResearchRun:
        """Record a person's answer to the checkpoint this run stopped at.

        ``proceed`` is the decision, not a formality. A researcher who answers
        "no, stop" has decided something the run must honour, so the run ends
        rather than continuing past the thing they declined.
        """

        answer = answer.strip()
        if not answer:
            raise ResearchStateError("a checkpoint answer needs at least one character")
        with run_lock(store.run_id, action="research answer"):
            return self._answer(store, answer=answer, proceed=proceed)

    def _answer(
        self, store: ResearchStore, *, answer: str, proceed: bool
    ) -> ResearchRun:
        run = store.load()
        pending = run.pending_checkpoints
        if not pending:
            raise ResearchStateError(f"{run.run_id} is not waiting for an answer")
        checkpoint = pending[0]
        answered = checkpoint.model_copy(
            update={
                "answer": answer,
                "answered_at": utc_now(),
                "decision": "proceed" if proceed else "stop",
            }
        )
        run = store.save(
            run.model_copy(
                update={
                    "checkpoints": [
                        answered if item.task_id == checkpoint.task_id else item
                        for item in run.checkpoints
                    ]
                }
            )
        )
        run = self._update(
            store,
            run,
            checkpoint.task_id,
            status=TaskStatus.DONE if proceed else TaskStatus.SKIPPED,
            finished_at=utc_now(),
            detail=f"answered: {answer}",
        )
        store.append_event(
            "human_checkpoint_answered",
            task_id=checkpoint.task_id,
            decision=answered.decision,
        )
        if proceed:
            return run
        run = store.save(
            run.model_copy(
                update={
                    "failure_reason": (
                        f"the researcher declined to continue at "
                        f"{checkpoint.task_id}: {answer}"
                    ),
                    "finished_at": utc_now(),
                }
            )
        )
        return self._transition(store, run, ResearchState.CANCELLED)

    # -- recovery --------------------------------------------------------

    def resume(
        self, store: ResearchStore, *, retry: bool = False, force: bool = False
    ) -> ResearchRun:
        """Recover a run whose process stopped while it was working.

        A crashed run's file still says ``EXECUTING`` and nothing will ever move
        it. This is the only thing that can, and it deliberately makes a person
        choose what happens to the task that was in flight, because the
        controller cannot tell from the outside whether that task spent
        anything. A worktree may exist, a cluster job may have been submitted, a
        provider may have been invoked and charged.

        The default is to mark the interrupted task failed and continue with
        whatever does not depend on it -- the conservative reading, in which
        nothing is repeated. ``retry`` re-runs it instead, and is refused for an
        experiment without ``force``, because re-running one is exactly the
        mistake that costs real money.
        """

        with run_lock(store.run_id, action="research resume"):
            return self._resume(store, retry=retry, force=force)

    def _resume(self, store: ResearchStore, *, retry: bool, force: bool) -> ResearchRun:
        run = store.load()
        if run.state not in INTERRUPTIBLE_STATES:
            raise ResearchStateError(
                f"{run.run_id} is {run.state}, which is not an interrupted run. "
                "Resume recovers a run whose process stopped while it was "
                "planning or executing."
            )
        if run.state is ResearchState.PLANNING:
            return self._fail(
                store,
                run,
                "the process stopped while this run was still planning. Planning "
                "is a single model call and produced nothing to salvage; start a "
                "new run.",
            )

        in_flight = [item for item in run.tasks if item.status is TaskStatus.RUNNING]
        for task in in_flight:
            if retry and task.kind is TaskKind.EXPERIMENT and not force:
                raise ResearchStateError(
                    f"{task.task_id} is an experiment that was interrupted. It may "
                    "already have run or been submitted, so retrying it could "
                    "spend the same compute twice. Check "
                    "'researchctl experiment runs' first, then pass --force if "
                    "you are sure, or resume without --retry to mark it failed."
                )
            if retry:
                # Charge whatever the interrupted attempt already spent before
                # queueing another. A killed process never reached the `finally`
                # that accounts for a delegated run, so without this a
                # kill-and-retry loop spends real money against a budget that
                # does not notice. The ledger knows which runs were started.
                run = self._reconcile_delegated_spend(store, run, task.task_id)
                run = self._update(
                    store,
                    run,
                    task.task_id,
                    status=TaskStatus.PENDING,
                    started_at=None,
                    finished_at=None,
                    failure_reason=None,
                    detail="retried after an interruption",
                )
            else:
                run = self._update(
                    store,
                    run,
                    task.task_id,
                    status=TaskStatus.FAILED,
                    finished_at=utc_now(),
                    failure_reason=(
                        "the process stopped while this task was running; it was "
                        "not retried, because it may already have spent something"
                    ),
                )
            store.append_event(
                "task_recovered",
                task_id=task.task_id,
                kind=str(task.kind),
                decision="retry" if retry else "fail",
                forced=bool(retry and force),
            )
        run = self._transition(
            store,
            run,
            ResearchState.INTERRUPTED,
            in_flight=[item.task_id for item in in_flight],
            decision="retry" if retry else "fail",
        )
        store.append_event("run_resumable", tasks_in_flight=len(in_flight))
        return run

    def _reconcile_delegated_spend(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Charge model calls an interrupted delegated run made but never reported.

        ``_delegate_to_automation`` charges in a ``finally``, which a killed
        process never runs. The event ledger recorded the automation run id when
        it started, so the spend is recoverable from disk: read each one, and
        charge the difference between what it used and what this run was told
        about.
        """

        from research_os.automation.store import RunStore
        from research_os.errors import AutomationError

        already = run.task(task_id).model_calls
        spent = 0
        for record in store.iter_events():
            if record.get("event") != "automation_run_started":
                continue
            if record.get("task_id") != task_id:
                continue
            inner_id = str(record.get("automation_run_id", ""))
            try:
                spent += RunStore.open(inner_id).load().model_calls_used
            except (AutomationError, OSError):
                continue
        owed = spent - already
        if owed <= 0:
            return store.load()
        store.append_event(
            "delegated_spend_reconciled",
            task_id=task_id,
            model_calls=owed,
            previously_recorded=already,
        )
        return self._charge(store, run, owed)

    # -- finishing -------------------------------------------------------

    def _finish(self, store: ResearchStore, run: ResearchRun) -> ResearchRun:
        run = store.load()
        blockers = ready_blockers(run)
        if blockers:
            return self._fail(
                store, run, "the run cannot finish: " + "; ".join(blockers)
            )
        run = store.save(run.model_copy(update={"finished_at": utc_now()}))
        run = self._transition(store, run, ResearchState.READY_FOR_HUMAN)
        store.append_event(
            "ready_for_human",
            tasks=[item.task_id for item in run.tasks],
            model_calls=run.model_calls_used,
        )
        return run

    def cancel(self, store: ResearchStore, *, reason: str) -> ResearchRun:
        """Stop a run that has not finished.

        Takes the same lock the executing process holds, so a cancel either
        happens between tasks or is refused outright. It must never be accepted
        and then silently overwritten by a run that carried on regardless.
        """

        with run_lock(store.run_id, action="research cancel"):
            return self._cancel(store, reason=reason)

    def _cancel(self, store: ResearchStore, *, reason: str) -> ResearchRun:
        run = store.load()
        if run.terminal:
            raise ResearchStateError(f"{run.run_id} is already {run.state}")
        run = store.save(
            run.model_copy(update={"failure_reason": reason, "finished_at": utc_now()})
        )
        return self._transition(store, run, ResearchState.CANCELLED, reason=reason)

    # -- primitives ------------------------------------------------------

    def _spend_write_task(
        self, store: ResearchStore, run: ResearchRun, task_id: str
    ) -> ResearchRun:
        """Charge one write task against the budget before anything is written."""

        if run.write_tasks_used >= run.budget.max_write_tasks:
            raise BudgetExceededError(
                f"{task_id} would be write task number {run.write_tasks_used + 1} "
                f"and this run allows {run.budget.max_write_tasks}"
            )
        return store.save(
            run.model_copy(update={"write_tasks_used": run.write_tasks_used + 1})
        )

    @staticmethod
    def _assert_compute_remains(
        run: ResearchRun, task_id: str, executor: ExecutorKind
    ) -> None:
        """Refuse an execution this run cannot pay for, before it is started.

        Checked against the executor the command actually resolved to, because
        a local run and a cluster submission are different resources with
        different ceilings -- and because a cluster budget enforced after
        ``sbatch`` returned would be a report, not a limit.
        """

        if run.experiments_used >= run.budget.max_experiments:
            raise BudgetExceededError(
                f"{task_id} would be experiment number {run.experiments_used + 1} "
                f"and this run allows {run.budget.max_experiments}"
            )
        if executor is ExecutorKind.LOCAL:
            return
        if run.cluster_submissions_used >= run.budget.max_cluster_submissions:
            raise BudgetExceededError(
                f"{task_id} would be cluster submission number "
                f"{run.cluster_submissions_used + 1} and this run allows "
                f"{run.budget.max_cluster_submissions}. Nothing was submitted."
            )

    def _charge(
        self, store: ResearchStore, run: ResearchRun, calls: int
    ) -> ResearchRun:
        """Record model calls a delegated controller spent on this run's behalf.

        Clamped to the budget rather than allowed to overflow it, because the
        run model refuses a count above its own budget and a delegated
        controller that overspent should leave a run at its ceiling rather than
        an unloadable file.
        """

        if calls <= 0:
            return store.load()
        current = store.load()
        used = min(current.budget.max_model_calls, current.model_calls_used + calls)
        return store.save(current.model_copy(update={"model_calls_used": used}))

    def _finish_task(
        self,
        store: ResearchStore,
        run: ResearchRun,
        task_id: str,
        *,
        detail: str,
        status: TaskStatus = TaskStatus.DONE,
        artifact_id: str | None = None,
        model_calls: int = 0,
        literature_keys: list[str] | None = None,
    ) -> ResearchRun:
        updates: dict[str, object] = {
            "status": status,
            "finished_at": utc_now(),
            "detail": detail,
            "model_calls": model_calls,
        }
        if artifact_id is not None:
            updates["artifact_id"] = artifact_id
        if literature_keys is not None:
            updates["literature_keys"] = literature_keys
        run = self._update(store, run, task_id, **updates)
        store.append_event(
            "task_finished",
            task_id=task_id,
            status=str(status),
            detail=detail,
            artifact_id=artifact_id,
            model_calls=model_calls,
        )
        return run

    @staticmethod
    def _assert_time_remains(
        store: ResearchStore, run: ResearchRun, started: str, task_id: str
    ) -> None:
        """Refuse to start another task once this pass has run out of time.

        A bound on the loop, not a timeout on the work. Killing a task mid-flight
        is how a run ends up holding a worktree nobody knows about or a cluster
        job nobody is watching; declining to start the next one leaves every
        artifact complete and the record honest about why it stopped.
        """

        limit = run.budget.max_wall_clock_seconds
        if limit is None:
            return
        elapsed = elapsed_seconds(started, utc_now())
        if elapsed <= limit:
            return
        store.append_event(
            "wall_clock_exhausted",
            task_id=task_id,
            elapsed_seconds=elapsed,
            limit_seconds=limit,
        )
        raise BudgetExceededError(
            f"this pass has been running for {elapsed}s and its wall-clock "
            f"budget is {limit}s, so {task_id} was not started. Everything that "
            "did run is complete and recorded."
        )

    @staticmethod
    def _assert_calls_remain(run: ResearchRun, needed: int) -> None:
        if run.remaining_model_calls < needed:
            raise BudgetExceededError(
                f"this task needs {needed} model call(s) and only "
                f"{run.remaining_model_calls} of {run.budget.max_model_calls} remain"
            )

    def _invoke(
        self,
        store: ResearchStore,
        run: ResearchRun,
        *,
        role: Role,
        setting: RoleSetting,
        prompt: str,
        timeout_seconds: int,
        json_schema: dict | None,
    ) -> tuple[ResearchRun, ModelInvocation, InvocationResult]:
        """Spend one model call from this run's own budget, and record it."""

        self._assert_calls_remain(run, 1)
        adapter = self.providers.get(setting.provider)
        if adapter is None:
            raise ProviderUnavailableError(
                f"no adapter for provider {setting.provider!r}"
            )
        if setting.access is not Access.CONTEXT_ONLY:
            raise ResearchStateError(
                f"the {role} role of a research run declares {setting.access}; the "
                "research planner is context-only and is given no tools"
            )
        invocation_id = f"INV-{run.model_calls_used + 1:04d}"
        cwd = store.path("context")
        cwd.mkdir(parents=True, exist_ok=True)
        store.write_text(f"prompts/{invocation_id}.txt", prompt)
        started = utc_now()
        monotonic = time.monotonic()
        result = adapter.invoke(
            InvocationRequest(
                role=role,
                prompt=prompt,
                cwd=cwd,
                read_only=True,
                timeout_seconds=timeout_seconds,
                model=setting.model,
                effort=setting.effort,
                access=Access.CONTEXT_ONLY,
                tools=(),
                json_schema=json_schema,
            )
        )
        invocation = ModelInvocation(
            invocation_id=invocation_id,
            run_id=run.run_id,
            role=role,
            provider=setting.provider,
            model=result.resolved_model or setting.model,
            effort=setting.effort,
            read_only=True,
            cwd=str(cwd),
            started_at=started,
            ended_at=utc_now(),
            duration_ms=int((time.monotonic() - monotonic) * 1000),
            timeout_seconds=timeout_seconds,
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_cost_usd=result.total_cost_usd,
            error=result.error,
        )
        store.write_text(f"model_outputs/{invocation_id}.txt", result.text or "")
        store.write_json(
            f"invocations/{invocation_id}.json", invocation.model_dump(mode="json")
        )
        run = self._charge(store, run, 1)
        store.append_event(
            "provider_invoked",
            invocation_id=invocation_id,
            role=str(role),
            provider=setting.provider,
            model=invocation.model,
            error=result.error,
            model_calls_used=run.model_calls_used,
            model_call_budget=run.budget.max_model_calls,
        )
        return run, invocation, result

    def _resolve_roles(self) -> ResolvedRoles:
        return resolve_roles(self.config, probe_registry(self.providers))

    def _transition(
        self,
        store: ResearchStore,
        run: ResearchRun,
        target: ResearchState,
        **fields: object,
    ) -> ResearchRun:
        if target not in allowed_transitions(run.state):
            raise ResearchStateError(
                f"invalid research run state transition {run.state} -> {target}"
            )
        updated = store.save(run.model_copy(update={"state": target}))
        store.append_event(
            "state_changed", previous=str(run.state), state=str(target), **fields
        )
        return updated

    def _fail(self, store: ResearchStore, run: ResearchRun, reason: str) -> ResearchRun:
        if run.terminal:
            return run
        run = store.save(
            run.model_copy(update={"failure_reason": reason, "finished_at": utc_now()})
        )
        return self._transition(store, run, ResearchState.FAILED, reason=reason)

    def _update(
        self,
        store: ResearchStore,
        run: ResearchRun,
        task_id: str,
        **fields: object,
    ) -> ResearchRun:
        tasks = [
            item.model_copy(update=fields) if item.task_id == task_id else item
            for item in run.tasks
        ]
        return store.save(run.model_copy(update={"tasks": tasks}))


def ready_blockers(run: ResearchRun) -> list[str]:
    """Return every reason this run may not be declared READY_FOR_HUMAN.

    Evaluated from persisted state rather than from what the controller believes
    happened, exactly as the automation controller's gate is. A run reaches a
    researcher's desk only when the file on disk says every task it planned
    actually finished.
    """

    blockers: list[str] = []
    if run.state is ResearchState.FAILED:
        blockers.append("the run has already failed")
    if run.state is ResearchState.CANCELLED:
        blockers.append("the run was cancelled")
    if not run.tasks:
        blockers.append("the run has no tasks")
    for task in run.tasks:
        if task.status is TaskStatus.FAILED:
            blockers.append(f"{task.task_id} failed: {task.failure_reason}")
        elif task.status is TaskStatus.BLOCKED:
            blockers.append(f"{task.task_id} was blocked")
        elif task.status is TaskStatus.AWAITING_HUMAN:
            blockers.append(f"{task.task_id} is waiting for the researcher")
        elif task.status in {TaskStatus.PENDING, TaskStatus.RUNNING}:
            blockers.append(f"{task.task_id} is {task.status}, not finished")
    for checkpoint in run.checkpoints:
        if checkpoint.pending:
            blockers.append(f"{checkpoint.task_id} has no answer")
    return blockers
