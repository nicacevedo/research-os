"""What the controller knows about a project without asking a model.

A planner that has to work out whether a repository is ``src``-layout, whether
it has a lock file, or whether it holds a Research Capsule is a planner being
asked to establish facts the machine can read. v1.0.0 paid for that twice: a
run against a ``src``-layout project chose a bare ``pytest`` because nothing had
told it the project's tests only import under ``uv run``, and a run against a
repository with no capsule invented scientific identifiers because nothing had
told it there were none.

A :class:`ProjectProfile` is the controller's answer to both. It is a set of
**capabilities** -- facts with an origin -- computed deterministically from the
repository's tracked files, its metadata, and the researcher's own
configuration. Four properties make it worth having rather than merely
convenient.

**Nothing here executes repository code.** Profiling reads ``git ls-files``,
checks whether named files are tracked, parses ``pyproject.toml`` with
``tomllib``, and asks the kernel's own ``validate_project`` whether there is a
readable capsule -- which parses the capsule's YAML and nothing else. A
repository that would like to be profiled differently has no way to say so.

**Nothing a model says can change it.** The profile is built before the planner
is invoked and is never rebuilt from worker output. It reaches a prompt as
controller-authored context and comes back as nothing at all.

**Explicit configuration wins.** A researcher who has declared what their
project is does not have their declaration overruled by a heuristic, and the
origin field says which happened for every fact.

**The same tree profiles the same way.** There is no timestamp in a profile and
no unordered collection; two profiles of one tree compare equal. That is what
makes "the profile changed" a statement about the repository.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from research_os.automation.gitutil import (
    git,
    has_commits,
    head_commit,
    repository_root,
)
from research_os.automation.promptdata import prompt_safe
from research_os.errors import GitError

#: How many tracked paths one profile reads before it stops looking.
#:
#: A bound on the work, not on the repository. Every capability below is decided
#: by the presence of a named file or a shallow directory shape, so a repository
#: larger than this is profiled from its first paths in sorted order and says so
#: in ``discovery_errors`` rather than silently profiling half a tree.
MAX_TRACKED_PATHS = 20_000

#: How many manuscript files a profile names.
MAX_MANUSCRIPTS = 20


class CapabilityOrigin(StrEnum):
    """Where one capability's value came from.

    Recorded for every capability, including the absent ones, because "we looked
    and it is not there" and "we could not look" are different facts and a
    planner that cannot tell them apart will guess.
    """

    EXPLICIT_CONFIG = "explicit_config"
    """The researcher declared it in configuration, outside every worktree."""

    REPOSITORY_METADATA = "repository_metadata"
    """Read out of a metadata file the repository itself carries."""

    DETERMINISTIC_STRUCTURE = "deterministic_structure"
    """Derived from the tracked file layout, with no interpretation."""

    UNAVAILABLE = "unavailable"
    """Could not be established. Never the same as ``present=False``."""


class CapabilityName(StrEnum):
    """The closed set of facts a profile establishes.

    Closed on purpose, and small. This is not a repository classifier: every
    entry exists because some part of this system dispatches differently on it,
    and a fact nothing dispatches on is a fact nobody has to trust.
    """

    CAPSULE = "capsule"
    """A Research Capsule exists at ``.research/``."""

    PYTHON_PROJECT = "python_project"
    PYPROJECT_TOML = "pyproject_toml"
    UV_LOCK = "uv_lock"
    PYTHON_SRC_LAYOUT = "python_src_layout"
    PYTHON_FLAT_LAYOUT = "python_flat_layout"
    PYTEST_AVAILABLE = "pytest_available"
    RUFF_AVAILABLE = "ruff_available"
    JULIA_PROJECT = "julia_project"
    EXPERIMENT_REGISTRY = "experiment_registry"
    """The researcher has declared experiment commands for this project."""

    VALIDATION_PROFILES = "validation_profiles"
    """The controller can resolve named checks without the planner writing argv."""

    MANUSCRIPTS = "manuscripts"


#: Capability names a researcher may assert in configuration.
#:
#: Every one of them, deliberately. A declaration is the researcher telling the
#: controller a fact about their own project; the controller's job is to record
#: that it was a declaration, not to decide which declarations are allowed.
CONFIGURABLE_CAPABILITIES: frozenset[str] = frozenset(
    item.value for item in CapabilityName
)


class ProvenanceMode(StrEnum):
    """Which grounding universe a project's reasoning is allowed to draw on.

    Chosen by the controller from :attr:`ProjectProfile.capsule_present`, never
    by a model. A project with a capsule reasons about Questions, Hypotheses,
    Claims and Evidence; one without reasons about files, symbols, checks and
    literature. Letting a worker pick between them would let a worker pick its
    own authority model.
    """

    SCIENTIFIC_PROJECT = "scientific_project"
    REPOSITORY_ASSESSMENT = "repository_assessment"


class Capability(BaseModel):
    """One fact about a project, and where it came from."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: CapabilityName
    present: bool
    origin: CapabilityOrigin
    detail: str = ""

    @property
    def known(self) -> bool:
        return self.origin is not CapabilityOrigin.UNAVAILABLE


