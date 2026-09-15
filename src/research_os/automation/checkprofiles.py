"""Named validation checks the controller owns, so a planner never writes argv.

A planner should decide **what** needs validating. Deciding **how** to invoke it
is an environment question, and v1.0.0 recorded the cost of asking a model that:
a ``src``-layout Python project's tests only import under ``uv run``, a planner
chose a bare ``pytest``, collection failed, the bounded repair burned itself on
an import error, and the run failed closed on something the controller could
have known by reading two filenames.

So a check is a name here. ``tests`` and ``lint`` are things a plan may ask for;
``["uv", "run", "pytest", "-q"]`` is what this module resolves that to, and a
model cannot change the second by any means, including asking for it.

Three rules hold the design together.

**Explicit configuration wins.** A researcher who has declared their project's
checks gets exactly those. Discovery is what happens when nobody has said.

**Discovery is conservative.** One ecosystem, recognised by two tracked files
and the dependencies the project itself declares. A pattern no current project
needs is a pattern nobody has watched work, and guessing wrong here means
running the wrong command against real code.

**A profile is not an escalation.** Every resolved argv is passed through the
same :mod:`~research_os.automation.command_policy` grammar that planner-authored
commands face, against the same project allowlist. A configuration file cannot
introduce an executable the policy forbids, because the policy is asked after
configuration is read, not before.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from research_os.automation.command_policy import authorize_planner_argv
from research_os.errors import CommandPolicyError

#: The check ids this build knows how to discover, in the order they should run.
#:
#: Cheap and broad first. A lint failure is usually faster to produce and easier
#: to read than a test failure, but a passing lint over broken code says nothing,
#: so tests lead and the rest follow.
TESTS = "tests"
LINT = "lint"
FORMAT = "format"

CHECK_IDS: tuple[str, ...] = (TESTS, LINT, FORMAT)


class ProfileSource(StrEnum):
    """Where one check profile's argv came from."""

    EXPLICIT_CONFIG = "explicit_config"
    DISCOVERED = "discovered"


