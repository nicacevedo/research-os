"""Human-readable role and budget configuration for automation runs.

Model names are not hard-coded facts about the world. They live in a small YAML
file the researcher can edit, default to aliases the local CLI resolves itself,
and are recorded per run as whatever the provider actually reported using.
"""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from research_os.automation.checkprofiles import CheckProfileSpec
from research_os.automation.command_policy import (
    SUPPORTED_PROGRAMS,
    authorize_planner_argv,
)
from research_os.automation.models import Access, Budget, Independence, RoleSetting
from research_os.automation.profile import CONFIGURABLE_CAPABILITIES
from research_os.automation.providers import provider_family
from research_os.errors import (
    AutomationError,
    CommandPolicyError,
    ProviderUnavailableError,
)
from research_os.paths import config_home
from research_os.sandbox import SandboxMode

CONFIG_FILENAME = "automation.yaml"

ROLE_NAMES: tuple[str, ...] = (
    "planner",
    "analyst",
    "literature",
    "coder",
    "reviewer",
)

#: The roles every run needs.
#:
#: The analyst and the literature reader are both optional: a plan that contains
#: no task of that kind never dispatches one, and a plan that does is refused by
#: name rather than silently substituting a role with different authority.
REQUIRED_ROLE_NAMES: tuple[str, ...] = ("planner", "coder", "reviewer")

#: The check programs a plan may name by default.
#:
#: Narrower than "programs the controller could run": ``python`` executes
#: arbitrary source with ``-c`` and ``git`` reaches the network with ``push``,
#: so neither is reachable from planner output. ``command_policy`` decides the
#: authorised command forms; this list may only narrow that grammar further.
DEFAULT_ALLOWED_CHECK_PROGRAMS: tuple[str, ...] = ("uv", "pytest", "ruff")

DEFAULT_CODER_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")

#: The only tools the analysis role is given.
#:
#: Read-only by construction: no ``Write``, no ``Edit``, no ``Bash``. The role
#: model refuses anything else here, and the invocation layer refuses it again.
DEFAULT_ANALYST_TOOLS: tuple[str, ...] = ("Read", "Glob", "Grep")


def config_path() -> Path:
    """Return the automation configuration path under the config home."""

    return config_home() / CONFIG_FILENAME


def _default_roles() -> dict[str, RoleSetting]:
    """Return the shipped role defaults.

    The coder and reviewer deliberately differ, so that on a machine with one
    provider family the review is at least a different model reading a frozen
    diff rather than the implementer marking its own work. That is still not an
    independent review, and the run reports it as degraded.

    **The planner is the strongest model, and that is a measured decision.** It
    shipped as ``sonnet`` and was changed on evidence, not on principle. Thirty
    real planner calls over five archived fixtures -- a capsule project, a
    capsule-less assessment, a 5,402-file repository, a ``src``-layout code
    workflow and a full experiment-and-write pipeline -- were judged by the
    production validators, twice each, under three policies:

    ==============================  =====  =====  =====  ==========  ==========
    policy                          cells  calls  valid  first-pass  degenerate
    ==============================  =====  =====  =====  ==========  ==========
    ``sonnet`` + one re-ask            10     16   7/10        4/10           3
    ``opus`` + one re-ask               9     10    9/9         8/9           0
    ``sonnet``, then ``opus`` on a
    deterministic failure              10     16   8/10        4/10           3
    ==============================  =====  =====  =====  ==========  ==========

    Two things about that table, because it does not add up on its own. The
    calls column sums to 42, not 30, because the first two policies make the
    *same* first call -- same model, same prompt, same schema -- so it was drawn
    once and scored for both. And the ``opus`` row has nine cells, not ten: the
    thirty-call cap was reached before its last repetition, so one cell was
    never measured. Had that cell failed, the row would read 9/10, still ahead
    of both alternatives on every criterion that decided this.

    Every structured-output exhaustion and every placeholder plan in that
    benchmark came from the smaller model; the stronger one produced neither,
    needed the fewest calls, cost the least *per accepted plan*, and was the
    only policy with no systematic failure on a project mode. Escalating after a
    failure recovered two of the three losses and still spent sixteen calls, so
    it buys routing code and a wasted attempt for less reliability than simply
    asking the stronger model first. ``docs/V1_BUILD_RECORD.md`` §32 records the
    protocol, the fixtures and every call.

    This is a default, not a requirement: ``planner:`` in ``automation.yaml``
    replaces it, and the run records the model that actually answered rather
    than the alias that was asked for, so a substitution cannot hide in the
    ledger. One qualification an independent review was right to insist on --
    if the *provider* a role names is not installed, ``resolve_roles`` re-homes
    that role onto an available one and drops the model with it, because an
    alias is provider-specific. A researcher who configures a planner model on
    a provider this machine does not have gets the substitution recorded in
    ``ResolvedRoles.substitutions``, not their chosen model.
    """

    return {
        "planner": RoleSetting(
            provider="claude",
            model="opus",
            effort="high",
            read_only=True,
            access=Access.CONTEXT_ONLY,
            tools=[],
        ),
        "analyst": RoleSetting(
            provider="claude",
            model="sonnet",
            effort="high",
            read_only=True,
            access=Access.SNAPSHOT_READ,
            tools=list(DEFAULT_ANALYST_TOOLS),
        ),
        "literature": RoleSetting(
            provider="claude",
            model="sonnet",
            effort="high",
            read_only=True,
            access=Access.CONTEXT_ONLY,
            tools=[],
        ),
        "coder": RoleSetting(
            provider="claude",
            model="opus",
            read_only=False,
            access=Access.ISOLATED_WRITE,
            tools=list(DEFAULT_CODER_TOOLS),
        ),
        "reviewer": RoleSetting(
            provider="claude",
            model="sonnet",
            effort="high",
            read_only=True,
            access=Access.CONTEXT_ONLY,
            tools=[],
        ),
    }