class ProjectProfile(BaseModel):
    """Everything the controller knows about one project, deterministically.

    Carries no timestamp and no unordered collection: two profiles of the same
    tree, configuration and base commit are equal, which is what lets a test
    assert stability rather than assert around instability.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    project_root: str
    base_commit: str | None = None
    project_id: str | None = None
    registered: bool = False
    capsule_present: bool = False
    capabilities: list[Capability] = Field(default_factory=list)
    """Every capability in :class:`CapabilityName`, in enum order."""

    declared_experiments: list[str] = Field(default_factory=list)
    manuscript_files: list[str] = Field(default_factory=list)
    discovery_errors: list[str] = Field(default_factory=list)
    """What could not be established, stated rather than guessed around."""

    @property
    def provenance_mode(self) -> ProvenanceMode:
        """Which grounding universe this project's workers may cite from."""

        return (
            ProvenanceMode.SCIENTIFIC_PROJECT
            if self.capsule_present
            else ProvenanceMode.REPOSITORY_ASSESSMENT
        )

    def capability(self, name: CapabilityName) -> Capability:
        for item in self.capabilities:
            if item.name is name:
                return item
        raise KeyError(name)

    def has(self, name: CapabilityName) -> bool:
        """Whether ``name`` is present. An unavailable capability is not present."""

        try:
            found = self.capability(name)
        except KeyError:  # pragma: no cover - capabilities are exhaustive
            return False
        return found.present and found.known


@dataclass(frozen=True, slots=True)
class RepositoryFacts:
    """What one deterministic pass over a repository established.

    Separate from :class:`ProjectProfile` because two things need it and neither
    should read the tree twice: the profile turns it into capabilities, and
    :mod:`research_os.automation.checkprofiles` turns it into argv. Reading
    ``git ls-files`` once per run also means both answers describe the same
    tree, which is what makes the profile's ``validation_profiles`` capability a
    statement about the profiles that will actually run.
    """

    root: Path
    tracked: frozenset[str]
    dependencies: frozenset[str]
    tool_sections: frozenset[str]
    capsule_present: bool
    capsule_unreadable: bool
    """A capsule is tracked here and could not be read as a project."""

    tracked_known: bool
    metadata_readable: bool
    errors: tuple[str, ...]


def inspect_repository(project_path: Path) -> RepositoryFacts:
    """Read one repository's tracked layout and its Python metadata. Once."""

    root = repository_root(project_path)
    errors: list[str] = []
    tracked, truncated, tracked_error = _tracked_paths(root)
    if tracked_error:
        errors.append(tracked_error)
    if truncated:
        # F10 from the delta review: a truncated list was being reported as a
        # deterministic *absence*. A repository whose pyproject.toml sorts below
        # the cut was told, under a heading saying these are facts, that it has
        # none. Truncation now makes every structural capability UNAVAILABLE,
        # which is what that value is for.
        errors.append(
            f"more than {MAX_TRACKED_PATHS} tracked paths, so the tracked file "
            "list was cut and no structural capability can be established from it"
        )
    metadata, metadata_error = _read_pyproject(root, tracked)
    if metadata_error:
        errors.append(metadata_error)
    metadata_readable = metadata_error is None
    capsule_present, capsule_unreadable, capsule_error = _capsule_state(root)
    if capsule_error:
        errors.append(capsule_error)
    return RepositoryFacts(
        root=root,
        tracked=tracked,
        dependencies=(
            _dependency_names(metadata) if metadata_readable else frozenset()
        ),
        tool_sections=_tool_sections(metadata) if metadata_readable else frozenset(),
        capsule_present=capsule_present,
        capsule_unreadable=capsule_unreadable,
        tracked_known=tracked_error is None and not truncated,
        metadata_readable=metadata_readable,
        errors=tuple(errors),
    )


