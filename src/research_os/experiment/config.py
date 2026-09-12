"""Experiment configuration: what may run, where, and how much of it.

One file, at ``~/.config/research-os/experiments.yaml``, and its location is a
security property rather than a convenience. It lives under the researcher's
config home, which is **outside every automation worktree**, so a write-enabled
worker confined to a worktree cannot reach it. That is what makes "a model
cannot add an experiment command" true by construction instead of by policy.

Three kinds of thing live here.

**Declared commands**, per project, by name. A plan selects one and fills in
declared parameters; see :mod:`.spec`.

**Scheduler settings**: which partitions are allowed, which account, whether a
cluster is reached locally or over SSH, and to which host. Partitions are an
allowlist and the first entry is the default -- so "prefer the Sloan partitions
over ``mit_normal``" is expressed by listing them in that order rather than by
special-casing a site in the code.

**Limits**: how many jobs one run may submit, how much wall clock it may ask
for, and whether execution is allowed at all without an explicit flag. An
experiment spends real time and sometimes real money, so the default is that
nothing runs until someone says so.

No credential is ever a value here. SSH access is by host alias, resolved by the
researcher's own ``~/.ssh/config`` and agent; this file names the alias and
nothing else.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from research_os.errors import ExperimentConfigError
from research_os.experiment.models import ExecutorKind
from research_os.experiment.spec import CommandSpec
from research_os.paths import config_home

CONFIG_FILENAME = "experiments.yaml"

#: An SSH destination alias. An alias, never a user@host:port with a key path.
#:
#: Resolution belongs to the researcher's own SSH configuration and agent, which
#: is where their credentials already are. Research OS names a host and hands it
#: to ``ssh``; it never learns a secret in order to do so.
SSH_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")

#: A Slurm partition or account name.
SLURM_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

#: A Slurm wall-clock limit, in the forms sbatch documents.
SLURM_TIME_RE = re.compile(
    r"^(\d+|\d+:\d{2}|\d+:\d{2}:\d{2}|\d+-\d+|\d+-\d+:\d{2}|\d+-\d+:\d{2}:\d{2})$"
)

#: How many jobs one run may submit before the controller refuses.
DEFAULT_MAX_SUBMISSIONS = 4

#: The longest wall clock one job may request, in seconds.
DEFAULT_MAX_WALL_CLOCK_SECONDS = 6 * 3600


class SlurmSettings(BaseModel):
    """How this machine reaches a Slurm cluster, and what it may ask for."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    ssh_host: str | None = None
    """The SSH destination alias for a cluster this machine is not part of.

    Unset means the scheduler commands are expected on this machine's own PATH.
    Set means every scheduler command is run as ``ssh <alias> ...``; the alias is
    resolved entirely by the researcher's own SSH configuration.
    """

    partitions: list[str] = Field(default_factory=list)
    """The partitions a job may be submitted to, most preferred first.

    An allowlist and an ordering in one. The first entry is the default, so
    preferring one partition over another is a matter of listing it first --
    there is no site-specific rule in the code deciding that for anyone.
    """

    account: str | None = None
    default_time_limit: str = "01:00:00"
    default_cpus: int | None = Field(default=None, ge=1, le=512)
    default_memory_mb: int | None = Field(default=None, ge=1)
    remote_working_directory: str | None = None

    @field_validator("ssh_host")
    @classmethod
    def _host_is_an_alias(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if SSH_HOST_RE.fullmatch(value) is None:
            raise ValueError(
                "ssh_host must be a plain SSH destination alias. Research OS "
                "hands the alias to ssh and lets your own SSH configuration and "
                "agent resolve it; it never takes a user, a port, a key path, or "
                "any credential"
            )
        return value

    @field_validator("partitions")
    @classmethod
    def _partition_names(cls, value: list[str]) -> list[str]:
        for item in value:
            if SLURM_NAME_RE.fullmatch(item) is None:
                raise ValueError(f"{item!r} is not a usable partition name")
        if len(set(value)) != len(value):
            raise ValueError("partitions must not repeat")
        return value

    @field_validator("account")
    @classmethod
    def _account_name(cls, value: str | None) -> str | None:
        if value is not None and SLURM_NAME_RE.fullmatch(value) is None:
            raise ValueError(f"{value!r} is not a usable account name")
        return value

    @field_validator("default_time_limit")
    @classmethod
    def _time_limit_shape(cls, value: str) -> str:
        if SLURM_TIME_RE.fullmatch(value) is None:
            raise ValueError(
                "a time limit must be one of the forms sbatch accepts: minutes, "
                "MM:SS, HH:MM:SS, D-HH, D-HH:MM, or D-HH:MM:SS"
            )
        return value

    @property
    def default_partition(self) -> str | None:
        return self.partitions[0] if self.partitions else None

    def allows(self, partition: str) -> bool:
        return partition in self.partitions

    @property
    def kind(self) -> ExecutorKind:
        return ExecutorKind.SLURM_SSH if self.ssh_host else ExecutorKind.SLURM


class ExecutionLimits(BaseModel):
    """What one run may spend before the controller refuses.

    Defaults are deliberately small. Someone discovering this feature by running
    it should not be able to fill a cluster queue by accident.
    """

    model_config = ConfigDict(extra="forbid")

    max_submissions_per_run: int = Field(default=DEFAULT_MAX_SUBMISSIONS, ge=0, le=100)
    max_wall_clock_seconds: int = Field(
        default=DEFAULT_MAX_WALL_CLOCK_SECONDS, ge=1, le=7 * 24 * 3600
    )
    max_local_runs_per_run: int = Field(default=8, ge=0, le=100)
    require_explicit_execute: bool = True
    """Whether an execution needs an explicit ``--execute`` to happen at all.

    True by default, and the reason is stated where a reader will see it: a
    literature question and a cluster job should not be able to look the same
    from the outside. Turning this off is a decision, and it is recorded in the
    run.
    """


class ProjectExperiments(BaseModel):
    """Everything declared for one project."""

    model_config = ConfigDict(extra="forbid")

    default_executor: ExecutorKind = ExecutorKind.LOCAL
    commands: dict[str, CommandSpec] = Field(default_factory=dict)
    slurm: SlurmSettings | None = None
    limits: ExecutionLimits | None = None

    @field_validator("commands")
    @classmethod
    def _keys_match_names(cls, value: dict[str, CommandSpec]) -> dict[str, CommandSpec]:
        for key, spec in value.items():
            if key != spec.name:
                raise ValueError(
                    f"command declared under key {key!r} names itself {spec.name!r}; "
                    "a command is addressed by one name"
                )
        return value


class ExperimentDocument(BaseModel):
    """The on-disk shape of ``experiments.yaml``. Every key is optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    slurm: SlurmSettings | None = None
    limits: ExecutionLimits | None = None
    projects: dict[str, ProjectExperiments] = Field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Resolved experiment settings and where they came from."""

    slurm: SlurmSettings
    limits: ExecutionLimits
    projects: dict[str, ProjectExperiments]
    source: Path | None

    def for_project(self, project_id: str | None) -> ProjectExperiments:
        """Return one project's declarations, or an empty set.

        An empty set is the honest answer for a project nobody has configured:
        it has no experiment commands, so nothing can be run for it, and the
        error a caller gets says exactly that.
        """

        if project_id is None:
            return ProjectExperiments()
        return self.projects.get(project_id, ProjectExperiments())

    def command(self, project_id: str | None, name: str) -> CommandSpec:
        """Return one declared command, or explain why there is none."""

        project = self.for_project(project_id)
        spec = project.commands.get(name)
        if spec is not None:
            return spec
        if not project.commands:
            raise ExperimentConfigError(
                f"no experiment commands are declared for project "
                f"{project_id or '(unregistered)'}. Declare them in "
                f"{config_path()} under projects.{project_id or '<project-id>'}"
                ".commands; a command a researcher has not declared cannot be run."
            )
        raise ExperimentConfigError(
            f"project {project_id} declares no experiment command {name!r}. "
            f"Declared: {', '.join(sorted(project.commands))}"
        )

    def slurm_for(self, project_id: str | None) -> SlurmSettings:
        """Return the scheduler settings in force for one project."""

        return self.for_project(project_id).slurm or self.slurm

    def limits_for(self, project_id: str | None) -> ExecutionLimits:
        return self.for_project(project_id).limits or self.limits

    def digest(self) -> str:
        """Return a digest of the resolved configuration.

        Recorded with every execution, so a run that behaved differently from
        another can be traced to the configuration having changed rather than to
        the code.
        """

        payload = {
            "slurm": self.slurm.model_dump(mode="json"),
            "limits": self.limits.model_dump(mode="json"),
            "projects": {
                key: value.model_dump(mode="json")
                for key, value in sorted(self.projects.items())
            },
        }
        return hashlib.sha256(
            json.dumps(payload, sort_keys=True).encode("utf-8")
        ).hexdigest()


def config_path() -> Path:
    return config_home() / CONFIG_FILENAME


def default_config() -> ExperimentConfig:
    """Return the shipped defaults: no commands, no cluster, nothing runs.

    A machine with no configuration can plan and propose experiments and cannot
    execute one, which is the right starting position: running something costly
    should require having said what it is.
    """

    return ExperimentConfig(
        slurm=SlurmSettings(),
        limits=ExecutionLimits(),
        projects={},
        source=None,
    )


def load_config(path: Path | None = None) -> ExperimentConfig:
    """Load experiment configuration, falling back to the shipped defaults."""

    target = path if path is not None else config_path()
    if not target.is_file():
        if path is not None:
            raise ExperimentConfigError(f"no experiment config at {target}")
        return default_config()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise ExperimentConfigError(f"cannot read {target}: {exc}") from exc
    try:
        data = yaml.safe_load(raw) or {}
    except yaml.YAMLError as exc:
        raise ExperimentConfigError(f"invalid YAML in {target}: {exc}") from exc
    if not isinstance(data, dict):
        raise ExperimentConfigError(f"{target} must contain a mapping")
    try:
        document = ExperimentDocument.model_validate(data)
    except ValidationError as exc:
        raise ExperimentConfigError(
            f"invalid experiment config at {target}: {exc}"
        ) from exc
    if document.schema_version != 1:
        raise ExperimentConfigError(
            f"{target} declares schema_version {document.schema_version}; this "
            "build understands 1"
        )
    return ExperimentConfig(
        slurm=document.slurm or SlurmSettings(),
        limits=document.limits or ExecutionLimits(),
        projects=document.projects,
        source=target,
    )


EXAMPLE_CONFIG = """\
# ~/.config/research-os/experiments.yaml
#
# This file lives outside every automation worktree on purpose: a worker
# confined to a worktree cannot reach it, so it cannot add a command or widen
# one. Everything an experiment may run is declared here, by you.

schema_version: 1

limits:
  max_submissions_per_run: 4
  max_wall_clock_seconds: 21600
  require_explicit_execute: true

slurm:
  enabled: false
  # ssh_host: cluster-login          # an alias from your own ~/.ssh/config
  # Most preferred partition first. The first entry is the default, which is
  # how you express "prefer these over mit_normal".
  partitions: []
  # account: my-account
  default_time_limit: "01:00:00"

projects:
  my-project-id:
    default_executor: local
    commands:
      fit-model:
        name: fit-model
        description: Fit the model on the held-out split.
        argv: ["uv", "run", "python", "-m", "myproject.fit", "--seed", "{seed}"]
        parameters:
          - name: seed
            type: integer
            required: true
            minimum: 0
            maximum: 65535
        outputs: ["results/fit.json"]
        timeout_seconds: 1800
        checks: ["outputs_exist", "outputs_are_json"]
"""