class SandboxSettings(BaseModel):
    """Whether commands this system did not write run under OS containment.

    Lives in ``automation.yaml`` rather than in ``runtime.yaml`` because the
    exposure is the v1 coding pipeline's as much as the runtime's: ``researchctl
    auto`` runs a project's acceptance commands after a write-enabled worker has
    edited files in scope, and has always done so. The runtime's contribution is
    that nobody decides to run it any more.

    The default is ``preferred``: contain where the host can, run uncontained
    and *record the absence* where it cannot. Not ``required``, because a
    researcher at the keyboard on a host with no mechanism should get a run and
    a clear note rather than a refusal -- and not ``off``, because the default
    must be the safe one wherever safety is available.

    High-autonomy runtime execution overrides this to ``required`` regardless.
    That is the case the mode cannot be trusted to a default: nobody is
    watching, and ``preferred`` there would mean model-written code running with
    the researcher's credentials unattended.
    """

    model_config = ConfigDict(extra="forbid")

    mode: SandboxMode = SandboxMode.PREFERRED
    network: bool = False
    """Whether contained commands may reach the network.

    A capability rather than a default. Most acceptance commands need no
    network, a lock file exists so that dependency resolution does not, and a
    command that silently fetches something is a command whose result is not
    reproducible.
    """

    extra_readable: list[str] = Field(default_factory=list)
    """Absolute paths a contained command may read, beyond the OS and its inputs.

    For the shared caches a build needs -- a wheel cache, a dataset directory.
    Declared by the researcher, in the config home, which is outside every
    worktree: a write-enabled worker cannot reach this file to grant itself a
    path.
    """

    @field_validator("extra_readable")
    @classmethod
    def _absolute_paths(cls, value: list[str]) -> list[str]:
        relative = [item for item in value if not item.startswith("/")]
        if relative:
            raise ValueError(
                "sandbox.extra_readable entries must be absolute paths; got "
                + ", ".join(relative)
            )
        return value