def _capsule_state(root: Path) -> tuple[bool, bool, str | None]:
    """Decide whether this project has a Research Capsule.

    By asking the question the rest of the system asks, in the one way it asks
    it: does ``validate_project`` return a parsed project? That is exactly
    :func:`build_science_context`'s test, and agreement with it is the whole
    property -- because the two answers end up in the *same prompt*, one listing
    citable scientific objects and the other saying there are none, under a
    heading that says these are facts.

    Both of this release's attempts at a cleverer rule were wrong, in opposite
    directions, and each was caught by a review.

    The first accepted ``(root / ".research").is_dir()``, so a crashed
    ``init-project`` or an empty directory made a repository into a scientific
    project whose only available citations were invented ones.

    The second required the capsule to be *tracked*, which closed that and
    opened the mirror image: ``init-project`` creates ``.research/`` and does
    not stage it, so between initialising a project and its first ``git add``
    the profile said capsule-less while the science context read the capsule off
    disk and listed its objects.

    There is no third clever rule. Asking the same question is the answer, and
    an unreadable capsule -- present but not parsing -- is reported as a
    discovery error so the contradiction cannot come back as silence.
    """

    from research_os.capsule import validate_project

    if not (root / ".research").exists():
        return False, False, None
    try:
        report = validate_project(root)
    except Exception as exc:  # noqa: BLE001 - a broken capsule must not crash profiling
        return False, True, f"a .research/ capsule is present but unreadable: {exc}"
    if report.capsule is None:
        return False, False, None
    if report.project is None:
        return (
            False,
            True,
            (
                "a .research/ capsule is present but its project.yaml does not "
                "parse, so this run has no scientific objects to reason over"
            ),
        )
    return True, False, None


def build_project_profile(
    *,
    project_path: Path,
    project_id: str | None = None,
    registered: bool = False,
    declared_experiments: tuple[str, ...] = (),
    declared_capabilities: dict[str, bool] | None = None,
    check_profile_ids: tuple[str, ...] = (),
    check_profiles_explicit: bool = False,
    facts: RepositoryFacts | None = None,
) -> ProjectProfile:
    """Return the deterministic profile of ``project_path``.

    ``declared_capabilities`` is the researcher's own configuration and always
    wins: a declared value is recorded with :attr:`CapabilityOrigin.EXPLICIT_CONFIG`
    and discovery for that name is not consulted. ``check_profile_ids`` is what
    :mod:`research_os.automation.checkprofiles` resolved, passed in rather than
    recomputed so there is one resolution per run.
    """

    declared = dict(declared_capabilities or {})
    unknown = sorted(set(declared) - CONFIGURABLE_CAPABILITIES)
    if unknown:
        raise ValueError(
            "configuration declares capabilities this build has no name for: "
            + ", ".join(unknown)
        )

    if facts is None:
        facts = inspect_repository(project_path)
    root = facts.root
    tracked = facts.tracked
    errors = list(facts.errors)
    capsule_present = facts.capsule_present

    discovered = _discover(
        tracked=tracked,
        capsule_present=capsule_present,
        capsule_unreadable=facts.capsule_unreadable,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        metadata_readable=facts.metadata_readable,
        declared_experiments=declared_experiments,
        check_profile_ids=check_profile_ids,
        check_profiles_explicit=check_profiles_explicit,
        tracked_known=facts.tracked_known,
    )

    capabilities: list[Capability] = []
    for name in CapabilityName:
        if name.value in declared:
            capabilities.append(
                Capability(
                    name=name,
                    present=bool(declared[name.value]),
                    origin=CapabilityOrigin.EXPLICIT_CONFIG,
                    detail="declared in this project's configuration",
                )
            )
            continue
        capabilities.append(discovered[name])

    manuscripts = _manuscripts(tracked) if facts.tracked_known else []
    if capabilities[list(CapabilityName).index(CapabilityName.CAPSULE)].present:
        capsule_present = True
    elif CapabilityName.CAPSULE.value in declared:
        capsule_present = bool(declared[CapabilityName.CAPSULE.value])

    return ProjectProfile(
        project_root=str(root),
        base_commit=head_commit(root) if has_commits(root) else None,
        project_id=project_id,
        registered=registered,
        capsule_present=capsule_present,
        capabilities=capabilities,
        declared_experiments=sorted(declared_experiments),
        manuscript_files=manuscripts,
        discovery_errors=sorted(set(errors)),
    )


