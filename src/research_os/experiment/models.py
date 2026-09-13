"""Runtime records for experiment execution.

Nothing here is a scientific Experiment. R0's ``Experiment`` is a capsule object
a human wrote: a purpose, hypotheses, predictions made before the result was
known. What these models record is that a process ran -- which command, from
which commit, on which executor, producing which bytes.

The two are connected by exactly one thing and deliberately no more: an
execution can produce a **candidate** evidence packet, which a human may read
and, if they agree with it, turn into capsule Evidence themselves. Nothing here
creates Evidence, and nothing here marks anything accepted.

The other rule these models hold is about honesty of measurement. A field that
the executor could not observe is ``None`` and renders as "unknown". Peak memory
on a local run, a node's CPU-seconds without a scheduler to ask, an exit code
for a job that is still queued -- none of those are estimated, because a run
report that guesses is worse than one that admits it does not know.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from research_os.automation.models import COMMIT_RE, SHA256_RE, utc_now
from research_os.models import NonBlankStr

EXPERIMENT_RUN_ID_RE = re.compile(r"^XRUN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
TASK_NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")


class ExecutorKind(StrEnum):
    """Where an experiment actually ran."""

    LOCAL = "local"
    SLURM = "slurm"
    SLURM_SSH = "slurm_ssh"


class ExecutionState(StrEnum):
    """The deterministic lifecycle of one experiment execution.

    Every one of these is assigned by the controller from something it observed:
    a subprocess exit code, a scheduler's own answer, a timeout it enforced.
    ``UNKNOWN`` is a real state, not a placeholder -- a scheduler that stops
    answering about a job leaves it genuinely unknown, and pretending otherwise
    would make a lost job look finished.
    """

    PREPARING = "preparing"
    """Recorded, but its isolated worktree does not exist yet.

    The two-phase window that makes worktree creation crash-consistent. Creating
    a Git worktree is an irreversible side effect in the *project* repository;
    writing the record first means a process killed between the two leaves a
    worktree whose owner is on disk and findable, instead of a directory and a
    registration nothing in the store has ever heard of. A run seen in this state
    is always a crash: the controller either moves it to PREPARED or fails it.
    """

    PREPARED = "prepared"
    SUBMITTED = "submitted"
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMED_OUT = "timed_out"
    UNKNOWN = "unknown"


TERMINAL_STATES: frozenset[ExecutionState] = frozenset(
    {
        ExecutionState.COMPLETED,
        ExecutionState.FAILED,
        ExecutionState.CANCELLED,
        ExecutionState.TIMED_OUT,
    }
)

#: States in which a job is still the scheduler's problem rather than ours.
ACTIVE_STATES: frozenset[ExecutionState] = frozenset(
    {ExecutionState.SUBMITTED, ExecutionState.PENDING, ExecutionState.RUNNING}
)


class ParameterType(StrEnum):
    """The value kinds an experiment parameter may have.

    Deliberately few. Each one has a validation rule that can refuse a hostile
    value outright, which is the point: a parameter is the only thing a plan
    supplies to a command, so the set of things it can be is the set of things
    that can reach a subprocess.
    """

    INTEGER = "integer"
    NUMBER = "number"
    TOKEN = "token"
    CHOICE = "choice"
    PATH = "path"
    FLAG = "flag"


class ParameterSpec(BaseModel):
    """One declared parameter of one experiment command.

    Declared by the researcher in their own configuration, outside every
    worktree. A plan may supply a *value* for it; nothing a plan or a worker
    says can add a parameter, widen its range, or change its type.
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    type: ParameterType
    description: str = ""
    required: bool = False
    default: str | int | float | bool | None = None
    minimum: float | None = None
    maximum: float | None = None
    choices: list[str] = Field(default_factory=list)

    @field_validator("name")
    @classmethod
    def _name_shape(cls, value: str) -> str:
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,31}", value):
            raise ValueError(
                "a parameter name must be 1-32 lowercase letters, digits, or "
                "underscores, starting with a letter"
            )
        return value

    @model_validator(mode="after")
    def _choices_are_declared_for_choice_parameters(self) -> Self:
        if self.type is ParameterType.CHOICE and not self.choices:
            raise ValueError(
                f"parameter {self.name!r} is a choice but declares no choices, so "
                "no value could ever be valid"
            )
        if self.type is not ParameterType.CHOICE and self.choices:
            raise ValueError(
                f"parameter {self.name!r} declares choices but is not a choice "
                "parameter; the type decides how a value is checked"
            )
        return self


