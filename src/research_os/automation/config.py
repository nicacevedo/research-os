"""Human-readable role and budget configuration for automation runs.

Model names are not hard-coded facts about the world. They live in a small YAML
file the researcher can edit, default to aliases the local CLI resolves itself,
and are recorded per run as whatever the provider actually reported using.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from research_os.automation.command_policy import SUPPORTED_PROGRAMS
from research_os.automation.models import Budget, Independence, RoleSetting
from research_os.automation.providers import provider_family
from research_os.errors import AutomationError, ProviderUnavailableError
from research_os.paths import config_home

CONFIG_FILENAME = "automation.yaml"

ROLE_NAMES: tuple[str, ...] = ("planner", "coder", "reviewer")

#: The check programs a plan may name by default.
#:
#: Narrower than "programs the controller could run": ``python`` executes
#: arbitrary source with ``-c`` and ``git`` reaches the network with ``push``,
#: so neither is reachable from planner output. ``command_policy`` decides the
#: authorised command forms; this list may only narrow that grammar further.
DEFAULT_ALLOWED_CHECK_PROGRAMS: tuple[str, ...] = ("uv", "pytest", "ruff")

DEFAULT_CODER_TOOLS: tuple[str, ...] = ("Read", "Write", "Edit", "Glob", "Grep")


def config_path() -> Path:
    """Return the automation configuration path under the config home."""

    return config_home() / CONFIG_FILENAME


def _default_roles() -> dict[str, RoleSetting]:
    """Return the shipped role defaults.

    The coder and reviewer deliberately differ, so that on a machine with one
    provider family the review is at least a different model reading a frozen
    diff rather than the implementer marking its own work. That is still not an
    independent review, and the run reports it as degraded.
    """

    return {
        "planner": RoleSetting(
            provider="claude",
            model="sonnet",
            effort="high",
            read_only=True,
            tools=[],
        ),
        "coder": RoleSetting(
            provider="claude",
            model="opus",
            read_only=False,
            tools=list(DEFAULT_CODER_TOOLS),
        ),
        "reviewer": RoleSetting(
            provider="claude",
            model="sonnet",
            effort="high",
            read_only=True,
            tools=[],
        ),
    }


class ConfigDocument(BaseModel):
    """The on-disk shape of ``automation.yaml``. Every key is optional."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    planner: RoleSetting | None = None
    coder: RoleSetting | None = None
    reviewer: RoleSetting | None = None
    budget: Budget | None = None
    allowed_check_programs: list[str] | None = None


@dataclass(frozen=True, slots=True)
class AutomationConfig:
    """Resolved configuration plus where each role setting came from."""

    roles: dict[str, RoleSetting]
    budget: Budget
    allowed_check_programs: tuple[str, ...]
    source: Path | None
    explicit_roles: frozenset[str]

    def role(self, name: str) -> RoleSetting:
        return self.roles[name]


def default_config() -> AutomationConfig:
    return AutomationConfig(
        roles=_default_roles(),
        budget=Budget(),
        allowed_check_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
        source=None,
        explicit_roles=frozenset(),
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
    return AutomationConfig(
        roles=roles,
        budget=document.budget or Budget(),
        allowed_check_programs=programs,
        source=target,
        explicit_roles=frozenset(explicit),
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
