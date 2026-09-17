"""The capability interfaces the runtime's reasoning layer is allowed to know about.

Deliberately few. An interface earns its place here only by isolating a real
external or operational concern that the layer above must not know the shape of.
Where the v1 layers already define a good contract it is *reused*, not wrapped:
:class:`research_os.literature.sources.base.SourceAdapter` is already the
literature-source interface and is re-exported here rather than duplicated, and
:class:`research_os.automation.providers.ProviderAdapter` is already how a
coding provider is invoked.

What the runtime adds is the layer above those: a *typed model request* carrying
role, criticality and an independence requirement, so a graph node can ask for
"the strongest independent reviewer available" without naming a vendor, and so
routing has something to route on. That is §20's requirement and it is not
expressible in the v1 adapter contract, which takes a role and a provider that
someone else already chose.

Three rules hold across this module, and they are what
``tests/test_runtime_layering.py`` checks:

- a graph node never names a provider;
- a graph node never constructs a shell command for a scheduler;
- experiment logic never knows the artifact store's directory layout.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from research_os.literature.sources.base import SourceAdapter, SourceResult

__all__ = [
    "ArtifactRef",
    "ArtifactStore",
    "Capability",
    "Criticality",
    "ExecutionHandle",
    "ExecutionSpec",
    "Executor",
    "Independence",
    "ModelProvider",
    "ModelRequest",
    "ModelResponse",
    "ModelRole",
    "Notifier",
    "SourceAdapter",
    "SourceResult",
    "WorktreeHandle",
    "WorktreeManager",
]


# ------------------------------------------------------------------ models --
class ModelRole(StrEnum):
    """What a model is being asked to be, for routing and for provenance.

    Distinct from :class:`research_os.automation.models.Role`, which is the
    five roles the v1 coding pipeline dispatches. These are the runtime's, they
    include the scientific ones the coding pipeline has no concept of, and a
    call's role is recorded so "who reviewed this" is answerable later.
    """

    PLANNER = "planner"
    BLIND_EXPLORER = "blind_explorer"
    SEEDED_EXPLORER = "seeded_explorer"
    SKEPTIC = "skeptic"
    EXPERIMENTALIST = "experimentalist"
    SCIENTIFIC_REVIEWER = "scientific_reviewer"
    CODE_REVIEWER = "code_reviewer"
    AUTHOR = "author"
    REFEREE = "referee"
    EXTRACTOR = "extractor"
    FRONTIER = "frontier"
    DERIVER = "deriver"
    """Derives, or fails to derive, a mathematical proposition from stated
    assumptions.

    Its own role because the question it is asked has a different acceptance
    condition from every other role's. An explorer is useful when it is
    interesting and a reviewer is useful when it is sharp; a deriver is useful
    only when each step follows, and "I could not derive this" is a complete
    and valuable answer from it where it would be a failure from the others.

    What it is never asked for is a measurement. The whole reason this role
    exists is that ``design_experiment`` was being asked to settle propositions
    no experiment can settle.
    """

    NOMINATOR = "nominator"
    """Judges whether one project's finding is candidate knowledge for others.

    Its own role rather than a second use of the skeptic, because provenance
    has to be able to say which question was asked. "A skeptic said this
    transfers" and "a nominator said this is worth a person's attention" are
    different claims, and only the second was made.
    """


class Capability(StrEnum):
    """What kind of thinking the request needs.

    The routing axis that is not "how important is this". A cheap model is the
    right answer for structured extraction from a paper abstract however
    critical the surrounding work is, and the strongest available model is the
    right answer for a scientific review however cheap the project is.
    """

    STRUCTURED_EXTRACTION = "structured_extraction"
    PLANNING = "planning"
    SYNTHESIS = "synthesis"
    CRITIQUE = "critique"
    CODING = "coding"


class Criticality(StrEnum):
    """How much it matters that this call is good.

    ``CRITICAL`` work is never silently routed below the configured capability
    threshold. If the capable provider is unavailable the request fails and says
    so, rather than being answered by something cheaper without comment.
    """

    ROUTINE = "routine"
    NORMAL = "normal"
    CRITICAL = "critical"


class Independence(StrEnum):
    """What separation the caller requires from the producer of the work.

    Requested, then *reported*. The runtime never claims an independence it did
    not achieve: a review that had to run on the producer's own model family is
    recorded as ``SAME_FAMILY`` and the degradation is visible in the run
    report, because a false claim of independent review is worse than an
    acknowledged absence of one.
    """

    NONE = "none"
    DIFFERENT_CONTEXT = "different_context"
    DIFFERENT_MODEL = "different_model"
    DIFFERENT_FAMILY = "different_family"


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    """A reference to bytes that live in the artifact store.

    What travels in LangGraph state, in prompts and in model requests. The bytes
    themselves never do: a checkpoint containing a PDF is a checkpoint table
    that grows without bound.
    """

    artifact_id: str
    media_type: str = "application/octet-stream"
    role: str | None = None
    size_bytes: int | None = None

    def __str__(self) -> str:
        return self.artifact_id


@dataclass(frozen=True, slots=True)
class ModelRequest:
    """One typed request for model work.

    Note what is *not* here: a provider, and a model name. A graph node that
    names either has hard-coded model identity into graph semantics, which §20
    forbids and ``tests/test_runtime_layering.py`` checks for.
    """

    role: ModelRole
    capability: Capability
    prompt: str
    prompt_version: str
    criticality: Criticality = Criticality.NORMAL
    independence: Independence = Independence.DIFFERENT_CONTEXT
    #: The group whose members must not be answered by the same model. Reviews
    #: of one object share a group with the thing they review.
    independence_group: str | None = None
    #: Artifact references the prompt refers to. Kept structured so provenance
    #: can record exactly which inputs produced which output.
    context_refs: tuple[ArtifactRef, ...] = ()
    json_schema: Mapping[str, Any] | None = None
    max_cost_usd: float | None = None
    timeout_seconds: int = 600


@dataclass(frozen=True, slots=True)
class ModelResponse:
    """What a provider returned, plus what it cost and how independent it was."""

    provider: str
    model: str | None
    text: str | None = None
    structured: Mapping[str, Any] | None = None
    tokens_in: int | None = None
    tokens_out: int | None = None
    cost_usd: float | None = None
    latency_ms: int | None = None
    #: The independence actually achieved, which may be weaker than requested.
    independence: Independence = Independence.NONE
    independence_note: str = ""
    error: str | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


@runtime_checkable
class ModelProvider(Protocol):
    """A router, from the graph's point of view.

    One method, because a graph node has exactly one thing to say: here is what
    I need thought about, and how good it has to be. Choosing the vendor,
    recording the provenance and enforcing the budget happen behind it.
    """

    def complete(self, request: ModelRequest) -> ModelResponse: ...


# ---------------------------------------------------------------- execution --
@dataclass(frozen=True, slots=True)
class ExecutionSpec:
    """What to run, where, with what, and what counts as its output.

    The whole point of this type is that a scientific graph node submits *this*
    and never a shell command. Nothing here is Slurm-shaped: ``resources`` is a
    neutral mapping that the Slurm executor turns into ``#SBATCH`` lines and the
    local executor mostly ignores.
    """

    name: str
    argv: tuple[str, ...]
    cwd: str
    #: Environment identity, not environment contents: ``{"kind": "uv",
    #: "project": "..."}`` or ``{"kind": "conda", "name": "..."}``. Reproducible
    #: execution is the requirement; one packaging technology is not.
    environment: Mapping[str, str] = field(default_factory=dict)
    resources: Mapping[str, str] = field(default_factory=dict)
    env: Mapping[str, str] = field(default_factory=dict)
    timeout_seconds: int = 3600
    #: Paths, relative to the run directory, that the run is expected to write.
    outputs: tuple[str, ...] = ()
    seeds: tuple[int, ...] = ()


@dataclass(frozen=True, slots=True)
class ExecutionHandle:
    """What a submission returns, immediately, without waiting.

    ``scheduler_job_id`` is ``None`` for a local execution that has already
    finished. Either way the caller does not block: the control plane
    reconciles.
    """

    executor: str
    run_dir: str
    spec_digest: str
    scheduler_job_id: str | None = None
    finished: bool = False
    exit_code: int | None = None
    detail: str = ""
    contained: bool = False
    """Whether OS-level containment was in force for this execution.

    Its own field rather than a sentence appended to ``detail``. ``detail``
    carries the executor's or the scheduler's own word for what happened -- it
    is asserted exactly by several tests and read by a person -- and mixing a
    second fact into it makes both harder to use.
    """

    containment: str = ""
    """The technology, or the reason there was none."""


@runtime_checkable
class Executor(Protocol):
    """Somewhere work can run.

    ``submit`` must not block on the work. ``poll`` is how state is discovered,
    and is called by the control plane rather than by the graph, so a cluster
    job that takes two days does not hold a worker or a transaction.
    """

    name: str

    def submit(self, spec: ExecutionSpec, *, run_dir: Path) -> ExecutionHandle: ...

    def poll(self, handle: ExecutionHandle) -> ExecutionHandle: ...

    def cancel(self, handle: ExecutionHandle) -> None: ...


# ---------------------------------------------------------------- artifacts --
@runtime_checkable
class ArtifactStore(Protocol):
    """Immutable bytes, addressed by content.

    Experiment logic, literature acquisition and model provenance all put bytes
    here and pass around the reference. None of them knows where the bytes are,
    which is what lets the store change without touching any of them.
    """

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        role: str | None = None,
        producer: str | None = None,
        source: str | None = None,
    ) -> ArtifactRef: ...

    def put_file(
        self,
        path: Path,
        *,
        media_type: str | None = None,
        role: str | None = None,
        producer: str | None = None,
        source: str | None = None,
    ) -> ArtifactRef: ...

    def get_bytes(self, artifact_id: str) -> bytes: ...

    def path_for(self, artifact_id: str) -> Path: ...

    def exists(self, artifact_id: str) -> bool: ...


# ----------------------------------------------------------- version control --
@dataclass(frozen=True, slots=True)
class WorktreeHandle:
    path: Path
    branch: str
    base_commit: str
    repository: Path


@runtime_checkable
class WorktreeManager(Protocol):
    """Isolated checkouts for autonomous code change.

    Every autonomous modification happens in one of these, off a frozen base
    commit, and never in the canonical checkout.
    """

    def create(
        self, *, repository: Path, base_commit: str, run_id: str, task_id: str
    ) -> WorktreeHandle: ...

    def release(self, handle: WorktreeHandle) -> None: ...

    def diff(self, handle: WorktreeHandle) -> str: ...

    def changed_paths(self, handle: WorktreeHandle) -> tuple[str, ...]: ...


# ------------------------------------------------------------- notification --
@runtime_checkable
class Notifier(Protocol):
    """How a human is told that their attention is genuinely required.

    Not a logging sink. This is called when a run has stopped for an `A2`
    decision or a fatal condition, which for a single-researcher deployment is
    a handful of times per project rather than continuously.
    """

    def notify(
        self,
        *,
        subject: str,
        body: str,
        run_id: str | None = None,
        urgent: bool = False,
    ) -> None: ...


def describe_sequence(values: Sequence[object]) -> str:
    """Render a sequence for a log line without pretending it is short."""

    rendered = ", ".join(str(value) for value in values[:8])
    return rendered if len(values) <= 8 else f"{rendered}, ... ({len(values)} total)"