class ArtifactRecord(BaseModel):
    """One file an execution produced, identified by its content."""

    model_config = ConfigDict(extra="forbid")

    path: NonBlankStr
    sha256: str
    byte_size: int = Field(ge=0)
    media_type: str = ""
    declared: bool = True
    """Whether the command specification named this output in advance.

    Declaring outputs beforehand is what makes "the experiment produced what it
    said it would" checkable. An undeclared file that appeared is recorded too,
    marked, because a run that quietly wrote somewhere unexpected is exactly
    what a reader needs to see.
    """

    @field_validator("sha256")
    @classmethod
    def _digest_shape(cls, value: str) -> str:
        if SHA256_RE.fullmatch(value) is None:
            raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
        return value


class ResourceUsage(BaseModel):
    """What the execution actually cost, where that could be observed.

    Every field is optional and none is ever estimated. A local run has no
    scheduler to ask for peak memory, so ``max_rss_kb`` stays ``None`` and the
    report says "unknown" rather than inventing a number that would later be
    quoted as a measurement.
    """

    model_config = ConfigDict(extra="forbid")

    wall_clock_seconds: float | None = None
    cpu_seconds: float | None = None
    max_rss_kb: int | None = None
    node_count: int | None = None
    cpu_count: int | None = None
    gpu_count: int | None = None
    observed_by: str = ""

    @property
    def anything_observed(self) -> bool:
        return any(
            value is not None
            for value in (
                self.wall_clock_seconds,
                self.cpu_seconds,
                self.max_rss_kb,
                self.node_count,
                self.cpu_count,
                self.gpu_count,
            )
        )


class SchedulerRecord(BaseModel):
    """What a scheduler said about one job, in the scheduler's own words."""

    model_config = ConfigDict(extra="forbid")

    job_id: str
    partition: str | None = None
    account: str | None = None
    submitted_at: str | None = None
    raw_state: str = ""
    """The scheduler's own state string, kept verbatim.

    Mapped onto :class:`ExecutionState` for the controller, and kept unmapped
    here because a mapping is an interpretation and the raw answer is the fact.
    """

    exit_code: int | None = None
    signal: int | None = None
    reason: str = ""
    polled_at: str | None = None
    host: str | None = None


