"""The deterministic path from a research goal to an assessed proposal.

Ordinary Python, like every other controller here. It reads the project's
scientific state from the capsule, optionally retrieves and analyses literature,
invokes one bounded proposal worker and one bounded assessor, validates both,
and archives everything. Nothing it produces is science; the boundary to science
is a separate, human-only command.

It is also the read-only controller. It creates no worktree, dispatches no
write-enabled worker, runs no command, and never touches a file inside the
project -- so unlike an automation run it does not require a clean tree, and it
cannot leave one dirty. A researcher can point it at live work in progress.

Budgets are real and small. Two model calls is the whole ordinary cost: one
proposal, one assessment. A literature pass adds one more.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import (
    Access,
    Independence,
    ModelInvocation,
    Role,
    RoleSetting,
    utc_now,
)
from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    probe_registry,
)
from research_os.errors import (
    AutomationError,
    ProposalValidationError,
    ProviderInvocationError,
    ProviderUnavailableError,
)
from research_os.literature.analyst import (
    LITERATURE_SCHEMA,
    build_literature_prompt,
    parse_literature_report,
    render_literature_data,
)
from research_os.literature.config import LiteratureConfig, default_config
from research_os.literature.packet import build_packet
from research_os.literature.search import SearchOptions, search
from research_os.literature.service import LiteratureService
from research_os.literature.store import LiteratureStore
from research_os.proposal.assessor import (
    ASSESSMENT_SCHEMA,
    build_assessment_prompt,
    parse_assessment,
)
from research_os.proposal.context import build_science_context, render_science_context
from research_os.proposal.models import (
    ProposalAssessment,
    ProposalGrounding,
    ResearchProposal,
)
from research_os.proposal.planner import (
    PROPOSAL_SCHEMA,
    build_proposal_prompt,
    parse_proposal,
    validate_proposal,
)
from research_os.proposal.store import ProposalStore, make_proposal_id

PROPOSAL_TIMEOUT_SECONDS = 900
ASSESSMENT_TIMEOUT_SECONDS = 600
LITERATURE_TIMEOUT_SECONDS = 900

#: How many works a literature pass puts in front of the analyst.
LITERATURE_PACKET_WORKS = 12

#: The most model calls one proposal may ever spend.
#:
#: Three: literature, proposal, assessment. A proposal that needed more than
#: that would be a run, and a run is what the automation controller is for.
MAX_MODEL_CALLS = 3


@dataclass
class ProposalOutcome:
    """Everything one proposal run produced."""

    store: ProposalStore
    proposal: ResearchProposal
    assessment: ProposalAssessment | None = None
    literature_keys: tuple[str, ...] = ()
    invocations: list[ModelInvocation] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        return len(self.invocations)


@dataclass
class ProposalController:
    """Drives one goal to an assessed, archived, non-canonical proposal."""

    providers: dict[str, ProviderAdapter]
    config: AutomationConfig
    literature_config: LiteratureConfig = field(default_factory=default_config)
    literature_store: LiteratureStore | None = None

    def propose(
        self,
        *,
        project_path: Path,
        goal: str,
        with_literature: bool = False,
        retrieve: bool = False,
        assess: bool = True,
    ) -> ProposalOutcome:
        """Produce one proposal for ``goal`` against ``project_path``.

        ``with_literature`` adds a read-only literature pass over the local
        index; ``retrieve`` additionally asks the providers first. Retrieval is
        opt-in because it reaches the network, and a researcher running this
        against a project on a train should get a proposal rather than an error.
        """

        goal = goal.strip()
        if not goal:
            raise ProposalValidationError(
                "a proposal needs a goal with at least one character"
            )
        root = repository_root(project_path)
        base_commit = head_commit(root) if has_commits(root) else None
        resolved = self._resolve_roles()

        context = build_science_context(root)
        created_at = utc_now()
        proposal_id = make_proposal_id(
            project_path=str(root), goal=goal, created_at=created_at
        )

        invocations: list[ModelInvocation] = []
        pending: list[tuple[str, str]] = []
        literature_data: str | None = None
        literature_keys: tuple[str, ...] = ()
        if with_literature:
            literature_data, literature_keys, entries = self._literature_pass(
                goal=goal,
                resolved=resolved,
                retrieve=retrieve,
                invocations=invocations,
            )
            pending.extend(entries)

        grounding = ProposalGrounding(
            capsule_ids=list(context.object_ids),
            literature_keys=list(literature_keys),
            finding_ids=[],
        )
        setting = self._setting(resolved, "planner", Role.PLANNER)
        prompt = build_proposal_prompt(
            goal=goal, context=context, literature_data=literature_data
        )
        invocation, result = self._invoke(
            role=Role.PLANNER,
            setting=setting,
            prompt=prompt,
            timeout_seconds=PROPOSAL_TIMEOUT_SECONDS,
            json_schema=PROPOSAL_SCHEMA,
            invocation_id=f"INV-{len(invocations) + 1:04d}",
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProviderInvocationError(
                f"the proposal worker failed: {invocation.error or 'unknown error'}"
            )
        proposal = parse_proposal(
            structured=result.structured,
            text=result.text,
            proposal_id=proposal_id,
            project_path=str(root),
            project_id=context.project_id,
            base_commit=base_commit,
            goal=goal,
            grounding=grounding,
            provider=setting.provider,
            model=invocation.model or setting.model,
            invocation_id=invocation.invocation_id,
        )
        validate_proposal(proposal)

        store = ProposalStore.create(proposal)
        store.append_event(
            "proposal_created",
            project_path=str(root),
            project_id=context.project_id,
            base_commit=base_commit,
            goal=goal,
            capsule_objects=len(context.object_ids),
            literature_works=len(literature_keys),
        )
        for relative, text in pending:
            store.write_text(relative, text)
        store.write_text(f"prompts/{invocation.invocation_id}.txt", prompt)
        store.write_text(
            f"model_outputs/{invocation.invocation_id}.txt", result.text or ""
        )
        store.append_event(
            "proposal_validated",
            items=len(proposal.items),
            uncertainties=len(proposal.uncertainties),
            next_actions=len(proposal.next_actions),
            human_checkpoints=len(proposal.human_checkpoints),
            provider=setting.provider,
            model=invocation.model,
        )

        assessment: ProposalAssessment | None = None
        if assess:
            assessment = self._assess(
                store=store,
                proposal=proposal,
                context_text=render_science_context(context),
                resolved=resolved,
                invocations=invocations,
            )
        return ProposalOutcome(
            store=store,
            proposal=proposal,
            assessment=assessment,
            literature_keys=literature_keys,
            invocations=invocations,
        )

    # -- phases ----------------------------------------------------------

    def _literature_pass(
        self,
        *,
        goal: str,
        resolved: ResolvedRoles,
        retrieve: bool,
        invocations: list[ModelInvocation],
    ) -> tuple[str | None, tuple[str, ...], list[tuple[str, str]]]:
        """Retrieve, rank, and read literature for the goal. Read-only throughout.

        A literature pass that finds nothing is not a failure: it returns no
        block, the proposal is built without one, and the record says the index
        had nothing. Pretending otherwise would make an ungrounded proposal look
        grounded.
        """

        store = self.literature_store or LiteratureStore.open()
        service = LiteratureService(store=store, config=self.literature_config)
        if retrieve:
            service.retrieve(goal)
        results = search(store, goal, SearchOptions(limit=LITERATURE_PACKET_WORKS))
        if not results:
            return None, (), []
        packet = build_packet(goal, results, max_works=LITERATURE_PACKET_WORKS)

        setting = self._setting(resolved, "literature", Role.LITERATURE)
        prompt = build_literature_prompt(goal=goal, packet=packet)
        invocation_id = f"INV-{len(invocations) + 1:04d}"
        invocation, result = self._invoke(
            role=Role.LITERATURE,
            setting=setting,
            prompt=prompt,
            timeout_seconds=LITERATURE_TIMEOUT_SECONDS,
            json_schema=LITERATURE_SCHEMA,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProviderInvocationError(
                f"the literature worker failed: {invocation.error or 'unknown error'}"
            )
        report = parse_literature_report(
            structured=result.structured,
            text=result.text,
            query=goal,
            supplied_keys=packet.work_keys,
            provider=setting.provider,
            model=invocation.model or setting.model,
            invocation_id=invocation_id,
        )
        archived = [
            (f"prompts/{invocation_id}.txt", prompt),
            (f"model_outputs/{invocation_id}.txt", result.text or ""),
            (
                "literature/report.json",
                report.model_dump_json(indent=2) + "\n",
            ),
        ]
        return render_literature_data(report), packet.work_keys, archived

    def _assess(
        self,
        *,
        store: ProposalStore,
        proposal: ResearchProposal,
        context_text: str,
        resolved: ResolvedRoles,
        invocations: list[ModelInvocation],
    ) -> ProposalAssessment:
        setting = self._setting(resolved, "reviewer", Role.REVIEWER)
        prompt = build_assessment_prompt(proposal, context_text=context_text)
        invocation_id = f"INV-{len(invocations) + 1:04d}"
        invocation, result = self._invoke(
            role=Role.REVIEWER,
            setting=setting,
            prompt=prompt,
            timeout_seconds=ASSESSMENT_TIMEOUT_SECONDS,
            json_schema=ASSESSMENT_SCHEMA,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProviderInvocationError(
                f"the proposal assessor failed: {invocation.error or 'unknown error'}"
            )
        assessment = parse_assessment(
            structured=result.structured,
            text=result.text,
            proposal_id=proposal.proposal_id,
            provider=setting.provider,
            model=invocation.model or setting.model,
            independence=str(resolved.independence),
            independence_note=resolved.note,
            invocation_id=invocation_id,
        )
        store.write_text(f"prompts/{invocation_id}.txt", prompt)
        store.write_text(f"model_outputs/{invocation_id}.txt", result.text or "")
        store.save_assessment(assessment)
        store.append_event(
            "proposal_assessed",
            verdict=str(assessment.verdict),
            findings=len(assessment.findings),
            blocking=len(assessment.blocking),
            independence=str(resolved.independence),
        )
        return assessment

    # -- primitives ------------------------------------------------------

    def _invoke(
        self,
        *,
        role: Role,
        setting: RoleSetting,
        prompt: str,
        timeout_seconds: int,
        json_schema: dict | None,
        invocation_id: str,
    ) -> tuple[ModelInvocation, InvocationResult]:
        """Spend one model call and record exactly what happened.

        Every worker this controller uses is context-only: no tools, and a
        working directory outside any repository. There is nothing here that
        could write to a project even if its prompt were subverted, which is
        what lets a proposal run safely against a dirty working tree.
        """

        adapter = self.providers.get(setting.provider)
        if adapter is None:
            raise ProviderUnavailableError(
                f"no adapter for provider {setting.provider!r}"
            )
        if setting.access is not Access.CONTEXT_ONLY:
            raise AutomationError(
                f"the {role} role declares {setting.access} access; a proposal run "
                "only ever dispatches context-only workers, which are given no "
                "tools and no repository"
            )
        cwd = _runtime_cwd()
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
            run_id=_placeholder_run_id(started),
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
            provider_session_id=result.session_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_cost_usd=result.total_cost_usd,
            error=result.error,
        )
        return invocation, result

    def _resolve_roles(self) -> ResolvedRoles:
        return resolve_roles(self.config, probe_registry(self.providers))

    @staticmethod
    def _setting(resolved: ResolvedRoles, name: str, role: Role) -> RoleSetting:
        """Return the configured role, refusing anything but a context-only one."""

        setting = resolved.roles.get(name)
        if setting is None:
            raise AutomationError(
                f"this configuration declares no {name} role, so a proposal run "
                f"cannot dispatch its {role} worker"
            )
        if setting.access is not Access.CONTEXT_ONLY or setting.tools:
            raise AutomationError(
                f"the {name} role declares {setting.access} access with tools "
                f"{', '.join(setting.tools) or 'none'}; a proposal run dispatches "
                "only context-only workers with no tools at all"
            )
        return setting


def _runtime_cwd() -> Path:
    """Return a runtime-owned directory to invoke read-only workers from.

    They have no tools, so this is not what stops them writing. It is the second
    half of the same idea: a process with nothing to act with has no reason to
    be standing in the researcher's project, and keeping it out means a future
    change to its tool set cannot silently inherit that position.
    """

    from research_os.paths import state_home

    target = state_home() / "proposals" / ".workspace"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _placeholder_run_id(created_at: str) -> str:
    """Return a run id shaped like an automation run id.

    ``ModelInvocation`` is shared with the automation control plane and requires
    one. A proposal is not a run, so this records the moment rather than
    pretending an automation run exists; the invocation's real home is the
    proposal directory it is archived in.
    """

    import hashlib

    stamp = created_at.replace("-", "").replace(":", "")
    digest = hashlib.sha256(f"proposal\n{created_at}".encode()).hexdigest()[:8]
    return f"RUN-{stamp}-{digest}"


def independence_of(resolved: ResolvedRoles) -> Independence:
    return resolved.independence
