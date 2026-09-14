"""Drives one goal to a grounded, archived, explicitly non-scientific assessment.

Structurally the proposal controller's sibling, and deliberately so: one
context-only worker, one bounded grounding correction, one archive, every model
call budgeted before it is spent. What differs is the universe it hands the
worker and the object it gets back.

The controller chooses that universe. A caller never says "assess this as a
repository"; the caller supplies a :class:`~research_os.automation.profile.ProjectProfile`,
and the profile's ``capsule_present`` decides. A model cannot reach this
decision from either side of it.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from research_os.assessment.models import (
    AssessmentGrounding,
    TechnicalAssessment,
)
from research_os.assessment.planner import (
    ASSESSMENT_SCHEMA,
    GroundingViolation,
    build_assessment_prompt,
    build_grounding_correction_prompt,
    grounding_violations,
    parse_assessment,
    render_repository_context,
    validate_assessment,
)
from research_os.assessment.store import AssessmentStore, make_assessment_id
from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import (
    Access,
    ModelInvocation,
    Role,
    RoleSetting,
    utc_now,
)
from research_os.automation.profile import ProjectProfile, inspect_repository
from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    probe_registry,
)
from research_os.errors import (
    AssessmentError,
    AssessmentGroundingError,
    AssessmentValidationError,
    AutomationError,
    ProviderInvocationError,
    ProviderUnavailableError,
)
from research_os.literature.config import LiteratureConfig
from research_os.literature.config import default_config as default_literature_config
from research_os.literature.models import SearchResult
from research_os.literature.packet import build_packet, render_packet
from research_os.literature.store import LiteratureStore

ASSESSMENT_TIMEOUT_SECONDS = 900

#: How many works a literature pass puts in front of the assessment worker.
LITERATURE_PACKET_WORKS = 12

#: The most model calls one assessment may ever spend.
#:
#: Two: the assessment itself and one bounded grounding correction. An
#: assessment that needed more than that would be a run, and a run is what the
#: research controller is for.
MAX_MODEL_CALLS = 2

#: How many times a refused assessment may be sent back for grounding correction.
#:
#: One, and a constant rather than a parameter so that no configuration can turn
#: it into a loop. A second attempt would be a model being asked repeatedly
#: until it happens to produce something that passes, which is how a gate stops
#: meaning anything.
MAX_GROUNDING_CORRECTIONS = 1


@dataclass(frozen=True, slots=True)
class GroundingCorrection:
    """The record of the one bounded correction, when it happened."""

    refused: tuple[str, ...]
    invocation_id: str


@dataclass
class AssessmentOutcome:
    """Everything one assessment run produced."""

    store: AssessmentStore
    assessment: TechnicalAssessment
    literature_keys: tuple[str, ...] = ()
    invocations: list[ModelInvocation] = field(default_factory=list)
    grounding_correction: GroundingCorrection | None = None

    @property
    def model_calls(self) -> int:
        return len(self.invocations)


@dataclass
class AssessmentController:
    """Produces one grounded technical assessment of one repository."""

    providers: dict[str, ProviderAdapter]
    config: AutomationConfig
    literature_config: LiteratureConfig = field(
        default_factory=default_literature_config
    )
    literature_store: LiteratureStore | None = None

    def assess(
        self,
        *,
        project_path: Path,
        goal: str,
        profile: ProjectProfile,
        check_ids: tuple[str, ...] = (),
        with_literature: bool = False,
        literature_keys: tuple[str, ...] = (),
        analysis_data: str | None = None,
        max_model_calls: int | None = None,
    ) -> AssessmentOutcome:
        """Produce one assessment, and make every failure say what it cost.

        The accounting wrapper is separate from the work for the reason the
        proposal layer documents: a failed assessment spends real model calls,
        and a budget that only counts successes is not a budget.
        """

        spent: list[ModelInvocation] = []
        try:
            return self._assess(
                project_path=project_path,
                goal=goal,
                profile=profile,
                check_ids=check_ids,
                with_literature=with_literature,
                literature_keys=literature_keys,
                analysis_data=analysis_data,
                max_model_calls=max_model_calls,
                invocations=spent,
            )
        except (AssessmentError, ProviderInvocationError) as failure:
            failure.model_calls = len(spent)
            raise

    def _assess(
        self,
        *,
        project_path: Path,
        goal: str,
        profile: ProjectProfile,
        check_ids: tuple[str, ...],
        with_literature: bool,
        literature_keys: tuple[str, ...],
        analysis_data: str | None,
        max_model_calls: int | None,
        invocations: list[ModelInvocation],
    ) -> AssessmentOutcome:
        if profile.capsule_present:
            raise AssessmentValidationError(
                "this project holds a Research Capsule, so its reasoning belongs "
                "in the scientific proposal pipeline. A technical assessment is "
                "the capsule-less mode and would discard the project's own "
                "scientific state."
            )
        ceiling = (
            min(max_model_calls, MAX_MODEL_CALLS)
            if max_model_calls is not None
            else MAX_MODEL_CALLS
        )
        goal = goal.strip()
        if not goal:
            raise AssessmentValidationError(
                "an assessment needs a goal with at least one character"
            )
        root = repository_root(project_path)
        base_commit = head_commit(root) if has_commits(root) else None
        resolved = self._resolve_roles()
        facts = inspect_repository(root)

        # The grounding universe, built from what is actually here. ``capsule_ids``
        # stays empty: this mode has no scientific objects, and leaving the field
        # present and empty is what turns an invented CLAIM-0001 into a refusal
        # rather than an unmodelled case.
        grounding = AssessmentGrounding(
            repository_files=sorted(facts.tracked),
            literature_keys=sorted(literature_keys),
            check_ids=sorted(check_ids),
            capsule_ids=[],
            base_commit=base_commit,
        )

        literature_data: str | None = None
        pending: list[tuple[str, str]] = []
        if with_literature and literature_keys:
            literature_data, pending = self._literature_block(literature_keys)

        created_at = utc_now()
        assessment_id = make_assessment_id(
            project_path=str(root), goal=goal, created_at=created_at
        )
        setting = self._setting(resolved, "planner", Role.PLANNER)
        prompt = build_assessment_prompt(
            goal=goal,
            grounding=grounding,
            repository_context=render_repository_context(
                tracked=sorted(facts.tracked),
                base_commit=base_commit,
                notes=list(facts.errors),
            ),
            literature_data=literature_data,
            analysis_data=analysis_data,
        )
        self._assert_affordable(invocations, ceiling, "the assessment worker")
        invocation, result = self._invoke(
            role=Role.PLANNER,
            setting=setting,
            prompt=prompt,
            invocation_id=f"INV-{len(invocations) + 1:04d}",
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProviderInvocationError(
                f"the assessment worker failed: {invocation.error or 'unknown error'}"
            )
        payload = result.structured
        if payload is None:
            from research_os.automation.structured import extract_json_object

            payload = extract_json_object(result.text)

        def parse(
            candidate: dict | None, source: ModelInvocation
        ) -> TechnicalAssessment:
            return parse_assessment(
                structured=candidate,
                text=result.text if candidate is payload else None,
                assessment_id=assessment_id,
                project_path=str(root),
                project_id=profile.project_id,
                base_commit=base_commit,
                goal=goal,
                grounding=grounding,
                provider=setting.provider,
                model=source.model or setting.model,
                invocation_id=source.invocation_id,
            )

        correction: GroundingCorrection | None = None
        correction_prompt = ""
        correction_text = ""
        try:
            assessment = parse(payload, invocation)
            validate_assessment(assessment)
        except AssessmentValidationError as first:
            violations = (
                grounding_violations(payload, grounding)
                if isinstance(payload, dict)
                else ()
            )
            if not violations:
                raise
            assessment, correction, correction_prompt, correction_text = (
                self._correct_grounding(
                    first=first,
                    violations=violations,
                    payload=payload,
                    goal=goal,
                    grounding=grounding,
                    setting=setting,
                    parse=parse,
                    invocations=invocations,
                    ceiling=ceiling,
                )
            )

        store = AssessmentStore.create(assessment)
        store.append_event(
            "assessment_created",
            project_path=str(root),
            project_id=profile.project_id,
            base_commit=base_commit,
            goal=goal,
            mode=str(assessment.mode),
            repository_files=len(grounding.repository_files),
            literature_works=len(grounding.literature_keys),
            check_ids=list(grounding.check_ids),
        )
        for relative, text in pending:
            store.write_text(relative, text)
        store.write_text(f"prompts/{invocation.invocation_id}.txt", prompt)
        store.write_text(
            f"model_outputs/{invocation.invocation_id}.txt", result.text or ""
        )
        if correction is not None:
            store.write_text(
                f"model_outputs/{invocation.invocation_id}.refused.json",
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            )
            store.append_event(
                "assessment_grounding_refused",
                invocation_id=invocation.invocation_id,
                unsupplied=list(correction.refused),
                refused_output=f"model_outputs/{invocation.invocation_id}.refused.json",
            )
            store.write_text(
                f"prompts/{correction.invocation_id}.txt", correction_prompt
            )
            store.write_text(
                f"model_outputs/{correction.invocation_id}.txt", correction_text
            )
            store.append_event(
                "assessment_grounding_corrected",
                invocation_id=correction.invocation_id,
                attempts=MAX_GROUNDING_CORRECTIONS,
            )
        store.append_event(
            "assessment_validated",
            observations=len(assessment.observations),
            uncertainties=len(assessment.uncertainties),
            next_actions=len(assessment.next_actions),
            human_actions=len(assessment.human_actions),
            cited_files=len(assessment.cited_files),
            provider=setting.provider,
            model=invocation.model,
        )
        return AssessmentOutcome(
            store=store,
            assessment=assessment,
            literature_keys=tuple(grounding.literature_keys),
            invocations=invocations,
            grounding_correction=correction,
        )

    def _correct_grounding(
        self,
        *,
        first: AssessmentValidationError,
        violations: tuple[GroundingViolation, ...],
        payload: dict,
        goal: str,
        grounding: AssessmentGrounding,
        setting: RoleSetting,
        parse: Callable[[dict | None, ModelInvocation], TechnicalAssessment],
        invocations: list[ModelInvocation],
        ceiling: int,
    ) -> tuple[TechnicalAssessment, GroundingCorrection, str, str]:
        """Make exactly one attempt to reground an assessment that cited fiction.

        The refusal is the property being preserved, not relaxed. What this adds
        is the ability to repair one wrong reference without a human restarting
        the run -- and it is a constant, not a parameter, so nothing can turn it
        into "ask until it passes".
        """

        self._assert_affordable(invocations, ceiling, "the grounding correction")
        prompt = build_grounding_correction_prompt(
            goal=goal,
            payload=payload,
            violations=violations,
            grounding=grounding,
        )
        invocation_id = f"INV-{len(invocations) + 1:04d}"
        invocation, result = self._invoke(
            role=Role.PLANNER,
            setting=setting,
            prompt=prompt,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProviderInvocationError(
                "the assessment grounding correction failed: "
                f"{invocation.error or 'unknown error'}"
            )
        corrected = result.structured
        if corrected is None:
            from research_os.automation.structured import extract_json_object

            corrected = extract_json_object(result.text)
        try:
            assessment = parse(corrected, invocation)
            validate_assessment(assessment)
        except AssessmentValidationError as second:
            raise AssessmentGroundingError(
                "the assessment cited things this run did not supply, and the one "
                f"correction did not repair it. First refusal: {first}. After "
                f"correction: {second}"
            ) from second
        record = GroundingCorrection(
            refused=tuple(sorted({item.cited for item in violations})),
            invocation_id=invocation_id,
        )
        return assessment, record, prompt, result.text or ""

    def _literature_block(
        self, literature_keys: tuple[str, ...]
    ) -> tuple[str | None, list[tuple[str, str]]]:
        """Render the retrieved works this assessment may cite. No model call.

        Built straight from the store's own records rather than by re-running a
        search, because the keys are already decided: the research controller
        ran the literature task, charged for it, and handed the result here.
        Spending a second model call to have a worker re-read what was just
        retrieved would make the assessment pay for the literature layer twice.
        """

        owned = self.literature_store is None
        store = self.literature_store or LiteratureStore.open()
        try:
            works = store.works(sorted(literature_keys))
        finally:
            if owned:
                store.close()
        if not works:
            return None, []
        packet = build_packet(
            "works retrieved for this assessment",
            [
                SearchResult(work=item, score=0.0, matched="retrieved", snippet="")
                for item in works
            ],
            max_works=LITERATURE_PACKET_WORKS,
            notes=("these are the works this run retrieved; they were not re-ranked",),
        )
        rendered = render_packet(packet)
        return rendered, [("literature/packet.txt", rendered)]

    # -- primitives ------------------------------------------------------

    @staticmethod
    def _assert_affordable(
        invocations: list[ModelInvocation], ceiling: int, what: str
    ) -> None:
        """Refuse a model call this run cannot pay for, before it is made."""

        if len(invocations) >= ceiling:
            raise AssessmentValidationError(
                f"this assessment may spend {ceiling} model call(s) and has "
                f"already spent {len(invocations)}; {what} would exceed it"
            )

    def _invoke(
        self,
        *,
        role: Role,
        setting: RoleSetting,
        prompt: str,
        invocation_id: str,
    ) -> tuple[ModelInvocation, InvocationResult]:
        """Spend one model call and record exactly what happened.

        Context-only, always: no tools, and a working directory outside any
        repository. There is nothing here that could write to a project even if
        its prompt were subverted, which is what lets an assessment run safely
        against a repository the researcher is in the middle of editing.
        """

        adapter = self.providers.get(setting.provider)
        if adapter is None:
            raise ProviderUnavailableError(
                f"no adapter for provider {setting.provider!r}"
            )
        if setting.access is not Access.CONTEXT_ONLY:
            raise AutomationError(
                f"the {role} role declares {setting.access} access; an assessment "
                "only ever dispatches context-only workers"
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
                timeout_seconds=ASSESSMENT_TIMEOUT_SECONDS,
                model=setting.model,
                effort=setting.effort,
                access=Access.CONTEXT_ONLY,
                tools=(),
                json_schema=ASSESSMENT_SCHEMA,
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
            timeout_seconds=ASSESSMENT_TIMEOUT_SECONDS,
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
        setting = resolved.roles.get(name)
        if setting is None:
            raise AutomationError(
                f"this configuration declares no {name} role, so an assessment "
                f"cannot dispatch its {role} worker"
            )
        if not setting.read_only or setting.access is not Access.CONTEXT_ONLY:
            raise AutomationError(
                f"the {name} role is configured with {setting.access} access; an "
                "assessment worker is read-only and context-only"
            )
        return setting


def _runtime_cwd() -> Path:
    """Return a working directory outside every repository.

    The same choice the proposal controller makes, for the same reason: a
    context-only worker has no business being started inside the code it is
    describing.
    """

    from research_os.paths import state_home

    target = state_home() / "assessment-workers"
    target.mkdir(parents=True, exist_ok=True)
    return target


def _placeholder_run_id(created_at: str) -> str:
    """Return the run id a standalone invocation record carries.

    A :class:`ModelInvocation` belongs to a run. An assessment is not a run, so
    it carries a derived id in the same shape rather than a blank field that a
    later reader would have to guess the meaning of.
    """

    import hashlib

    digest = hashlib.sha256(f"assessment\n{created_at}".encode()).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"RUN-{stamp}-{digest}"