class ExperimentRun(BaseModel):
    """The complete runtime record of one experiment execution."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    run_id: str
    task_name: str
    project_id: str | None = None
    project_path: NonBlankStr
    base_commit: str | None = None
    worktree_path: str | None = None
    """Where this experiment actually ran.

    An isolated Git worktree, not the researcher's checkout. Recorded because a
    result nobody can locate is not a result, and because the artifacts below
    are identified by content hash *and* by path within it.
    """

    branch: str | None = None
    isolated: bool = True
    """Whether the execution was isolated from the canonical checkout.

    False only when a caller supplied its own directory, which the CLI allows
    deliberately and the record then states plainly. An independent reviewer
    found every caller doing that by default; the default is now isolation.
    """

    executor: ExecutorKind
    argv: list[NonBlankStr] = Field(min_length=1)
    parameters: dict[str, str] = Field(default_factory=dict)
    environment_note: str = ""
    declared_outputs: list[str] = Field(default_factory=list)
    timeout_seconds: int = Field(ge=1)
    created_at: str = Field(default_factory=utc_now)
    started_at: str | None = None
    ended_at: str | None = None
    state: ExecutionState = ExecutionState.PREPARED
    exit_code: int | None = None
    failure_reason: str | None = None
    stdout_path: str | None = None
    stderr_path: str | None = None
    script_path: str | None = None
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    scheduler: SchedulerRecord | None = None
    config_digest: str | None = None
    authorized_by: str = ""
    """How this execution was authorised, recorded rather than assumed.

    An experiment costs real time and sometimes real money, so a run says which
    explicit authorisation let it happen: the ``--execute`` flag, a configured
    allowance, or a dry run that never spent anything.
    """

    @field_validator("run_id")
    @classmethod
    def _run_id_shape(cls, value: str) -> str:
        if EXPERIMENT_RUN_ID_RE.fullmatch(value) is None:
            raise ValueError("run_id must look like XRUN-20260912T101500Z-0a1b2c3d")
        return value

    @field_validator("task_name")
    @classmethod
    def _task_name_shape(cls, value: str) -> str:
        if TASK_NAME_RE.fullmatch(value) is None:
            raise ValueError(
                "task_name must be 1-64 lowercase letters, digits, or hyphens, "
                "starting with a letter"
            )
        return value

    @field_validator("base_commit")
    @classmethod
    def _full_commit(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if COMMIT_RE.fullmatch(value) is None:
            raise ValueError("base_commit must be a full 40-character commit sha")
        return value

    @property
    def terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    @property
    def succeeded(self) -> bool:
        return self.state is ExecutionState.COMPLETED and self.exit_code == 0

    @property
    def missing_outputs(self) -> list[str]:
        """Return declared outputs the execution did not actually produce."""

        produced = {item.path for item in self.artifacts}
        return [item for item in self.declared_outputs if item not in produced]

    @property
    def undeclared_artifacts(self) -> list[str]:
        return [item.path for item in self.artifacts if not item.declared]


class CheckOutcome(BaseModel):
    """One deterministic postprocessing check the controller ran on the results."""

    model_config = ConfigDict(extra="forbid")

    name: NonBlankStr
    passed: bool
    detail: str = ""


class EvidencePacket(BaseModel):
    """A candidate reading of one execution's results. Never accepted Evidence.

    The word "candidate" is load-bearing. R0's ``Evidence`` is a capsule object
    that a Claim may rest on and a human Review binds by digest. This is a
    structured summary of what a process produced, assembled deterministically,
    for a human to read before deciding whether any of it is Evidence at all.

    It carries no verdict about the hypothesis. Whether the results support or
    contradict anything is a scientific judgement, and the packet's job is to
    put the facts -- the command, the commit, the digests, the checks -- in
    front of the person making it.
    """

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    packet_id: str
    run_id: str
    task_name: str
    project_id: str | None = None
    project_path: str
    base_commit: str | None = None
    executor: ExecutorKind
    argv: list[str] = Field(default_factory=list)
    parameters: dict[str, str] = Field(default_factory=dict)
    state: ExecutionState
    exit_code: int | None = None
    artifacts: list[ArtifactRecord] = Field(default_factory=list)
    missing_outputs: list[str] = Field(default_factory=list)
    checks: list[CheckOutcome] = Field(default_factory=list)
    usage: ResourceUsage = Field(default_factory=ResourceUsage)
    created_at: str = Field(default_factory=utc_now)
    notes: list[str] = Field(default_factory=list)

    @property
    def usable(self) -> bool:
        """Whether this packet is worth a human's time as a basis for Evidence.

        Not a scientific judgement. It answers the mechanical question: did the
        process finish successfully, produce everything it declared, and pass
        every deterministic check? A packet that fails this is still recorded
        and still shown; it simply must not be read as a result.
        """

        return (
            self.state is ExecutionState.COMPLETED
            and self.exit_code == 0
            and not self.missing_outputs
            and all(item.passed for item in self.checks)
        )

    @property
    def blocking_notes(self) -> list[str]:
        """Return why this packet is not usable, in the order a reader needs."""

        reasons: list[str] = []
        if self.state is not ExecutionState.COMPLETED:
            reasons.append(f"the execution ended {self.state}, not completed")
        elif self.exit_code not in (0, None):
            reasons.append(f"the command exited {self.exit_code}")
        if self.missing_outputs:
            reasons.append(
                "declared outputs were not produced: " + ", ".join(self.missing_outputs)
            )
        failed = [item.name for item in self.checks if not item.passed]
        if failed:
            reasons.append("checks failed: " + ", ".join(failed))
        return reasons