# -- discovery -----------------------------------------------------------


def _tracked_paths(root: Path) -> tuple[frozenset[str], bool, str | None]:
    """Return the repository's tracked paths, sorted and bounded.

    Tracked rather than on-disk, and that is the point of the choice: an
    untracked scratch file, a stray virtual environment, or a half-finished
    experiment output must not change what kind of project this is. A capability
    that flips because somebody left a file in the working tree is a capability
    nothing can depend on.
    """

    try:
        output = git(["ls-files", "-z"], cwd=root).stdout
    except GitError as exc:
        return frozenset(), False, f"cannot list tracked files: {exc}"
    entries = sorted(item for item in output.split("\0") if item)
    truncated = len(entries) > MAX_TRACKED_PATHS
    return frozenset(entries[:MAX_TRACKED_PATHS]), truncated, None


def _read_pyproject(
    root: Path, tracked: frozenset[str]
) -> tuple[dict[str, object], str | None]:
    """Return the parsed ``pyproject.toml``, or an empty mapping and a reason."""

    if "pyproject.toml" not in tracked:
        return {}, None
    target = root / "pyproject.toml"
    try:
        raw = target.read_bytes()
    except OSError as exc:
        return {}, f"cannot read pyproject.toml: {exc}"
    try:
        parsed = tomllib.loads(raw.decode("utf-8", errors="replace"))
    except (tomllib.TOMLDecodeError, ValueError) as exc:
        return {}, f"pyproject.toml is not valid TOML: {exc}"
    return parsed, None