class CheckProfile(BaseModel):
    """One named validation check, with the exact argv the controller will run."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    check_id: str
    argv: list[str] = Field(min_length=1)
    source: ProfileSource
    required: bool = True
    description: str = ""

    @field_validator("check_id")
    @classmethod
    def _identifier_shape(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned or not cleaned.replace("-", "").replace("_", "").isalnum():
            raise ValueError(
                "a check id is a short alphanumeric name such as 'tests' or 'lint'"
            )
        return cleaned


class CheckProfileSpec(BaseModel):
    """One check as a researcher declares it in configuration."""

    model_config = ConfigDict(extra="forbid")

    argv: list[str] = Field(min_length=1)
    required: bool = True
    description: str = ""


def resolve_check_profiles(
    *,
    tracked: frozenset[str],
    dependencies: frozenset[str],
    tool_sections: frozenset[str],
    allowed_programs: tuple[str, ...],
    configured: dict[str, CheckProfileSpec] | None = None,
) -> tuple[tuple[CheckProfile, ...], bool]:
    """Return this project's check profiles and whether they were configured.

    Configuration is consulted first and, when it says anything at all, it is
    the whole answer: a researcher who declared two checks meant two checks, and
    silently adding a discovered third would run a command they did not ask for.

    Every argv -- configured or discovered -- is authorised against the command
    policy before it is returned. A configured command the policy refuses raises
    :class:`~research_os.errors.CommandPolicyError`, because a check the
    controller cannot run is a configuration defect the researcher should be
    told about rather than a check that quietly disappears.
    """

    if configured:
        profiles = tuple(
            CheckProfile(
                check_id=name,
                argv=list(spec.argv),
                source=ProfileSource.EXPLICIT_CONFIG,
                required=spec.required,
                description=spec.description or f"configured check {name!r}",
            )
            for name, spec in sorted(configured.items())
        )
        for profile in profiles:
            authorize_planner_argv(profile.argv, allowed_programs=allowed_programs)
        return profiles, True

    discovered = _discover(tracked=tracked, dependencies=dependencies)
    usable: list[CheckProfile] = []
    for profile in discovered:
        try:
            authorize_planner_argv(profile.argv, allowed_programs=allowed_programs)
        except CommandPolicyError:
            # A project allowlist that narrows away ``uv`` is a deliberate
            # choice, and the honest consequence is fewer profiles rather than a
            # failed run. Discovery proposes; the policy disposes.
            continue
        usable.append(profile)
    return tuple(usable), False


def _discover(
    *, tracked: frozenset[str], dependencies: frozenset[str]
) -> tuple[CheckProfile, ...]:
    """Return the profiles a Python/uv project's own metadata justifies.

    ``uv.lock`` beside ``pyproject.toml`` is the whole trigger, and it is what
    makes ``uv run`` the right prefix rather than a preference: a project with a
    lock file has an environment uv knows how to materialise, and running its
    tests any other way runs them against whatever happens to be on PATH. That
    is precisely the failure this module exists to remove.
    """

    if "pyproject.toml" not in tracked or "uv.lock" not in tracked:
        return ()

    # Declared dependencies only, and that is the whole rule. An independent
    # review of this release found the earlier version offering `uv run pytest`
    # to a project with a `tests/` directory and pytest declared nowhere, where
    # the command cannot spawn -- reintroducing, through the profile, exactly
    # the v1.0.0 trap this module exists to remove. `_dependency_names` already
    # absorbs every list uv could have resolved the tool from, so a project
    # where `uv run <tool>` works is a project where the tool is declared.
    found: list[CheckProfile] = []
    if "pytest" in dependencies:
        found.append(
            CheckProfile(
                check_id=TESTS,
                argv=["uv", "run", "pytest", "-q"],
                source=ProfileSource.DISCOVERED,
                description=(
                    "the project's own test suite, run in the environment uv "
                    "materialises from this project's lock file"
                ),
            )
        )
    if "ruff" in dependencies:
        found.append(
            CheckProfile(
                check_id=LINT,
                argv=["uv", "run", "ruff", "check", "."],
                source=ProfileSource.DISCOVERED,
                description="the lint rules this project configures",
            )
        )
        found.append(
            CheckProfile(
                check_id=FORMAT,
                argv=["uv", "run", "ruff", "format", "--check", "."],
                source=ProfileSource.DISCOVERED,
                description="the formatting this project configures, reported not applied",
            )
        )
    order = {name: index for index, name in enumerate(CHECK_IDS)}
    return tuple(sorted(found, key=lambda item: order.get(item.check_id, len(order))))


def resolve_required_checks(
    profiles: tuple[CheckProfile, ...],
    requested: list[str],
) -> tuple[CheckProfile, ...]:
    """Return the profiles ``requested`` names, refusing anything unresolvable.

    A plan naming a check this project does not have is refused here rather than
    silently running fewer checks than the plan said it would. Duplicate names
    collapse; order follows the profile order, not the request, so a plan cannot
    reorder a project's checks.
    """

    known = {item.check_id: item for item in profiles}
    unknown = sorted(set(requested) - set(known))
    if unknown:
        raise CommandPolicyError(
            "the plan requires validation checks this project has no profile for: "
            + ", ".join(unknown)
            + ". Available: "
            + (", ".join(sorted(known)) or "none")
        )
    wanted = set(requested)
    return tuple(item for item in profiles if item.check_id in wanted)


def render_check_profiles(profiles: tuple[CheckProfile, ...]) -> str:
    """Render the available checks as the line a planner reads.

    The argv is shown deliberately. A planner that can see what ``tests`` will
    actually run can tell whether asking for it addresses the goal; what it
    cannot do is change it.
    """

    if not profiles:
        return (
            "    (none - this project has no controller-owned validation profile, "
            "so a code task must name its own acceptance commands)"
        )
    return "\n".join(
        f"    {item.check_id}: {' '.join(item.argv)}"
        + (f"  [{item.description}]" if item.description else "")
        for item in profiles
    )