class ProjectSettings(BaseModel):
    """What a researcher has declared about one of their own projects.

    Keyed by project id under ``projects:``, exactly as ``experiments.yaml``
    keys its declared commands, and living in the same place for the same
    reason: the config home is outside every automation worktree, so a
    write-enabled worker cannot reach this file to declare itself new checks or
    new capabilities.
    """

    model_config = ConfigDict(extra="forbid")

    check_profiles: dict[str, CheckProfileSpec] = Field(default_factory=dict)
    """Named validation checks, replacing discovery entirely when present."""

    capabilities: dict[str, bool] = Field(default_factory=dict)
    """Facts the researcher asserts about this project, overriding discovery."""

    @field_validator("capabilities")
    @classmethod
    def _known_capability_names(cls, value: dict[str, bool]) -> dict[str, bool]:
        unknown = sorted(set(value) - CONFIGURABLE_CAPABILITIES)
        if unknown:
            raise ValueError(
                "capabilities names "
                + ", ".join(unknown)
                + ", which this build has no capability for. Known: "
                + ", ".join(sorted(CONFIGURABLE_CAPABILITIES))
            )
        return value


class ConfigDocument(BaseModel):
    """The on-disk shape of ``automation.yaml``. Every key is optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    planner: RoleSetting | None = None
    analyst: RoleSetting | None = None
    literature: RoleSetting | None = None
    coder: RoleSetting | None = None
    reviewer: RoleSetting | None = None
    budget: Budget | None = None
    allowed_check_programs: list[str] | None = None
    projects: dict[str, ProjectSettings] = Field(default_factory=dict)
    sandbox: SandboxSettings | None = None


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    """Resolved configuration plus where each role setting came from."""

    roles: dict[str, RoleSetting]
    budget: Budget
    allowed_check_programs: tuple[str, ...]
    source: Path | None
    explicit_roles: frozenset[str]
    projects: dict[str, ProjectSettings] = dataclasses.field(default_factory=dict)
    sandbox: SandboxSettings = dataclasses.field(default_factory=SandboxSettings)

    def role(self, name: str) -> RoleSetting:
        return self.roles[name]

    def for_project(self, project_id: str | None) -> ProjectSettings:
        """Return one project's declarations, or an empty set.

        An unregistered project has no id to key on, and a registered one
        nobody has configured has nothing declared. Both are the same honest
        answer: discovery decides, and the profile records that it did.
        """

        if project_id is None:
            return ProjectSettings()
        return self.projects.get(project_id, ProjectSettings())


def default_config() -> AutomationConfig:
    return AutomationConfig(
        roles=_default_roles(),
        budget=Budget(),
        allowed_check_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
        source=None,
        explicit_roles=frozenset(),
        projects={},
        sandbox=SandboxSettings(),
    )


def load_config(path: Path | None = None) -> AutomationConfig:
    """Load automation configuration, falling back to the shipped defaults."""

    target = path if path is not None else config_path()
    if not target.is_file():
        if path is not None:
            raise AutomationError(f"no automation config at {target}")
        return default_config()
    try:
        raw = target.read_text(encoding="utf-8")
    except OSError as exc:
        raise AutomationError(f"cannot read {target}: {exc}") from exc
    try:
        data = yaml.safe_load(raw)
    except yaml.YAMLError as exc:
        raise AutomationError(f"invalid YAML in {target}: {exc}") from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise AutomationError(f"{target} must contain a mapping")
    try:
        document = ConfigDocument.model_validate(data)
    except ValidationError as exc:
        raise AutomationError(f"invalid automation config at {target}: {exc}") from exc

    roles = _default_roles()
    explicit: set[str] = set()
    for name in ROLE_NAMES:
        setting = getattr(document, name)
        if setting is not None:
            roles[name] = setting
            explicit.add(name)
    programs = (
        tuple(document.allowed_check_programs)
        if document.allowed_check_programs is not None
        else DEFAULT_ALLOWED_CHECK_PROGRAMS
    )
    unsupported = [item for item in programs if item not in SUPPORTED_PROGRAMS]
    if unsupported:
        raise AutomationError(
            f"invalid automation config at {target}: allowed_check_programs "
            f"names {', '.join(sorted(unsupported))}, which the acceptance "
            "command policy has no grammar for. Configuration may only narrow "
            f"the supported programs: {', '.join(SUPPORTED_PROGRAMS)}"
        )
    for project_id, settings in sorted(document.projects.items()):
        for name, spec in sorted(settings.check_profiles.items()):
            try:
                authorize_planner_argv(spec.argv, allowed_programs=programs)
            except CommandPolicyError as exc:
                raise AutomationError(
                    f"invalid automation config at {target}: the check profile "
                    f"{name!r} declared for project {project_id!r} is not a "
                    f"command the controller may run: {exc}"
                ) from exc
    return AutomationConfig(
        roles=roles,
        budget=document.budget or Budget(),
        allowed_check_programs=programs,
        source=target,
        explicit_roles=frozenset(explicit),
        projects=dict(document.projects),
        sandbox=document.sandbox or SandboxSettings(),
    )


@dataclass(frozen=True, slots=True)
class ResolvedRoles:
    """Which provider each role will actually use, and how independent that is."""

    roles: dict[str, RoleSetting]
    independence: Independence
    note: str
    substitutions: tuple[str, ...]

    @property
    def degraded(self) -> bool:
        return self.independence is not Independence.INDEPENDENT_PROVIDER_FAMILY


def resolve_roles(
    config: AutomationConfig,
    probes: dict[str, Any],
) -> ResolvedRoles:
    """Bind each role to an available provider and state the review's independence.

    Degraded independence is never presented as an independent review. When only
    one model family is installed the run still proceeds, because a same-family
    reviewer reading a frozen diff still catches real defects, but the report
    says plainly that it is not independent.
    """

    available = sorted(name for name, probe in probes.items() if probe.available)
    if not available:
        raise ProviderUnavailableError(
            "no locally verified agent provider is available; install and "
            "authenticate one of: " + ", ".join(sorted(probes))
        )

    roles: dict[str, RoleSetting] = {}
    substitutions: list[str] = []
    for name in ROLE_NAMES:
        if name not in config.roles:
            # A configuration loaded from disk always carries every role,
            # because the defaults are the starting point. One built in code
            # may omit an optional role such as the analyst; the work that
            # needs it then refuses by name rather than failing here.
            continue
        setting = config.role(name)
        if setting.provider in available:
            roles[name] = setting
            continue
        replacement = available[0]
        substitutions.append(
            f"{name}: {setting.provider} is unavailable, using {replacement}"
        )
        roles[name] = setting.model_copy(
            update={"provider": replacement, "model": None}
        )

    missing = [name for name in REQUIRED_ROLE_NAMES if name not in roles]
    if missing:
        raise AutomationError(
            "this automation configuration declares no "
            f"{', '.join(missing)} role; a run cannot plan, implement, and "
            "review without all three"
        )

    reviewer = roles["reviewer"]
    coder = roles["coder"]
    if provider_family(reviewer.provider) == provider_family(coder.provider):
        alternative = next(
            (
                name
                for name in available
                if provider_family(name) != provider_family(coder.provider)
            ),
            None,
        )
        if alternative is not None and "reviewer" not in config.explicit_roles:
            substitutions.append(
                f"reviewer: moved to {alternative} so the review does not run on "
                "the implementer's own model family"
            )
            reviewer = reviewer.model_copy(
                update={"provider": alternative, "model": None}
            )
            roles["reviewer"] = reviewer

    independence, note = _assess_independence(coder, reviewer)
    return ResolvedRoles(
        roles=roles,
        independence=independence,
        note=note,
        substitutions=tuple(substitutions),
    )


def _assess_independence(
    coder: RoleSetting,
    reviewer: RoleSetting,
) -> tuple[Independence, str]:
    coder_family = provider_family(coder.provider)
    reviewer_family = provider_family(reviewer.provider)
    if coder_family != reviewer_family:
        note = (
            f"reviewer runs on {reviewer.provider} ({reviewer_family}), a "
            f"different model family from the implementer's {coder.provider} "
            f"({coder_family})"
        )
        return Independence.INDEPENDENT_PROVIDER_FAMILY, note
    if (coder.model or "") == (reviewer.model or ""):
        note = (
            "NOT an independent review: reviewer and implementer are the same "
            f"provider ({coder.provider}) and the same model "
            f"({coder.model or 'provider default'})"
        )
        return Independence.DEGRADED_SAME_MODEL, note
    note = (
        "NOT an independent review: reviewer and implementer are different "
        f"models ({reviewer.model or 'provider default'} reviewing "
        f"{coder.model or 'provider default'}) from the same family "
        f"({coder_family}); no second model family is installed"
    )
    return Independence.DEGRADED_SAME_PROVIDER_FAMILY, note