def _discover(
    *,
    tracked: frozenset[str],
    capsule_present: bool,
    capsule_unreadable: bool,
    dependencies: frozenset[str],
    tool_sections: frozenset[str],
    metadata_readable: bool,
    declared_experiments: tuple[str, ...],
    check_profile_ids: tuple[str, ...],
    check_profiles_explicit: bool,
    tracked_known: bool,
) -> dict[CapabilityName, Capability]:
    """Return one discovered capability per name. Never raises.

    One guard comes first. If the tracked file list could not be read, or had to
    be cut, then *nothing* structural about this repository is established --
    including the facts that happen to be derivable from a metadata file that
    sorted above the cut. Scattering that check through each capability is how
    the first version of this fix left ``pytest_available`` claiming
    ``repository_metadata`` certainty about a tree it had only seen one file of.
    """

    unavailable = CapabilityOrigin.UNAVAILABLE
    if not tracked_known:
        found: dict[CapabilityName, Capability] = {}
        for name in CapabilityName:
            if name is CapabilityName.CAPSULE:
                # Read from ``.research/`` rather than from the file list, so a
                # list that had to be cut says nothing about it either way.
                found[name] = _capsule_capability(
                    present=capsule_present,
                    unreadable=capsule_unreadable,
                )
            elif name is CapabilityName.EXPERIMENT_REGISTRY or (
                name is CapabilityName.VALIDATION_PROFILES and check_profiles_explicit
            ):
                found[name] = _configured_capability(
                    name, declared_experiments, check_profile_ids
                )
            else:
                found[name] = Capability(
                    name=name,
                    present=False,
                    origin=unavailable,
                    detail="the tracked file list could not be read in full",
                )
        return found
    structure = CapabilityOrigin.DETERMINISTIC_STRUCTURE
    repo_metadata = CapabilityOrigin.REPOSITORY_METADATA

    def structural(
        name: CapabilityName, present: bool, when_present: str, when_absent: str
    ) -> Capability:
        """Return one structural capability, with the detail that is actually true.

        Two strings rather than one. A single detail is written for whichever
        case the author had in mind and is then printed for both, which is how
        the profile came to tell a planner "manuscripts: no -- tracked manuscript
        sources were found". A block that opens by saying these are facts and not
        to contradict them cannot then contradict itself.
        """

        if not tracked_known:
            return Capability(
                name=name,
                present=False,
                origin=unavailable,
                detail="the tracked file list could not be read",
            )
        return Capability(
            name=name,
            present=present,
            origin=structure,
            detail=when_present if present else when_absent,
        )

    has_pyproject = "pyproject.toml" in tracked
    has_uv_lock = "uv.lock" in tracked
    src_packages = _src_packages(tracked)
    flat_packages = _flat_packages(tracked)
    dependency_names = dependencies

    def from_metadata(
        name: CapabilityName, present: bool, when_present: str, when_absent: str
    ) -> Capability:
        if not metadata_readable:
            return Capability(
                name=name,
                present=False,
                origin=unavailable,
                detail="pyproject.toml could not be parsed",
            )
        if not has_pyproject:
            return structural(
                name,
                False,
                when_present,
                "the repository tracks no pyproject.toml to declare it in",
            )
        return Capability(
            name=name,
            present=present,
            origin=repo_metadata,
            detail=when_present if present else when_absent,
        )

    found: dict[CapabilityName, Capability] = {
        CapabilityName.CAPSULE: _capsule_capability(
            present=capsule_present, unreadable=capsule_unreadable
        ),
        CapabilityName.PYPROJECT_TOML: structural(
            CapabilityName.PYPROJECT_TOML,
            has_pyproject,
            "pyproject.toml is tracked",
            "no tracked pyproject.toml",
        ),
        CapabilityName.UV_LOCK: structural(
            CapabilityName.UV_LOCK,
            has_uv_lock,
            "uv.lock is tracked",
            "no tracked uv.lock",
        ),
        CapabilityName.PYTHON_PROJECT: structural(
            CapabilityName.PYTHON_PROJECT,
            has_pyproject or bool(src_packages) or bool(flat_packages),
            "pyproject.toml or an importable package directory is tracked",
            "no tracked pyproject.toml and no importable package directory",
        ),
        CapabilityName.PYTHON_SRC_LAYOUT: structural(
            CapabilityName.PYTHON_SRC_LAYOUT,
            bool(src_packages),
            "tracked importable packages under src/: "
            + ", ".join(sorted(src_packages)),
            "no tracked importable package under src/",
        ),
        CapabilityName.PYTHON_FLAT_LAYOUT: structural(
            CapabilityName.PYTHON_FLAT_LAYOUT,
            bool(flat_packages) and not src_packages,
            "tracked top-level importable packages: "
            + ", ".join(sorted(flat_packages)),
            "no top-level importable package outside src/",
        ),
        CapabilityName.PYTEST_AVAILABLE: from_metadata(
            CapabilityName.PYTEST_AVAILABLE,
            "pytest" in dependency_names or "pytest" in tool_sections,
            "pytest is declared in pyproject.toml",
            "pyproject.toml declares no pytest",
        ),
        CapabilityName.RUFF_AVAILABLE: from_metadata(
            CapabilityName.RUFF_AVAILABLE,
            "ruff" in dependency_names or "ruff" in tool_sections,
            "ruff is declared in pyproject.toml",
            "pyproject.toml declares no ruff",
        ),
        CapabilityName.JULIA_PROJECT: structural(
            CapabilityName.JULIA_PROJECT,
            "Project.toml" in tracked,
            "Project.toml is tracked",
            "no tracked Project.toml",
        ),
        CapabilityName.EXPERIMENT_REGISTRY: Capability(
            name=CapabilityName.EXPERIMENT_REGISTRY,
            present=bool(declared_experiments),
            origin=CapabilityOrigin.EXPLICIT_CONFIG,
            detail=(
                f"{len(declared_experiments)} declared experiment command(s)"
                if declared_experiments
                else "the researcher has declared no experiment command"
            ),
        ),
        CapabilityName.VALIDATION_PROFILES: Capability(
            name=CapabilityName.VALIDATION_PROFILES,
            present=bool(check_profile_ids),
            origin=(
                CapabilityOrigin.EXPLICIT_CONFIG
                if check_profiles_explicit
                else structure
            ),
            detail=(
                "controller-owned checks: " + ", ".join(check_profile_ids)
                if check_profile_ids
                else "no validation profile could be resolved for this project"
            ),
        ),
        CapabilityName.MANUSCRIPTS: structural(
            CapabilityName.MANUSCRIPTS,
            bool(_manuscripts(tracked)),
            "tracked manuscript sources were found",
            "no tracked manuscript source at a name or directory this build reads",
        ),
    }
    return found


