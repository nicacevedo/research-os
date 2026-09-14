"""One resolution of what a project is, used by everything that needs to know.

:mod:`~research_os.automation.profile` reads a repository and
:mod:`~research_os.automation.checkprofiles` turns the same reading into argv.
Both need the researcher's configuration, and configuration imports neither, so
this is where the three meet. It exists to make one guarantee: a run resolves
its project exactly once, and the profile it puts in a prompt describes the same
tree, the same configuration and the same checks the controller will run.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from research_os.automation.checkprofiles import CheckProfile, resolve_check_profiles
from research_os.automation.config import AutomationConfig
from research_os.automation.profile import (
    ProjectProfile,
    build_project_profile,
    inspect_repository,
)


@dataclass(frozen=True, slots=True)
class ResolvedProject:
    """A project's deterministic profile and the checks that go with it."""

    profile: ProjectProfile
    check_profiles: tuple[CheckProfile, ...]

    @property
    def check_ids(self) -> tuple[str, ...]:
        return tuple(item.check_id for item in self.check_profiles)

    @property
    def has_checks(self) -> bool:
        return bool(self.check_profiles)


def resolve_project(
    *,
    project_path: Path,
    config: AutomationConfig,
    project_id: str | None = None,
    registered: bool = False,
    declared_experiments: tuple[str, ...] = (),
) -> ResolvedProject:
    """Resolve ``project_path`` into a profile and its controller-owned checks."""

    facts = inspect_repository(project_path)
    settings = config.for_project(project_id)
    profiles, explicit = resolve_check_profiles(
        tracked=facts.tracked,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        allowed_programs=config.allowed_check_programs,
        configured=settings.check_profiles or None,
    )
    profile = build_project_profile(
        project_path=project_path,
        project_id=project_id,
        registered=registered,
        declared_experiments=declared_experiments,
        declared_capabilities=settings.capabilities or None,
        check_profile_ids=tuple(item.check_id for item in profiles),
        check_profiles_explicit=explicit,
        facts=facts,
    )
    return ResolvedProject(profile=profile, check_profiles=profiles)
