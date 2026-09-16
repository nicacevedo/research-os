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

import json
import time
from collections.abc import Callable, Sequence
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
from research_os.automation.structured import extract_json_object
from research_os.errors import (
    AutomationError,
    ProposalBudgetError,
    ProposalError,
    ProposalGroundingError,
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
from research_os.proposal.basis import referenced_object_ids, scientific_basis
from research_os.proposal.context import build_science_context, render_science_context
from research_os.proposal.models import (
    ProposalAssessment,
    ProposalGrounding,
    ResearchProposal,
    SuppliedFinding,
)
from research_os.proposal.planner import (
    PROPOSAL_SCHEMA,
    GroundingViolation,
    build_grounding_correction_prompt,
    build_proposal_prompt,
    clipped_statement,
    grounding_violations,
    parse_proposal,
    render_supplied_findings,
    supplied_findings_digest,
    validate_proposal,
)
from research_os.proposal.store import (
    PROPOSAL_ID_RE,
    ProposalStore,
    make_proposal_id,
)

PROPOSAL_TIMEOUT_SECONDS = 900
ASSESSMENT_TIMEOUT_SECONDS = 600
LITERATURE_TIMEOUT_SECONDS = 900

#: How many works a literature pass puts in front of the analyst.
LITERATURE_PACKET_WORKS = 12

#: The most model calls one proposal may ever spend.
#:
#: Four: literature, proposal, one bounded grounding correction, assessment. A
#: proposal that needed more than that would be a run, and a run is what the
#: automation controller is for.
MAX_MODEL_CALLS = 4

#: How many times a refused proposal may be sent back for grounding correction.
#:
#: One, and it is a constant rather than a parameter so that no configuration
#: can turn it into a loop. The first real pilot produced a proposal citing a
#: literature key that did not exist, and the validator refused it -- correctly,
#: and that refusal is the property being preserved here, not relaxed. What was
#: missing was the ability to fix one wrong reference without a human restarting
#: the whole run. A second attempt would be something else: a model being asked
#: repeatedly until it happens to produce something that passes, which is how a
#: gate stops meaning anything.
MAX_GROUNDING_CORRECTIONS = 1

#: The most findings one caller may supply as grounding for one proposal.
#:
#: Twelve, matching :data:`research_os.proposal.planner.MAX_ITEMS`, for the
#: reason that constant gives: a list a researcher cannot read in one sitting
#: is a list they will skim, and a skimmed grounding allowlist is how an
#: ungrounded proposal gets promoted.
MAX_SUPPLIED_FINDINGS = 12


@dataclass(frozen=True, slots=True)
class GroundingCorrection:
    """What the one bounded grounding correction attempt did.

    Recorded whether it succeeded or not. A correction that failed is the more
    interesting record of the two: it says the controller spent a model call and
    still refused to put the proposal in front of a human.
    """

    refused: tuple[str, ...]
    attempted: bool
    corrected: bool
    still_refused: tuple[str, ...] = ()
    reason: str = ""


@dataclass
class ProposalOutcome:
    """Everything one proposal run produced."""

    store: ProposalStore
    proposal: ResearchProposal
    assessment: ProposalAssessment | None = None
    literature_keys: tuple[str, ...] = ()
    invocations: list[ModelInvocation] = field(default_factory=list)
    grounding_correction: GroundingCorrection | None = None
    adopted: bool = False
    """True when a reserved id already named a proposal and it was reused.

    Reported rather than hidden, because "a proposal was produced" and "a
    proposal produced by an earlier attempt was found" are different facts and
    the model calls this run spent differ between them.
    """

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
        max_model_calls: int | None = None,
        findings: Sequence[SuppliedFinding] = (),
        proposal_id: str = "",
    ) -> ProposalOutcome:
        """Produce one proposal, and make every failure say what it cost.

        The accounting wrapper is separate from the work because a failed
        proposal spends real model calls and used to report none. A caller with
        a budget charged only on success, so a run whose planner reliably cited
        a nonexistent key could spend three calls a time, be retried, and spend
        three more against a ledger that never moved. Found by an independent
        review of this release.
        """

        spent: list[ModelInvocation] = []
        try:
            return self._propose(
                project_path=project_path,
                goal=goal,
                with_literature=with_literature,
                retrieve=retrieve,
                assess=assess,
                max_model_calls=max_model_calls,
                invocations=spent,
                findings=tuple(findings),
                proposal_id=proposal_id,
            )
        except (ProposalError, ProviderInvocationError) as failure:
            failure.model_calls = len(spent)
            raise

    def _propose(
        self,
        *,
        project_path: Path,
        goal: str,
        with_literature: bool,
        retrieve: bool,
        assess: bool,
        max_model_calls: int | None,
        invocations: list[ModelInvocation],
        findings: tuple[SuppliedFinding, ...] = (),
        proposal_id: str = "",
    ) -> ProposalOutcome:
        """Produce one proposal for ``goal`` against ``project_path``.

        ``with_literature`` adds a read-only literature pass over the local
        index; ``retrieve`` additionally asks the providers first. Retrieval is
        opt-in because it reaches the network, and a researcher running this
        against a project on a train should get a proposal rather than an error.

        ``max_model_calls`` is the ceiling a caller with a budget of its own
        imposes on this run. It exists for the bounded grounding correction: the
        correction is an ordinary model call, so whether one can be afforded is
        the caller's question, not this controller's. ``None`` means only this
        controller's own :data:`MAX_MODEL_CALLS` applies.

        ``findings`` are observations the caller supplies as grounding. They
        become three things at once, and it has to be all three or none: the
        contents of a fenced data block in the prompt, the ``finding_ids`` half
        of the grounding allowlist the validator checks every citation against,
        and ``supplied_findings`` on the stored proposal so a reader months
        later can see what each cited id said. Supplying the block without the
        allowlist would show a worker identifiers it is then refused for citing;
        supplying the allowlist without the block would permit citations to
        text nobody can read. Bounded by
        :data:`MAX_SUPPLIED_FINDINGS`.

        ``proposal_id`` lets a caller decide this proposal's identity *before*
        calling, which is what closes the crash window for a caller that runs
        inside a resuming runtime. Creating the proposal directory is an
        externally visible effect, so a caller that learns the id only from the
        return value has an interval -- the whole of this method -- in which a
        crash leaves a directory it cannot find again, and a retry mints a
        second proposal for one logical decision. With a reserved id the retry
        computes the same id, and the caller's reconciler finds the directory.
        The default is the derived id, unchanged.
        """

        if max_model_calls is not None:
            max_model_calls = min(max_model_calls, MAX_MODEL_CALLS)
        else:
            max_model_calls = MAX_MODEL_CALLS

        goal = goal.strip()
        if not goal:
            raise ProposalValidationError(
                "a proposal needs a goal with at least one character"
            )
        root = repository_root(project_path)
        base_commit = head_commit(root) if has_commits(root) else None
        resolved = self._resolve_roles()

        if len(findings) > MAX_SUPPLIED_FINDINGS:
            raise ProposalValidationError(
                f"a proposal may be grounded in at most {MAX_SUPPLIED_FINDINGS} "
                f"supplied findings; this one was given {len(findings)}. More "
                f"than that is not grounding, it is a corpus, and a worker "
                f"handed a corpus of identifiers cites from it decoratively"
            )
        # Clipped to what the prompt will actually show, so the stored
        # statement and the text the worker read are the same string. See
        # `clipped_statement`.
        findings = tuple(
            item.model_copy(update={"statement": clipped_statement(item.statement)})
            for item in findings
        )
        supplied_ids = [item.finding_id for item in findings]
        if len(set(supplied_ids)) != len(supplied_ids):
            raise ProposalValidationError(
                "the same finding was supplied twice: "
                + ", ".join(
                    sorted({x for x in supplied_ids if supplied_ids.count(x) > 1})
                )
            )

        findings_digest = supplied_findings_digest(findings)

        context = build_science_context(root)
        created_at = utc_now()
        reserved = bool(proposal_id)
        if proposal_id:
            if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
                raise ProposalValidationError(
                    f"reserved proposal id {proposal_id!r} is not a proposal id; a "
                    f"caller reserving one must mint it with "
                    f"`proposal.store.reserved_proposal_id`"
                )
        else:
            proposal_id = make_proposal_id(
                project_path=str(root), goal=goal, created_at=created_at
            )

        pending: list[tuple[str, str]] = []
        literature_data: str | None = None
        literature_keys: tuple[str, ...] = ()
        if with_literature:
            literature_data, literature_keys, entries = self._literature_pass(
                goal=goal,
                resolved=resolved,
                retrieve=retrieve,
                invocations=invocations,
                max_model_calls=max_model_calls,
            )
            pending.extend(entries)

        grounding = ProposalGrounding(
            capsule_ids=list(context.object_ids),
            literature_keys=list(literature_keys),
            # Exactly what was supplied, and nothing else. The allowlist is
            # computed here rather than taken from the worker, which is the
            # property that makes a citation to an unavailable finding fail
            # validation in precisely the way one to an unavailable capsule
            # object or literature key does.
            finding_ids=list(supplied_ids),
        )
        setting = self._setting(resolved, "planner", Role.PLANNER)
        prompt = build_proposal_prompt(
            goal=goal,
            context=context,
            literature_data=literature_data,
            findings_data=render_supplied_findings(findings) or None,
            grounding_finding_ids=supplied_ids,
        )
        self._assert_affordable(invocations, max_model_calls, "the proposal worker")
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
        payload = result.structured
        if payload is None:
            payload = extract_json_object(result.text)

        def _parse(candidate: dict | None, source: ModelInvocation) -> ResearchProposal:
            parsed = parse_proposal(
                structured=candidate,
                text=result.text if candidate is payload else None,
                proposal_id=proposal_id,
                project_path=str(root),
                project_id=context.project_id,
                base_commit=base_commit,
                goal=goal,
                grounding=grounding,
                provider=setting.provider,
                model=source.model or setting.model,
                invocation_id=source.invocation_id,
                supplied_findings=findings,
            )
            # The basis is snapshotted *after* parsing, because what it
            # snapshots is the objects the proposal's items turned out to cite,
            # not the whole set the worker was shown. Snapshotting the offered
            # set would make any change anywhere in the capsule invalidate
            # every waiting proposal, which is the over-broad check this
            # replaces. Recorded by reconstructing the proposal rather than
            # mutating it: the model is frozen, and a basis attached after
            # validation is a basis the validator never saw.
            return parsed.model_copy(
                update={
                    "scientific_basis": scientific_basis(
                        referenced_object_ids(parsed, root=root),
                        root=root,
                        project_id=context.project_id,
                        literature_keys=literature_keys,
                        finding_packet_digest=findings_digest,
                        include_charter=bool(context.charter),
                    )
                }
            )

        correction: GroundingCorrection | None = None
        correction_prompt = ""
        correction_text = ""
        try:
            proposal = _parse(payload, invocation)
            validate_proposal(proposal)
        except ProposalValidationError as first:
            # The trigger is a fact about the payload, not the wording of the
            # error: ``grounding_violations`` re-derives which citations were
            # unsupplied. Anything else -- a malformed shape, a non-sequential
            # id, an experiment that discriminates nothing -- has no correction
            # path and is re-raised exactly as before.
            violations = (
                grounding_violations(payload, grounding)
                if isinstance(payload, dict)
                else ()
            )
            if not violations:
                raise
            proposal, correction, correction_prompt, correction_text = (
                self._correct_grounding(
                    first=first,
                    violations=violations,
                    payload=payload,
                    goal=goal,
                    grounding=grounding,
                    setting=setting,
                    parse=_parse,
                    invocations=invocations,
                    max_model_calls=max_model_calls,
                )
            )

        # A reserved id may already name a directory a previous attempt created
        # before it crashed. Adoption returns that proposal unchanged rather
        # than a second one; a derived id is new by construction and still
        # refuses an existing directory.
        if reserved:
            store, created = ProposalStore.adopt_or_create(proposal)
            if not created:
                adopted = store.load()
                store.append_event(
                    "proposal_adopted",
                    reason=(
                        "a previous attempt had already created this reserved "
                        "proposal; returning it rather than creating a second"
                    ),
                )
                return ProposalOutcome(
                    store=store,
                    proposal=adopted,
                    assessment=store.load_assessment(),
                    literature_keys=literature_keys,
                    invocations=invocations,
                    grounding_correction=correction,
                    adopted=True,
                )
        else:
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
        if correction is not None:
            # The refused payload, written as itself rather than left to
            # ``result.text``. A provider answering through a structured-output
            # schema returns no text at all, so relying on the text file would
            # have preserved the correction and lost the thing it corrected --
            # which is the one artefact a reader needs to judge whether the
            # correction was honest.
            store.write_text(
                f"model_outputs/{invocation.invocation_id}.refused.json",
                json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False),
            )
            store.append_event(
                "proposal_grounding_refused",
                invocation_id=invocation.invocation_id,
                unsupplied=list(correction.refused),
                refused_output=f"model_outputs/{invocation.invocation_id}.refused.json",
            )
            corrected_id = invocations[-1].invocation_id
            store.write_text(f"prompts/{corrected_id}.txt", correction_prompt)
            store.write_text(f"model_outputs/{corrected_id}.txt", correction_text)
            store.append_event(
                "proposal_grounding_corrected",
                invocation_id=corrected_id,
                attempts=MAX_GROUNDING_CORRECTIONS,
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
                max_model_calls=max_model_calls,
            )
        return ProposalOutcome(
            store=store,
            proposal=proposal,
            assessment=assessment,
            literature_keys=literature_keys,
            invocations=invocations,
            grounding_correction=correction,
        )

    def _correct_grounding(
        self,
        *,
        first: ProposalValidationError,
        violations: tuple[GroundingViolation, ...],
        payload: dict,
        goal: str,
        grounding: ProposalGrounding,
        setting: RoleSetting,
        parse: Callable[[dict | None, ModelInvocation], ResearchProposal],
        invocations: list[ModelInvocation],
        max_model_calls: int,
    ) -> tuple[ResearchProposal, GroundingCorrection, str, str]:
        """Spend one model call to make a refused proposal cite only what it has.

        The bound is the design. One attempt, the same evidence packet, the same
        goal, no new authority of any kind -- the correction worker is given a
        strictly smaller task than the one that already failed, and if it fails
        too the run ends. What it may *not* do is as important as what it may:
        it cannot widen the evidence universe, because ``grounding`` is the same
        object the first attempt was held to and is re-applied to its output by
        the same validator.
        """

        refused = tuple(item.render() for item in violations)
        unsupplied = sorted({item.cited for item in violations})

        # Budget first, and before the prompt is even built. A correction is an
        # ordinary model call and is charged like one; a controller that spent a
        # call it had not checked for would be deciding its own budget.
        if len(invocations) >= max_model_calls:
            raise ProposalGroundingError(
                f"{first}. One grounding correction was available but this run "
                f"has spent its {max_model_calls} model call(s), so the refused "
                "proposal stands. Unsupplied identifier(s): " + ", ".join(unsupplied)
            ) from first

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
            timeout_seconds=PROPOSAL_TIMEOUT_SECONDS,
            json_schema=PROPOSAL_SCHEMA,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        if not result.ok:
            raise ProposalGroundingError(
                f"{first}. The one grounding correction failed to run: "
                f"{invocation.error or 'unknown error'}"
            ) from first

        corrected_payload = result.structured
        if corrected_payload is None:
            corrected_payload = extract_json_object(result.text)
        again = (
            grounding_violations(corrected_payload, grounding)
            if isinstance(corrected_payload, dict)
            else ()
        )
        if again:
            # The exact failure this bound exists for: a worker told an
            # identifier does not exist invented another one. There is no second
            # attempt, because a gate that retries until it passes is not a gate.
            raise ProposalGroundingError(
                f"{first}. The one available grounding correction was used and "
                "still cited identifiers this run did not have: "
                + ", ".join(sorted({item.cited for item in again}))
                + ". No further correction is attempted."
            ) from first
        try:
            proposal = parse(corrected_payload, invocation)
            validate_proposal(proposal)
        except ProposalValidationError as second:
            raise ProposalGroundingError(
                f"{first}. The one available grounding correction was used and "
                f"its output is still not a valid proposal: {second}"
            ) from second

        correction = GroundingCorrection(
            refused=refused,
            attempted=True,
            corrected=True,
        )
        return proposal, correction, prompt, result.text or ""

    # -- phases ----------------------------------------------------------

    def _literature_pass(
        self,
        *,
        goal: str,
        resolved: ResolvedRoles,
        retrieve: bool,
        invocations: list[ModelInvocation],
        max_model_calls: int,
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
        self._assert_affordable(invocations, max_model_calls, "the literature analyst")
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
        max_model_calls: int,
    ) -> ProposalAssessment:
        setting = self._setting(resolved, "reviewer", Role.REVIEWER)
        self._assert_affordable(invocations, max_model_calls, "the assessor")
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

    @staticmethod
    def _assert_affordable(
        invocations: list[ModelInvocation], ceiling: int, what: str
    ) -> None:
        """Refuse a model call this run cannot pay for, before it is made.

        Checked at every spend, not only at the bounded grounding correction --
        which is where it was first needed and where, for one release, it was
        the only place it existed. A ceiling enforced at one of four call sites
        is not a ceiling; it is a parameter whose name promises something the
        code does not do, and the next caller to trust it is the one who finds
        out. Found by an independent reviewer and reproduced: a run asked for a
        ceiling of one made two calls.
        """

        if len(invocations) >= ceiling:
            raise ProposalBudgetError(
                f"this proposal run may spend {ceiling} model call(s) and has "
                f"already spent {len(invocations)}; {what} would exceed it"
            )

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