def _configured_capability(
    name: CapabilityName,
    declared_experiments: tuple[str, ...],
    check_profile_ids: tuple[str, ...],
) -> Capability:
    """Return a capability that comes from configuration rather than the tree.

    These two survive an unreadable tracked list, because neither was read from
    it: the researcher declared them.
    """

    if name is CapabilityName.EXPERIMENT_REGISTRY:
        return Capability(
            name=name,
            present=bool(declared_experiments),
            origin=CapabilityOrigin.EXPLICIT_CONFIG,
            detail=(
                f"{len(declared_experiments)} declared experiment command(s)"
                if declared_experiments
                else "the researcher has declared no experiment command"
            ),
        )
    return Capability(
        name=name,
        present=bool(check_profile_ids),
        origin=CapabilityOrigin.EXPLICIT_CONFIG,
        detail=(
            "controller-owned checks: " + ", ".join(check_profile_ids)
            if check_profile_ids
            else "no validation profile is configured for this project"
        ),
    )


def _capsule_capability(*, present: bool, unreadable: bool) -> Capability:
    """Return the capsule capability, which decides the whole provenance mode.

    Its own function because it is the one capability with three answers rather
    than two: there is a readable capsule, there is no capsule, or there is
    something at ``.research/`` that could not be read. Only the first two are
    facts a plan may be built on.
    """

    if present:
        return Capability(
            name=CapabilityName.CAPSULE,
            present=True,
            origin=CapabilityOrigin.DETERMINISTIC_STRUCTURE,
            detail="the repository holds a Research Capsule at .research/",
        )
    if unreadable:
        return Capability(
            name=CapabilityName.CAPSULE,
            present=False,
            origin=CapabilityOrigin.REPOSITORY_METADATA,
            detail=(
                "not a usable capsule: a .research/ directory is present but its "
                "project.yaml does not parse, so there are no scientific objects"
            ),
        )
    return Capability(
        name=CapabilityName.CAPSULE,
        present=False,
        origin=CapabilityOrigin.DETERMINISTIC_STRUCTURE,
        detail="no .research/ capsule; this is an ordinary repository",
    )


#: Directories whose tracked contents are read as manuscript sources.
MANUSCRIPT_DIRECTORIES: tuple[str, ...] = ("paper/", "manuscript/", "manuscripts/")

#: Repository-root files that are a manuscript whatever directory convention is used.
MANUSCRIPT_ROOT_FILES: frozenset[str] = frozenset(
    {"main.tex", "manuscript.md", "manuscript.tex", "paper.md", "paper.tex"}
)

#: Suffixes a manuscript source may have.
MANUSCRIPT_SUFFIXES: tuple[str, ...] = (".tex", ".md")


def _manuscripts(tracked: frozenset[str]) -> list[str]:
    """Return tracked manuscript sources, deterministically and conservatively.

    Two rules and no guessing: a named file at the repository root, or a ``.tex``
    or ``.md`` file inside a directory whose name says it holds a manuscript.
    A ``README.md`` is not a manuscript, and neither is every Markdown file in a
    repository that happens to document itself.
    """

    found: set[str] = set()
    for path in tracked:
        if path in MANUSCRIPT_ROOT_FILES:
            found.add(path)
            continue
        if not path.endswith(MANUSCRIPT_SUFFIXES):
            continue
        if any(path.startswith(prefix) for prefix in MANUSCRIPT_DIRECTORIES):
            found.add(path)
    return sorted(found)[:MAX_MANUSCRIPTS]


def _src_packages(tracked: frozenset[str]) -> frozenset[str]:
    """Return importable package names directly under ``src/``."""

    found: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if len(parts) >= 3 and parts[0] == "src" and parts[2] == "__init__.py":
            found.add(parts[1])
    return frozenset(found)


def _flat_packages(tracked: frozenset[str]) -> frozenset[str]:
    """Return importable package names directly at the repository root."""

    found: set[str] = set()
    for path in tracked:
        parts = path.split("/")
        if len(parts) == 2 and parts[1] == "__init__.py" and parts[0] != "src":
            found.add(parts[0])
    return frozenset(found)


