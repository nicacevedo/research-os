"""Shared error, finding, and result primitives for Research OS."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_USAGE = 2

E_DUP_ID = "E_DUP_ID"
E_DANGLING_REF = "E_DANGLING_REF"
E_WRONG_REF_TYPE = "E_WRONG_REF_TYPE"
E_CREATED_FROM_CYCLE = "E_CREATED_FROM_CYCLE"
E_SUPERSESSION_CYCLE = "E_SUPERSESSION_CYCLE"
E_SUPERSEDED_WITHOUT_SUCCESSOR = "E_SUPERSEDED_WITHOUT_SUCCESSOR"
E_SUPERSEDES_NON_SUPERSEDED = "E_SUPERSEDES_NON_SUPERSEDED"
E_WITHDRAWN_SUPERSEDED = "E_WITHDRAWN_SUPERSEDED"
E_NONQUALIFYING_EVIDENCE = "E_NONQUALIFYING_EVIDENCE"
E_ACCEPTED_WITHOUT_EVIDENCE = "E_ACCEPTED_WITHOUT_EVIDENCE"
E_ACCEPTED_WITHOUT_HUMAN_REVIEW = "E_ACCEPTED_WITHOUT_HUMAN_REVIEW"
E_STALE_REVIEW_DIGEST = "E_STALE_REVIEW_DIGEST"
E_EVIDENCE_DIGEST_UNLINKED = "E_EVIDENCE_DIGEST_UNLINKED"
E_EVIDENCE_DIGESTS_INCOMPLETE = "E_EVIDENCE_DIGESTS_INCOMPLETE"
E_EXPERIMENT_DIGEST_UNLINKED = "E_EXPERIMENT_DIGEST_UNLINKED"
E_EXPERIMENT_DIGESTS_INCOMPLETE = "E_EXPERIMENT_DIGESTS_INCOMPLETE"
E_REVIEW_OF_REVIEW = "E_REVIEW_OF_REVIEW"
E_MISSING_CAPSULE_FILE = "E_MISSING_CAPSULE_FILE"
E_YAML_PARSE = "E_YAML_PARSE"
E_DUPLICATE_YAML_KEY = "E_DUPLICATE_YAML_KEY"
E_SCHEMA = "E_SCHEMA"
E_FILENAME_ID_MISMATCH = "E_FILENAME_ID_MISMATCH"
E_WRONG_OBJECT_DIRECTORY = "E_WRONG_OBJECT_DIRECTORY"
E_UNSAFE_PATH = "E_UNSAFE_PATH"
E_UNEXPECTED_FILE = "E_UNEXPECTED_FILE"

W_STALE_SUBJECT_DIGEST = "W_STALE_SUBJECT_DIGEST"
W_STALE_EVIDENCE_DIGEST = "W_STALE_EVIDENCE_DIGEST"
W_STALE_EXPERIMENT_DIGEST = "W_STALE_EXPERIMENT_DIGEST"
W_PROMOTED_WITHOUT_HYPOTHESIS = "W_PROMOTED_WITHOUT_HYPOTHESIS"
W_RESERVED_DIRECTORY = "W_RESERVED_DIRECTORY"
W_UNEXPECTED_FILE = "W_UNEXPECTED_FILE"


class ResearchOSError(Exception):
    """Base exception for Research OS kernel failures."""


class InvalidIdError(ResearchOSError, ValueError):
    """Raised when a scientific or project identifier is malformed."""


class NotAGitRepositoryError(ResearchOSError):
    """Raised when a path is not inside a Git repository."""


class CapsuleExistsError(ResearchOSError):
    """Raised when init-project would overwrite an existing capsule."""


class ProjectIdRequiredError(ResearchOSError):
    """Raised when a repository basename is not a valid project id."""


class CapsuleError(ResearchOSError):
    """Raised for operational capsule/project failures."""


class RegistryError(ResearchOSError):
    """Raised for project-registry operational failures."""


class RegistryConflictError(RegistryError):
    """Raised when registration would collide with a live identity or path."""


class CapsuleCreatedRegistryFailedError(ResearchOSError):
    """Raised when init created a capsule but could not register it."""


class ReviewBlockedError(ResearchOSError):
    """Raised when a capsule is not in a state where a review may be recorded.

    Carries the findings that block the review so the caller can render them
    with the ordinary validation formatter instead of restating them.
    """

    def __init__(self, message: str, findings: tuple[Finding, ...] = ()) -> None:
        super().__init__(message)
        self.findings = findings


class AutomationError(ResearchOSError):
    """Base class for automation control-plane failures.

    Runtime automation is not scientific state, so these never indicate a
    corrupt capsule. They are raised, reported, and recorded in the run ledger.
    """


class PreflightError(AutomationError):
    """Raised when a project is not in a state an automation run may start from."""


class GitError(AutomationError):
    """Raised when a deterministic Git inspection or worktree command fails."""


class RunStoreError(AutomationError):
    """Raised when the runtime run store cannot be read or written."""


class RunNotFoundError(AutomationError):
    """Raised when a run id names no run directory."""


class RunStateError(AutomationError):
    """Raised on an undeclared run-state transition."""


class BudgetExceededError(AutomationError):
    """Raised before an invocation that would exceed a declared run budget."""


class ProviderUnavailableError(AutomationError):
    """Raised when a configured provider is not usable on this machine."""


class ProviderInvocationError(AutomationError):
    """Raised when a provider ran but produced no usable result."""


class PlanValidationError(AutomationError):
    """Raised when planner output is not a valid, in-policy work plan."""


class CommandPolicyError(AutomationError):
    """Raised when a planner-originated acceptance command is not authorised.

    The controller executes acceptance commands itself, so which command shapes
    a plan may name is policy. Raised before the argument vector reaches
    ``subprocess.run``, never after.
    """


class WorktreeError(AutomationError):
    """Raised when an isolated automation worktree cannot be created or removed."""


class WorktreeIsolationError(WorktreeError):
    """Raised when a write-enabled worker would run outside its own worktree.

    An enforced invariant rather than a prompt instruction: the controller
    checks the resolved working directory before every write invocation.
    """


class SymlinkScopeError(WorktreeIsolationError):
    """Raised when a symlink in the worktree resolves outside it.

    A writer told to change an in-scope path writes through whatever that path
    is. If the path is a symlink out of the worktree, the write lands outside
    the isolation boundary and Git never sees it, so the run is refused before
    the writer is invoked.
    """


class AnalystOutputError(AutomationError):
    """Raised when an analysis worker's structured findings are not usable.

    Analyst output is data the controller parses, never instruction it follows,
    so a report that does not validate is a failed work order rather than
    something to interpret generously.
    """


class SnapshotMutationError(AutomationError):
    """Raised when a snapshot-read worker changed the checkout it was reading.

    The analysis worker is given read-only file tools and a pinned snapshot, so
    a changed HEAD or a dirty tree means an enforcement boundary did not hold.
    The run fails and the violation is recorded; nothing is quietly restored.
    """


class PromptDataError(AutomationError):
    """Raised when model-originated text would forge a prompt data boundary.

    The controller renders every model-originated string through one prompt-safe
    serializer, so this is a programming error rather than a hostile input: it
    means a field reached a data block without passing that boundary. It fails
    closed, because a prompt whose fence is ambiguous has already lost the
    distinction between data and instruction.
    """


class LiteratureError(ResearchOSError):
    """Base class for literature-subsystem failures.

    The literature index is shared, rebuildable infrastructure, not scientific
    truth, so none of these ever indicates a corrupt capsule. They are reported
    and, where a run is involved, recorded in its ledger.
    """


class LiteratureStoreError(LiteratureError):
    """Raised when the shared scholarly store cannot be opened, read, or written."""


class SourceUnavailableError(LiteratureError):
    """Raised when a literature provider cannot be used on this machine.

    A missing credential, an unreachable host, and an exhausted rate budget are
    all this: the provider is not usable right now. It is a reportable state
    rather than a crash, so the rest of a retrieval can continue and say plainly
    which source was skipped.
    """


class ExtractionError(LiteratureError):
    """Raised when local text extraction from a stored file cannot be completed."""


class ProposalError(ResearchOSError):
    """Base class for scientific-proposal failures.

    A proposal is runtime state, not science, so none of these ever indicates a
    corrupt capsule. The one that touches a capsule -- promotion -- raises
    ``CapsuleError`` for filesystem problems and these for everything it refuses.
    """


class ProposalValidationError(ProposalError):
    """Raised when proposal output is not a usable, grounded proposal.

    Fail-closed. A proposal is what a researcher decides from, so output that
    does not validate is a failed task rather than something to interpret
    generously -- and a proposal citing something this run never had is refused
    outright rather than trimmed.
    """


class ProposalStoreError(ProposalError):
    """Raised when the proposal store cannot be read or written."""


class ProposalNotFoundError(ProposalError):
    """Raised when a proposal id names no proposal directory."""


class PromotionRefusedError(ProposalError):
    """Raised when a promotion would cross a boundary only a human may cross.

    Never raised because a proposal was poor. It is raised when something other
    than an interactive human asked for scientific state to be written, or when
    the promotion would produce something stronger than a draft.
    """


class ExperimentError(ResearchOSError):
    """Base class for experiment-execution failures.

    An execution record is runtime state, never science, so none of these means
    a corrupt capsule. What they mean is that something was refused, could not
    be reached, or did not produce what it said it would.
    """


class ExperimentConfigError(ExperimentError):
    """Raised when experiment configuration is missing, invalid, or silent.

    Also raised for the ordinary case of a project with no declared commands.
    That is not a malfunction: a command nobody declared cannot be run, and
    saying so plainly is the whole point of declaring them.
    """


class ExperimentSpecError(ExperimentError):
    """Raised when a command cannot be built from its declaration and values.

    Refusal, never repair. A supplied value that does not fit its declared type
    is rejected rather than quoted or escaped, because escaping would mean
    guessing what the caller meant -- and the caller may be a model.
    """


class ExperimentAuthorizationError(ExperimentError):
    """Raised when an execution is not authorised to spend what it would spend.

    An experiment costs real time and sometimes real money. A missing explicit
    authorisation, an exhausted submission budget, and a partition outside the
    allowlist are all this, and each says which one it was.
    """


class ExperimentIngestError(ExperimentError):
    """Raised when what an execution produced cannot be read or hashed."""


class ExperimentStoreError(ExperimentError):
    """Raised when the experiment run store cannot be read or written."""


class ExperimentRunNotFoundError(ExperimentError):
    """Raised when an experiment run id names no run directory."""


class SchedulerUnavailableError(ExperimentError):
    """Raised when a scheduler cannot be reached or refuses a submission.

    Reported rather than worked around. A machine with no ``sbatch`` is not a
    submit host, and pretending a job was queued would be worse than saying so.
    """


class InsightError(ResearchOSError):
    """Base class for cross-project insight failures.

    An insight is durable shared knowledge but never scientific truth: it holds
    nobody's Claim and rests on nobody's Evidence. These failures are therefore
    operational, and none of them indicates a corrupt capsule.
    """


class InsightStoreError(InsightError):
    """Raised when the insight corpus cannot be read or written."""


class InsightNotFoundError(InsightError):
    """Raised when an insight or nomination id names nothing."""


class InsightPromotionRefusedError(InsightError):
    """Raised when something other than a human tried to promote an insight.

    Promotion is the only operation that puts knowledge where another project's
    workers will read it. An agent may nominate; the crossing is a human act.
    """


class PaperError(ResearchOSError):
    """Base class for manuscript-writing failures.

    A draft is prose about science, never science. None of these means a corrupt
    capsule, and none of them means anything was accepted: a writing task ends
    with a diff on a branch that a human reads.
    """


class PaperPacketError(PaperError):
    """Raised when the sources a writing task would use cannot be assembled.

    Most often because a Claim was asked for that is not accepted, or whose
    human approval no longer binds its current evidence. That is a refusal
    rather than a malfunction: a manuscript may only state what the project has
    actually accepted, as it stands now.
    """


class PaperManifestError(PaperError):
    """Raised when a writer produced no usable record of what it wrote from.

    Fail-closed. Without a manifest the prose cannot be checked against
    anything, so prose with no provenance record is an incomplete task rather
    than a task with a missing extra.
    """


class PaperWritingError(PaperError):
    """Raised when a writing task is refused or its writer left its scope."""


class PaperStoreError(PaperError):
    """Raised when the draft store cannot be read or written."""


class DraftNotFoundError(PaperError):
    """Raised when a draft id names no draft directory."""


class ResearchError(ResearchOSError):
    """Base class for unified research-orchestration failures.

    A research run is the layer above every other controller: it decides what
    runs, in what order, against which budget, and when to stop for a person. It
    owns no worker and writes nothing scientific, so none of these means the
    capsule is wrong -- they mean a run could not be planned, could not be
    dispatched, or was asked to do something it is not allowed to do.
    """


class ResearchPlanError(ResearchError):
    """Raised when a research plan is absent, malformed, or not executable.

    Includes the case that matters most: a plan naming an experiment command the
    researcher never declared. That is refused at planning time rather than at
    the moment of spending, so the failure costs nothing.
    """


class ResearchStateError(ResearchError):
    """Raised when a research run is asked for a transition it cannot make."""


class ResearchStoreError(ResearchError):
    """Raised when a research run directory cannot be read or written."""


class ResearchRunNotFoundError(ResearchError):
    """Raised when a run id names no research run directory."""


class Severity(StrEnum):
    """Finding severity for deterministic validation reports."""

    ERROR = "ERROR"
    WARNING = "WARNING"
    INFO = "INFO"


@dataclass(frozen=True, slots=True, kw_only=True)
class Finding:
    """One deterministic validation finding.

    M3 formats these; M2 only produces structured data.
    """

    severity: Severity
    code: str
    message: str
    object_id: str | None = None
    field: str | None = None
    reference: str | None = None
    source: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationReport:
    """Deterministic collection of validation findings."""

    findings: tuple[Finding, ...] = field(default_factory=tuple)

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(
            item for item in self.findings if item.severity is Severity.WARNING
        )

    @property
    def infos(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.INFO)

    @property
    def ok(self) -> bool:
        return not self.errors

    def codes(self) -> tuple[str, ...]:
        return tuple(item.code for item in self.findings)
