"""The only door between the autonomous runtime and scientific truth.

Everything the runtime knows about a project's science it learns here, and
everything it is allowed to do to that science it does here. That is the point:
one module to read when the question is "could the runtime have faked this",
rather than a search across a package.

**What it can do.** Read a capsule, validate it, resolve an object, compute a
digest, ask whether a Claim's approval currently stands, and describe the
unresolved frontier. All of it read-only, all of it delegating to the kernel's
own implementation -- `validate_project`, `claim_approval`, `subject_digest` --
rather than reimplementing any of it. A second implementation of the acceptance
rule would eventually disagree with the first, and the one that disagreed
quietly would be the one that let an unapproved claim be quoted.

**What it cannot do.** Record a human Review. Move a Claim to `accepted`. Write
any capsule file at all. There is no method here that does, no private helper
that does, and `tests/test_runtime_authority.py` asserts the absence by
inspecting the module rather than by trusting this paragraph.

That is not an oversight to be filled in by a later release. It is the
difference between a system with near-total operational autonomy and a system
with scientific authority, and the whole architecture is arranged so that the
first does not quietly become the second.

**Proposing is not accepting.** The runtime *can* prepare scientific content --
a drafted hypothesis, an evidence packet, a review packet, a decision packet --
and it does so constantly. Those go to a person as a proposal through the v1
proposal and paper layers, which already require human promotion. This module
exposes the reading and the checking those layers need; it does not offer a
shortcut past them.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

from research_os.capsule import (
    ProjectStatusReport,
    ProjectValidationReport,
    load_project_identity,
    project_status,
    resolve_object,
    validate_project,
)
from research_os.digests import subject_digest
from research_os.errors import CapsuleError, ResearchOSError
from research_os.models import (
    Claim,
    ClaimStatus,
    Evidence,
    EvidenceStatus,
    Experiment,
    ExperimentStatus,
    Hypothesis,
    HypothesisStatus,
    Question,
    QuestionStatus,
    ScientificObject,
)

#: Hypotheses that are neither resolved nor retired, and therefore still work.
#: ``TESTING`` is included in "needs a test" but not in "actionable": something
#: is already running for it, and proposing a second experiment for a hypothesis
#: under test is how a runtime duplicates its own work.
UNRESOLVED_HYPOTHESIS_STATUSES = frozenset(
    {HypothesisStatus.DRAFT, HypothesisStatus.ACTIVE}
)
TESTABLE_HYPOTHESIS_STATUSES = UNRESOLVED_HYPOTHESIS_STATUSES | {
    HypothesisStatus.TESTING
}
from research_os.validate import ClaimApproval, claim_approval

LOG = logging.getLogger("research_os.runtime.kernel")


class ScientificAuthorityError(ResearchOSError):
    """Raised when the runtime is asked to do something only a person may do.

    Raised rather than returned, and never caught inside the runtime, because
    the correct handling of "this needs human authority" is to stop the cycle
    and prepare a decision packet -- not to work around it.
    """


@dataclass(frozen=True, slots=True)
class Frontier:
    """The unresolved research frontier, derived from capsule files only.

    Every field is computed from the objects on disk by ordinary Python. No
    model is consulted to produce it, which matters for two reasons: it is
    free, so it can be recomputed at the start of every cycle; and it is
    deterministic, so two cycles that disagree about what is unresolved are
    disagreeing about the files rather than about the weather.
    """

    project_id: str
    open_questions: tuple[str, ...] = ()
    actionable_hypotheses: tuple[str, ...] = ()
    hypotheses_without_tests: tuple[str, ...] = ()
    claims_awaiting_review: tuple[str, ...] = ()
    claims_with_stale_review: tuple[str, ...] = ()
    contested_claims: tuple[str, ...] = ()
    evidence_gaps: tuple[str, ...] = ()
    pending_experiments: tuple[str, ...] = ()
    validation_errors: tuple[str, ...] = ()
    notes: tuple[str, ...] = field(default=())

    @property
    def empty(self) -> bool:
        """True when the capsule presents nothing actionable.

        Used by the continuation policy to conclude ``DONE_FOR_NOW`` rather than
        inventing work. A frontier that is never empty is a runtime that never
        stops, which is the failure mode this property exists to make
        expressible.
        """

        return not (
            self.open_questions
            or self.actionable_hypotheses
            or self.hypotheses_without_tests
            or self.claims_awaiting_review
            or self.claims_with_stale_review
            or self.contested_claims
            or self.evidence_gaps
            or self.pending_experiments
        )

    def summary(self) -> dict[str, int]:
        return {
            "open_questions": len(self.open_questions),
            "actionable_hypotheses": len(self.actionable_hypotheses),
            "hypotheses_without_tests": len(self.hypotheses_without_tests),
            "claims_awaiting_review": len(self.claims_awaiting_review),
            "claims_with_stale_review": len(self.claims_with_stale_review),
            "contested_claims": len(self.contested_claims),
            "evidence_gaps": len(self.evidence_gaps),
            "pending_experiments": len(self.pending_experiments),
            "validation_errors": len(self.validation_errors),
        }


class ScientificKernelAdapter:
    """Read-only access to one project's canonical scientific state."""

    __slots__ = ("_repo",)

    def __init__(self, repo_path: Path | str) -> None:
        self._repo = Path(repo_path)

    @property
    def repo_path(self) -> Path:
        return self._repo

    # ------------------------------------------------------------- identity --
    def identity(self) -> tuple[Path, object]:
        """Return ``(git_root, Project)`` without validating the science."""

        return load_project_identity(self._repo)

    def project_id(self) -> str:
        _root, project = self.identity()
        return str(project.id)

    def has_capsule(self) -> bool:
        try:
            self.identity()
        except ResearchOSError:
            return False
        return True

    # ----------------------------------------------------------- inspection --
    def validate(self) -> ProjectValidationReport:
        """Validate the capsule. Never writes, never consults the registry."""

        return validate_project(self._repo)

    def status(self) -> ProjectStatusReport:
        return project_status(self._repo)

    def objects(self) -> tuple[ScientificObject, ...]:
        return self.validate().objects

    def by_id(self) -> dict[str, ScientificObject]:
        return {str(obj.id): obj for obj in self.objects()}

    def object(self, object_id: str) -> ScientificObject:
        return resolve_object(self.validate(), object_id)

    def digest(self, object_id: str) -> str:
        report = self.validate()
        if report.project is None:
            raise CapsuleError(f"{self._repo}: no valid project identity")
        return subject_digest(
            resolve_object(report, object_id), project_id=str(report.project.id)
        )

    # ------------------------------------------------------------ acceptance --
    def approval(self, claim_id: str) -> ClaimApproval:
        """Ask the kernel whether this Claim's human approval currently stands.

        Delegates to :func:`research_os.validate.claim_approval`, which is the
        one implementation of the acceptance rule. Anything in the runtime that
        needs to know whether a claim may be quoted asks this and nothing else.
        """

        report = self.validate()
        if report.project is None:
            raise CapsuleError(f"{self._repo}: no valid project identity")
        claim = resolve_object(report, claim_id)
        if not isinstance(claim, Claim):
            raise CapsuleError(f"{claim_id} is not a Claim")
        return claim_approval(claim, self.by_id(), str(report.project.id))

    def quotable_claims(self) -> tuple[str, ...]:
        """Claims an author agent may state as established.

        Both conditions, because either alone has been enough to let an
        unapproved claim into prose: the file says ``accepted``, *and* the
        kernel says a qualifying human approval still stands against the
        current digests.
        """

        report = self.validate()
        if report.project is None or not report.ok:
            # A capsule with validation errors is a capsule whose cross-object
            # invariants are not known to hold, and the acceptance rule is a
            # cross-object rule. An independent review found that this checked
            # only for a readable project identity, so prose could be drafted
            # from a claim in a capsule that does not validate -- caught
            # afterwards by `deterministic_check`, but only after the draft
            # existed as an artifact.
            return ()
        by_id = {str(obj.id): obj for obj in report.objects}
        project_id = str(report.project.id)
        quotable = []
        for obj in report.objects:
            if not isinstance(obj, Claim) or obj.status is not ClaimStatus.ACCEPTED:
                continue
            if claim_approval(obj, by_id, project_id).qualifies:
                quotable.append(str(obj.id))
        return tuple(quotable)

    # -------------------------------------------------------------- frontier --
    def frontier(self) -> Frontier:
        """Derive the unresolved frontier from the files, deterministically."""

        report = self.validate()
        if report.project is None:
            raise CapsuleError(f"{self._repo}: no valid project identity")
        project_id = str(report.project.id)
        by_id = {str(obj.id): obj for obj in report.objects}

        open_questions: list[str] = []
        actionable: list[str] = []
        untested: list[str] = []
        awaiting_review: list[str] = []
        stale_review: list[str] = []
        contested: list[str] = []
        gaps: list[str] = []
        pending: list[str] = []

        hypotheses_by_question: dict[str, list[str]] = {}
        experiments_by_hypothesis: dict[str, list[str]] = {}
        for obj in report.objects:
            if isinstance(obj, Hypothesis):
                for question in obj.addresses or ():
                    hypotheses_by_question.setdefault(str(question), []).append(
                        str(obj.id)
                    )
            if isinstance(obj, Experiment):
                for hypothesis in obj.hypotheses or ():
                    experiments_by_hypothesis.setdefault(str(hypothesis), []).append(
                        str(obj.id)
                    )

        for obj in report.objects:
            object_id = str(obj.id)
            if isinstance(obj, Question) and obj.status is QuestionStatus.OPEN:
                open_questions.append(object_id)
            elif isinstance(obj, Hypothesis):
                if obj.status in UNRESOLVED_HYPOTHESIS_STATUSES:
                    actionable.append(object_id)
                if (
                    obj.status in TESTABLE_HYPOTHESIS_STATUSES
                    and not experiments_by_hypothesis.get(object_id)
                ):
                    untested.append(object_id)
            elif isinstance(obj, Experiment):
                if obj.status is not ExperimentStatus.COMPLETED:
                    pending.append(object_id)
            elif isinstance(obj, Claim):
                if obj.status is ClaimStatus.EVIDENCE_LINKED:
                    awaiting_review.append(object_id)
                if obj.contrary_evidence:
                    contested.append(object_id)
                if obj.status is ClaimStatus.ACCEPTED:
                    verdict = claim_approval(obj, by_id, project_id)
                    if not verdict.qualifies:
                        stale_review.append(object_id)
                if not obj.supporting_evidence:
                    gaps.append(object_id)
            elif isinstance(obj, Evidence) and obj.status is not EvidenceStatus.ACTIVE:
                gaps.append(object_id)

        return Frontier(
            project_id=project_id,
            open_questions=tuple(sorted(open_questions)),
            actionable_hypotheses=tuple(sorted(actionable)),
            hypotheses_without_tests=tuple(sorted(untested)),
            claims_awaiting_review=tuple(sorted(awaiting_review)),
            claims_with_stale_review=tuple(sorted(stale_review)),
            contested_claims=tuple(sorted(contested)),
            evidence_gaps=tuple(sorted(set(gaps))),
            pending_experiments=tuple(sorted(pending)),
            validation_errors=tuple(
                sorted({finding.code for finding in report.errors})
            ),
        )

    # ------------------------------------------------------------- authority --
    def refuse_scientific_authority(self, action: str) -> None:
        """Refuse, loudly, an action only a human may take.

        Exists so that the refusal is one call with one message rather than a
        scattering of ad-hoc raises, and so that the list of refused actions is
        greppable.
        """

        raise ScientificAuthorityError(
            f"{action} requires human scientific authority. The runtime prepares the "
            f"decision; it does not record it. Run `researchctl review` (interactive) "
            f"or answer the pending approval with `researchctl runtime approve`."
        )