def _dependency_names(metadata: dict[str, object]) -> frozenset[str]:
    """Return every distribution named anywhere in this project's dependencies.

    Every list a Python packaging file can put a requirement in, read as
    requirement strings and reduced to their distribution names. A project that
    declares ``pytest>=8`` in a dependency group and one that declares it in
    ``project.dependencies`` are the same fact for this purpose.
    """

    found: set[str] = set()

    def absorb(value: object) -> None:
        if isinstance(value, str):
            name = _requirement_name(value)
            if name:
                found.add(name)
            return
        if isinstance(value, list):
            for item in value:
                absorb(item)
            return
        if isinstance(value, dict):
            for item in value.values():
                absorb(item)

    project = metadata.get("project")
    if isinstance(project, dict):
        absorb(project.get("dependencies"))
        absorb(project.get("optional-dependencies"))
    absorb(metadata.get("dependency-groups"))
    tool = metadata.get("tool")
    if isinstance(tool, dict):
        uv = tool.get("uv")
        if isinstance(uv, dict):
            absorb(uv.get("dev-dependencies"))
    return frozenset(found)


def _requirement_name(value: str) -> str | None:
    """Return the distribution name of one PEP 508 requirement string."""

    text = value.strip()
    for stop in (";", "[", "<", ">", "=", "!", "~", "@", " "):
        index = text.find(stop)
        if index > 0:
            text = text[:index]
    text = text.strip().lower().replace("_", "-")
    return text or None


def _tool_sections(metadata: dict[str, object]) -> frozenset[str]:
    """Return the ``[tool.*]`` table names this project configures."""

    tool = metadata.get("tool")
    if not isinstance(tool, dict):
        return frozenset()
    return frozenset(str(key) for key in tool)


# -- rendering -----------------------------------------------------------


def render_project_profile(profile: ProjectProfile) -> str:
    """Render the profile as the controller-authored context a planner receives.

    Plain lines rather than a fenced data block, because unlike a file body or a
    worker report none of this is somebody else's text: every character was
    produced by this module out of Git and configuration. It still goes through
    ``prompt_safe`` for the two fields that quote the repository -- a path and a
    capability detail naming a package -- since a directory name is chosen by
    whoever wrote the repository.
    """

    mode = profile.provenance_mode
    lines = [
        "# Project profile (deterministic; established by the controller, not by you)",
        "",
        "These are facts. They were read from Git and from this researcher's own",
        "configuration before you were asked anything. Do not restate them as",
        "questions, and do not contradict them.",
        "",
        f"project_root: {prompt_safe(profile.project_root)}",
        f"project_id: {prompt_safe(profile.project_id or 'unregistered')}",
        f"base_commit: {profile.base_commit or 'none (unborn HEAD)'}",
        f"capsule_present: {profile.capsule_present}",
        f"provenance_mode: {mode.value}",
        "",
        "## Capabilities",
    ]
    for item in profile.capabilities:
        value = "yes" if item.present else "no"
        if not item.known:
            value = "unknown"
        detail = prompt_safe(item.detail) if item.detail else ""
        suffix = f"  -- {detail}" if detail else ""
        lines.append(f"- {item.name.value}: {value}  [{item.origin.value}]{suffix}")
    if profile.manuscript_files:
        lines.extend(["", "## Manuscript sources"])
        lines.extend(f"- {prompt_safe(item)}" for item in profile.manuscript_files)
    if profile.discovery_errors:
        lines.extend(["", "## What could not be established"])
        lines.extend(f"- {prompt_safe(item)}" for item in profile.discovery_errors)
    lines.extend(["", "## What this means for your plan", ""])
    if mode is ProvenanceMode.SCIENTIFIC_PROJECT:
        lines.append(
            "This project holds a Research Capsule, so scientific objects exist "
            "and may be cited by their identifiers."
        )
    else:
        lines.append(
            "This project holds NO Research Capsule. There are no Questions, "
            "Hypotheses, Claims, Evidence objects or Decisions, and there are no "
            "identifiers for them. Do not write a plan that assumes any exist, "
            "and do not invent one. Work from the repository's own files, its "
            "deterministic checks, and retrieved literature."
        )
    return "\n".join(lines) + "\n"
