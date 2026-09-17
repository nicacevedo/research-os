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

from research_os.automation.checkprofiles import (
    CheckProfile,
    ProfileSource,
    resolve_check_profiles,
)
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

    @property
    def configured(self) -> bool:
        """Whether the researcher declared these checks rather than discovery.

        ``resolve_check_profiles`` answers all-or-nothing -- configuration, when
        it says anything, is the whole answer -- so every profile carries the
        same source and reading the first is reading the decision. Exposed here
        because a caller deciding whether a declaration *overrules* something
        else must distinguish a declaration from a guess, and the guess has no
        such authority.
        """

        return bool(self.check_profiles) and all(
            item.source is ProfileSource.EXPLICIT_CONFIG for item in self.check_profiles
        )


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
        # Discovery reads the tracked list, so a list that had to be cut is a
        # list discovery must not reason from. Configuration still applies: the
        # researcher's declaration does not depend on the file count.
        tracked=facts.tracked if facts.tracked_known else frozenset(),
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


def project_id_for(project_path: Path) -> str | None:
    """The capsule's project id, or ``None`` when the repository has no capsule.

    Every caller that needs a project's *configured* check profiles has to key
    ``projects.<id>`` in ``automation.yaml`` on something, and until now only
    callers that already held a scientific run knew what that something was. A
    controller handed nothing but a path resolved ``for_project(None)`` and got
    an empty declaration back -- so the same repository had configured checks on
    one path through the system and discovered checks on another.

    The id is read from the same file every other layer reads it from:
    ``.research/project.yaml``, through ``load_project_identity``, which does
    not validate the science. A repository with no capsule genuinely has no id,
    and ``None`` is the honest answer rather than a guess from the directory
    name.
    """

    from research_os.capsule import load_project_identity
    from research_os.errors import ResearchOSError

    try:
        _root, project = load_project_identity(project_path)
    except ResearchOSError:
        return None
    return str(project.id)


def resolve_project_from_path(
    *, project_path: Path, config: AutomationConfig
) -> ResolvedProject:
    """Resolve a project when the caller has a path and nothing else.

    The same resolution :func:`resolve_project` performs, with the project id
    derived from the repository instead of supplied. This is what makes "one
    acceptance profile per project" true rather than aspirational: a path is the
    only thing every caller has, so a resolution keyed on a path is the only one
    they can all reach.
    """

    project_id = project_id_for(project_path)
    return resolve_project(
        project_path=project_path,
        config=config,
        project_id=project_id,
        registered=project_id is not None,
    )
