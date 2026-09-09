"""Research Capsule filesystem layout, YAML loading, and project validation.

M3 owns project location, canonical file discovery, YAML safety, and
filename/directory binding. Scientific-object semantics remain in M2.
This module does not consult the global project registry.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import yaml
from pydantic import ValidationError

from research_os.errors import (
    E_DUPLICATE_YAML_KEY,
    E_FILENAME_ID_MISMATCH,
    E_MISSING_CAPSULE_FILE,
    E_SCHEMA,
    E_UNEXPECTED_FILE,
    E_UNSAFE_PATH,
    E_WRONG_OBJECT_DIRECTORY,
    E_YAML_PARSE,
    W_RESERVED_DIRECTORY,
    W_UNEXPECTED_FILE,
    CapsuleError,
    CapsuleExistsError,
    Finding,
    InvalidIdError,
    NotAGitRepositoryError,
    ProjectIdRequiredError,
    Severity,
    ValidationReport,
)
from research_os.ids import object_type_from_id, validate_project_id
from research_os.models import Project, ScientificObject, parse_object
from research_os.validate import validate_objects

REQUIRED_CAPSULE_FILES = (
    ".gitignore",
    "project.yaml",
    "CHARTER.md",
    "STATE.md",
)

TYPED_DIRECTORIES: dict[str, str] = {
    "questions": "question",
    "ideas": "idea",
    "hypotheses": "hypothesis",
    "assumptions": "assumption",
    "claims": "claim",
    "decisions": "decision",
    "reviews": "review",
    "evidence": "evidence",
}

EXPERIMENT_DIRECTORY = "experiments"
EXPERIMENT_TYPE = "experiment"
RUNTIME_DIRECTORY = "runtime"
RESERVED_DIRECTORIES = ("literature", "work_orders", "handoffs")

CHARTER_TEMPLATE = "# Charter\n"
STATE_TEMPLATE = "# State\n"
CAPSULE_GITIGNORE = "/runtime/\n"

_SEVERITY_ORDER = {Severity.ERROR: 0, Severity.WARNING: 1, Severity.INFO: 2}
_OBJECT_TYPE_ORDER = (
    "question",
    "idea",
    "hypothesis",
    "assumption",
    "claim",
    "decision",
    "experiment",
    "review",
    "evidence",
)


class DuplicateYamlKeyError(yaml.constructor.ConstructorError):
    """Raised when a YAML mapping contains a duplicate key."""

    def __init__(self, key: object, mark: yaml.Mark | None = None) -> None:
        self.key = key
        problem = f"duplicate mapping key {key!r}"
        super().__init__(None, None, problem, mark)


class UniqueKeySafeLoader(yaml.SafeLoader):
    """SafeLoader that rejects duplicate mapping keys."""

    def construct_mapping(
        self,
        node: yaml.nodes.MappingNode,
        deep: bool = False,
    ) -> dict[Any, Any]:
        if not isinstance(node, yaml.nodes.MappingNode):
            raise yaml.constructor.ConstructorError(
                None,
                None,
                f"expected a mapping node, but found {node.id}",
                node.start_mark,
            )
        self.flatten_mapping(node)
        mapping: dict[Any, Any] = {}
        for key_node, value_node in node.value:
            key = self.construct_object(key_node, deep=deep)
            if key in mapping:
                raise DuplicateYamlKeyError(key, key_node.start_mark)
            mapping[key] = self.construct_object(value_node, deep=deep)
        return mapping


@dataclass(frozen=True, slots=True)
class ProjectValidationReport:
    """Structured capsule validation result for CLI rendering."""

    git_root: Path
    capsule: Path | None
    project: Project | None
    objects: tuple[ScientificObject, ...] = ()
    findings: tuple[Finding, ...] = ()

    @property
    def errors(self) -> tuple[Finding, ...]:
        return tuple(item for item in self.findings if item.severity is Severity.ERROR)

    @property
    def warnings(self) -> tuple[Finding, ...]:
        return tuple(
            item for item in self.findings if item.severity is Severity.WARNING
        )

    @property
    def ok(self) -> bool:
        return not self.errors


@dataclass(frozen=True, slots=True)
class ProjectStatusReport:
    """Read-only capsule status derived from files, not the registry."""

    git_root: Path
    project: Project
    object_counts: dict[str, int]
    error_count: int
    warning_count: int
    state_sqlite_present: bool
    findings: tuple[Finding, ...] = ()


@dataclass(frozen=True, slots=True)
class _LoadedObject:
    obj: ScientificObject
    source: str


@dataclass(frozen=True, slots=True)
class _YamlDocument:
    data: dict[str, Any]


def resolve_git_root(path: Path | str) -> Path:
    """Return the Git repository root that contains ``path``."""

    raw = Path(path).expanduser()
    if not raw.exists():
        raise NotAGitRepositoryError(f"path does not exist: {raw}")
    lookup = raw if raw.is_dir() else raw.parent
    try:
        completed = subprocess.run(
            ["git", "-C", str(lookup), "rev-parse", "--show-toplevel"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise NotAGitRepositoryError("git is not available on PATH") from exc
    if completed.returncode != 0:
        raise NotAGitRepositoryError(f"path is not inside a Git repository: {raw}")
    return Path(completed.stdout.strip()).resolve()


def init_project(
    path: Path | str = ".",
    *,
    project_id: str | None = None,
    title: str | None = None,
) -> tuple[Path, Project]:
    """Create a new Research Capsule at the Git root of ``path``.

    Returns ``(git_root, project)``. Does not consult or update the registry.
    """

    git_root = resolve_git_root(path)
    capsule = git_root / ".research"
    if capsule.exists() or capsule.is_symlink():
        raise CapsuleExistsError(
            f"Research Capsule already exists at {capsule}; refusing to overwrite"
        )

    basename = git_root.name
    if project_id is None:
        try:
            project_id = validate_project_id(basename)
        except InvalidIdError as exc:
            raise ProjectIdRequiredError(
                f"repository directory name {basename!r} is not a valid project "
                "id; pass --id <valid-project-id>"
            ) from exc
    else:
        try:
            project_id = validate_project_id(project_id)
        except InvalidIdError as exc:
            raise CapsuleError(f"invalid project id: {project_id!r}") from exc

    project = Project.model_validate(
        {
            "id": project_id,
            "title": basename if title is None else title,
            "capsule_version": 1,
            "status": "active",
        }
    )

    tmp = Path(tempfile.mkdtemp(prefix=".research.tmp.", dir=git_root))
    try:
        _write_new_capsule(tmp, project)
        _validate_new_capsule(tmp, project)
        try:
            tmp.rename(capsule)
        except FileExistsError as exc:
            raise CapsuleExistsError(
                f"Research Capsule already exists at {capsule}; refusing to overwrite"
            ) from exc
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return git_root, project


def load_project_identity(path: Path | str) -> tuple[Path, Project]:
    """Load ``project.yaml`` for registration without validating science."""

    git_root = resolve_git_root(path)
    capsule = git_root / ".research"
    if not capsule.exists():
        raise CapsuleError(f"no Research Capsule at {capsule}")
    if not _contained(capsule, git_root):
        raise CapsuleError(f"unsafe capsule path: {capsule}")
    project_file = capsule / "project.yaml"
    if not project_file.is_file() and not project_file.is_symlink():
        raise CapsuleError(f"missing { _rel(project_file, git_root) }")
    if not _contained(project_file, git_root):
        raise CapsuleError(f"unsafe path: { _rel(project_file, git_root) }")
    try:
        document = load_yaml_mapping(project_file)
    except DuplicateYamlKeyError as exc:
        raise CapsuleError(
            f"duplicate YAML mapping key in {_rel(project_file, git_root)}: {exc.key!r}"
        ) from exc
    except yaml.YAMLError as exc:
        raise CapsuleError(
            f"invalid YAML in {_rel(project_file, git_root)}: {exc}"
        ) from exc
    except CapsuleError:
        raise
    try:
        project = Project.model_validate(document.data)
    except ValidationError as exc:
        raise CapsuleError(
            f"invalid project.yaml at {_rel(project_file, git_root)}: "
            f"{_schema_message(exc)}"
        ) from exc
    return git_root, project


def validate_project(path: Path | str = ".") -> ProjectValidationReport:
    """Validate a Research Capsule. Never writes files or consults the registry.

    Cross-object scientific validation is project-scoped, so it runs only
    when ``project.yaml`` yields a valid identity. A capsule without one
    already reports a hard error, and validating objects without project
    identity would silently drop the review and digest guarantees.
    """

    git_root = resolve_git_root(path)
    findings: list[Finding] = []
    capsule = git_root / ".research"
    if not capsule.exists() and not capsule.is_symlink():
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_MISSING_CAPSULE_FILE,
                message="missing Research Capsule directory .research",
                source=".research",
            )
        )
        return ProjectValidationReport(
            git_root=git_root,
            capsule=None,
            project=None,
            findings=_sorted_findings(findings),
        )
    if not _contained(capsule, git_root):
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_UNSAFE_PATH,
                message=".research resolves outside the Git repository",
                source=".research",
            )
        )
        return ProjectValidationReport(
            git_root=git_root,
            capsule=capsule,
            project=None,
            findings=_sorted_findings(findings),
        )

    project = _load_required_project(capsule, git_root, findings)
    _warn_reserved_directories(capsule, git_root, findings)
    loaded = _discover_objects(capsule, git_root, findings)
    objects = tuple(item.obj for item in loaded)
    if project is not None:
        m2_report = validate_objects(objects, project_id=project.id)
        findings.extend(_attach_sources(m2_report, loaded))
    return ProjectValidationReport(
        git_root=git_root,
        capsule=capsule,
        project=project,
        objects=objects,
        findings=_sorted_findings(findings),
    )


def project_status(path: Path | str = ".") -> ProjectStatusReport:
    """Return file-derived capsule status. Does not create runtime or registry."""

    report = validate_project(path)
    if report.project is None:
        raise CapsuleError("cannot determine project identity from project.yaml")
    counts = {object_type: 0 for object_type in _OBJECT_TYPE_ORDER}
    for obj in report.objects:
        counts[str(obj.type)] = counts.get(str(obj.type), 0) + 1
    state_sqlite = report.git_root / ".research" / "runtime" / "state.sqlite"
    present = state_sqlite.is_file()
    return ProjectStatusReport(
        git_root=report.git_root,
        project=report.project,
        object_counts=counts,
        error_count=len(report.errors),
        warning_count=len(report.warnings),
        state_sqlite_present=present,
        findings=report.findings,
    )


def load_yaml_mapping(path: Path) -> _YamlDocument:
    """Load a single YAML mapping with SafeLoader semantics and unique keys."""

    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CapsuleError(f"cannot read {path}: {exc}") from exc
    try:
        documents = list(yaml.load_all(text, Loader=UniqueKeySafeLoader))
    except DuplicateYamlKeyError:
        raise
    except yaml.YAMLError as exc:
        raise yaml.YAMLError(str(exc)) from exc
    if len(documents) == 0:
        raise CapsuleError("YAML document is empty")
    if len(documents) != 1:
        raise CapsuleError("YAML file must contain exactly one document")
    data = documents[0]
    if not isinstance(data, dict):
        raise CapsuleError("YAML document must be a mapping")
    return _YamlDocument(data=data)


def _write_new_capsule(capsule: Path, project: Project) -> None:
    (capsule / ".gitignore").write_text(CAPSULE_GITIGNORE, encoding="utf-8")
    (capsule / "project.yaml").write_text(_dump_project_yaml(project), encoding="utf-8")
    (capsule / "CHARTER.md").write_text(CHARTER_TEMPLATE, encoding="utf-8")
    (capsule / "STATE.md").write_text(STATE_TEMPLATE, encoding="utf-8")


def _validate_new_capsule(capsule: Path, project: Project) -> None:
    for name in REQUIRED_CAPSULE_FILES:
        path = capsule / name
        if not path.is_file():
            raise CapsuleError(f"failed to write required capsule file {name}")
    gitignore = (capsule / ".gitignore").read_text(encoding="utf-8")
    if gitignore != CAPSULE_GITIGNORE:
        raise CapsuleError("generated .research/.gitignore is not /runtime/")
    loaded = Project.model_validate(load_yaml_mapping(capsule / "project.yaml").data)
    if loaded != project:
        raise CapsuleError("generated project.yaml did not round-trip")


def _dump_project_yaml(project: Project) -> str:
    payload = {
        "id": project.id,
        "title": project.title,
        "capsule_version": int(project.capsule_version),
        "status": str(project.status),
    }
    dumped = yaml.safe_dump(
        payload,
        default_flow_style=False,
        allow_unicode=True,
        sort_keys=False,
        explicit_start=False,
        explicit_end=False,
    )
    if not dumped.endswith("\n"):
        dumped += "\n"
    return dumped


def _load_required_project(
    capsule: Path,
    git_root: Path,
    findings: list[Finding],
) -> Project | None:
    for name in REQUIRED_CAPSULE_FILES:
        path = capsule / name
        source = _rel(path, git_root)
        if not path.exists() and not path.is_symlink():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_MISSING_CAPSULE_FILE,
                    message=f"missing required capsule file {source}",
                    source=source,
                )
            )
            continue
        if not _contained(path, git_root):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNSAFE_PATH,
                    message=f"{source} resolves outside the Git repository",
                    source=source,
                )
            )
    project_file = capsule / "project.yaml"
    if not project_file.exists() and not project_file.is_symlink():
        return None
    if not _contained(project_file, git_root):
        return None
    return _parse_project_file(project_file, git_root, findings)


def _parse_project_file(
    project_file: Path,
    git_root: Path,
    findings: list[Finding],
) -> Project | None:
    source = _rel(project_file, git_root)
    mapping = _load_mapping_file(project_file, git_root, findings)
    if mapping is None:
        return None
    try:
        return Project.model_validate(mapping)
    except ValidationError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_SCHEMA,
                message=_schema_message(exc),
                field=_schema_field(exc),
                source=source,
            )
        )
        return None


def _warn_reserved_directories(
    capsule: Path,
    git_root: Path,
    findings: list[Finding],
) -> None:
    known = set(TYPED_DIRECTORIES)
    known.update(
        {
            EXPERIMENT_DIRECTORY,
            RUNTIME_DIRECTORY,
            *RESERVED_DIRECTORIES,
        }
    )
    entries = _safe_iterdir(capsule, git_root, findings, source=_rel(capsule, git_root))
    if entries is None:
        return
    for entry in entries:
        name = entry.name
        source = _rel(entry, git_root)
        if name in {".gitignore", "project.yaml", "CHARTER.md", "STATE.md"}:
            continue
        if name == RUNTIME_DIRECTORY:
            continue
        if name in TYPED_DIRECTORIES or name == EXPERIMENT_DIRECTORY:
            continue
        if name in RESERVED_DIRECTORIES or name not in known:
            if _unsafe_entry(entry, git_root, findings, source):
                continue
            if entry.is_dir() and _directory_has_entries(entry):
                findings.append(
                    Finding(
                        severity=Severity.WARNING,
                        code=W_RESERVED_DIRECTORY,
                        message=(
                            f"{source} is not parsed in R0 and its contents were ignored"
                        ),
                        source=source,
                    )
                )
            elif entry.is_file() or entry.is_symlink():
                if name.endswith((".yaml", ".yml")) and name != "project.yaml":
                    findings.append(
                        Finding(
                            severity=Severity.ERROR,
                            code=E_UNEXPECTED_FILE,
                            message=f"unexpected YAML file {source}",
                            source=source,
                        )
                    )
                elif name.endswith(".md") and name not in {"CHARTER.md", "STATE.md"}:
                    findings.append(
                        Finding(
                            severity=Severity.WARNING,
                            code=W_UNEXPECTED_FILE,
                            message=f"noncanonical Markdown file {source}",
                            source=source,
                        )
                    )
                else:
                    findings.append(
                        Finding(
                            severity=Severity.WARNING,
                            code=W_UNEXPECTED_FILE,
                            message=f"unexpected file {source}",
                            source=source,
                        )
                    )


def _discover_objects(
    capsule: Path,
    git_root: Path,
    findings: list[Finding],
) -> list[_LoadedObject]:
    loaded: list[_LoadedObject] = []
    for dirname, expected_type in TYPED_DIRECTORIES.items():
        directory = capsule / dirname
        if not directory.exists() and not directory.is_symlink():
            continue
        source = _rel(directory, git_root)
        if _unsafe_entry(directory, git_root, findings, source):
            continue
        if not directory.is_dir():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNEXPECTED_FILE,
                    message=f"{source} must be a directory",
                    source=source,
                )
            )
            continue
        loaded.extend(
            _scan_typed_directory(
                directory,
                expected_type=expected_type,
                git_root=git_root,
                findings=findings,
            )
        )
    experiments = capsule / EXPERIMENT_DIRECTORY
    if experiments.exists() or experiments.is_symlink():
        source = _rel(experiments, git_root)
        if not _unsafe_entry(experiments, git_root, findings, source):
            if not experiments.is_dir():
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_UNEXPECTED_FILE,
                        message=f"{source} must be a directory",
                        source=source,
                    )
                )
            else:
                loaded.extend(
                    _scan_experiments(experiments, git_root, findings)
                )
    return loaded


def _scan_typed_directory(
    directory: Path,
    *,
    expected_type: str,
    git_root: Path,
    findings: list[Finding],
) -> list[_LoadedObject]:
    loaded: list[_LoadedObject] = []
    typed_dir = directory.name
    for file_path, rel in _walk_files(directory, git_root, findings):
        if rel.endswith(".yml"):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNEXPECTED_FILE,
                    message=f"unsupported YAML extension {rel}",
                    source=rel,
                )
            )
            continue
        if rel.endswith(".yaml"):
            kind, object_id = _classify_ordinary_yaml(rel, typed_dir, expected_type)
            if kind == "canonical":
                obj = _load_scientific_object(
                    file_path,
                    git_root=git_root,
                    findings=findings,
                    expected_id=object_id,
                    expected_type=expected_type,
                )
                if obj is not None:
                    loaded.append(_LoadedObject(obj=obj, source=rel))
            elif kind == "wrong_dir":
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_WRONG_OBJECT_DIRECTORY,
                        message=(
                            f"{rel} is in {typed_dir}/ but the filename id "
                            f"encodes type {object_type_from_id(object_id)}"
                        ),
                        object_id=object_id,
                        source=rel,
                    )
                )
                _load_scientific_object(
                    file_path,
                    git_root=git_root,
                    findings=findings,
                    expected_id=object_id,
                    expected_type=expected_type,
                    bind=False,
                )
            elif kind == "bad_name":
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_FILENAME_ID_MISMATCH,
                        message=f"{rel} is not a canonical object filename",
                        source=rel,
                    )
                )
                _load_mapping_file(file_path, git_root, findings)
            else:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_UNEXPECTED_FILE,
                        message=f"unexpected YAML file {rel}",
                        source=rel,
                    )
                )
                _load_mapping_file(file_path, git_root, findings)
            continue
        if rel.endswith(".md"):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_UNEXPECTED_FILE,
                    message=f"noncanonical Markdown file {rel}",
                    source=rel,
                )
            )
            continue
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code=W_UNEXPECTED_FILE,
                message=f"unexpected file {rel}",
                source=rel,
            )
        )
    return loaded


def _scan_experiments(
    directory: Path,
    git_root: Path,
    findings: list[Finding],
) -> list[_LoadedObject]:
    loaded: list[_LoadedObject] = []
    _check_experiment_directories(directory, git_root, findings)
    for file_path, rel in _walk_files(directory, git_root, findings):
        if rel.endswith(".yml"):
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNEXPECTED_FILE,
                    message=f"unsupported YAML extension {rel}",
                    source=rel,
                )
            )
            continue
        if rel.endswith(".yaml"):
            kind, object_id = _classify_experiment_yaml(rel)
            if kind == "canonical":
                obj = _load_scientific_object(
                    file_path,
                    git_root=git_root,
                    findings=findings,
                    expected_id=object_id,
                    expected_type=EXPERIMENT_TYPE,
                )
                if obj is not None:
                    loaded.append(_LoadedObject(obj=obj, source=rel))
            elif kind == "id_mismatch_path":
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_FILENAME_ID_MISMATCH,
                        message=f"{rel} is not a canonical experiment path",
                        object_id=object_id,
                        source=rel,
                    )
                )
                _load_mapping_file(file_path, git_root, findings)
            else:
                findings.append(
                    Finding(
                        severity=Severity.ERROR,
                        code=E_UNEXPECTED_FILE,
                        message=f"unexpected YAML file {rel}",
                        source=rel,
                    )
                )
                _load_mapping_file(file_path, git_root, findings)
            continue
        if rel.endswith(".md"):
            findings.append(
                Finding(
                    severity=Severity.WARNING,
                    code=W_UNEXPECTED_FILE,
                    message=f"noncanonical Markdown file {rel}",
                    source=rel,
                )
            )
            continue
        findings.append(
            Finding(
                severity=Severity.WARNING,
                code=W_UNEXPECTED_FILE,
                message=f"unexpected file {rel}",
                source=rel,
            )
        )
    return loaded


def _check_experiment_directories(
    directory: Path,
    git_root: Path,
    findings: list[Finding],
) -> None:
    entries = _safe_iterdir(
        directory,
        git_root,
        findings,
        source=_rel(directory, git_root),
    )
    if entries is None:
        return
    for entry in entries:
        source = _rel(entry, git_root)
        if _unsafe_entry(entry, git_root, findings, source):
            continue
        if not entry.is_dir():
            continue
        try:
            actual_type = object_type_from_id(entry.name)
        except InvalidIdError:
            continue
        if actual_type != EXPERIMENT_TYPE:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_WRONG_OBJECT_DIRECTORY,
                    message=(
                        f"{source} is in experiments/ but the directory name "
                        f"encodes type {actual_type}"
                    ),
                    object_id=entry.name,
                    source=source,
                )
            )
            continue
        manifest = entry / "manifest.yaml"
        if not manifest.exists() and not manifest.is_symlink():
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_MISSING_CAPSULE_FILE,
                    message=f"missing {_rel(manifest, git_root)}",
                    object_id=entry.name,
                    source=_rel(manifest, git_root),
                )
            )


def _classify_ordinary_yaml(
    rel: str,
    typed_dir: str,
    expected_type: str,
) -> tuple[str, str]:
    parts = Path(rel).parts
    capsule_rel = Path(*parts[1:]) if parts and parts[0] == ".research" else Path(rel)
    rel_parts = capsule_rel.parts
    if (
        len(rel_parts) == 2
        and rel_parts[0] == typed_dir
        and rel_parts[1].endswith(".yaml")
    ):
        stem = rel_parts[1][:-5]
        try:
            actual_type = object_type_from_id(stem)
        except InvalidIdError:
            return "bad_name", stem
        if actual_type != expected_type:
            return "wrong_dir", stem
        return "canonical", stem
    return "unexpected", ""


def _classify_experiment_yaml(rel: str) -> tuple[str, str]:
    parts = Path(rel).parts
    capsule_rel = Path(*parts[1:]) if parts and parts[0] == ".research" else Path(rel)
    rel_parts = capsule_rel.parts
    if (
        len(rel_parts) == 3
        and rel_parts[0] == EXPERIMENT_DIRECTORY
        and rel_parts[2] == "manifest.yaml"
    ):
        stem = rel_parts[1]
        try:
            actual_type = object_type_from_id(stem)
        except InvalidIdError:
            return "id_mismatch_path", stem
        if actual_type != EXPERIMENT_TYPE:
            return "id_mismatch_path", stem
        return "canonical", stem
    return "unexpected", ""


def _load_scientific_object(
    path: Path,
    *,
    git_root: Path,
    findings: list[Finding],
    expected_id: str,
    expected_type: str,
    bind: bool = True,
) -> ScientificObject | None:
    source = _rel(path, git_root)
    mapping = _load_mapping_file(path, git_root, findings)
    if mapping is None:
        return None
    try:
        obj = parse_object(mapping)
    except ValidationError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_SCHEMA,
                message=_schema_message(exc),
                object_id=mapping.get("id") if isinstance(mapping.get("id"), str) else None,
                field=_schema_field(exc),
                source=source,
            )
        )
        return None
    except (ValueError, TypeError) as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_SCHEMA,
                message=str(exc),
                object_id=mapping.get("id") if isinstance(mapping.get("id"), str) else None,
                source=source,
            )
        )
        return None
    if not bind:
        return None
    if obj.id != expected_id:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_FILENAME_ID_MISMATCH,
                message=(
                    f"{source} filename/directory id {expected_id} does not "
                    f"match object id {obj.id}"
                ),
                object_id=obj.id,
                field="id",
                source=source,
            )
        )
        return None
    if str(obj.type) != expected_type:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_WRONG_OBJECT_DIRECTORY,
                message=(
                    f"{source} is in a {expected_type} directory but object "
                    f"type is {obj.type}"
                ),
                object_id=obj.id,
                field="type",
                source=source,
            )
        )
        return None
    return obj


def _load_mapping_file(
    path: Path,
    git_root: Path,
    findings: list[Finding],
) -> dict[str, Any] | None:
    source = _rel(path, git_root)
    try:
        document = load_yaml_mapping(path)
    except DuplicateYamlKeyError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_DUPLICATE_YAML_KEY,
                message=f"duplicate mapping key {exc.key!r}",
                field=str(exc.key) if _is_field_name(exc.key) else None,
                source=source,
            )
        )
        return None
    except yaml.YAMLError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_YAML_PARSE,
                message=str(exc).strip() or "YAML parse error",
                source=source,
            )
        )
        return None
    except CapsuleError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_YAML_PARSE,
                message=str(exc),
                source=source,
            )
        )
        return None
    return document.data


def _walk_files(
    directory: Path,
    git_root: Path,
    findings: list[Finding],
) -> list[tuple[Path, str]]:
    collected: list[tuple[Path, str]] = []
    seen: set[Path] = set()
    stack = [directory]
    while stack:
        current = stack.pop()
        source = _rel(current, git_root)
        if _unsafe_entry(current, git_root, findings, source):
            continue
        try:
            resolved = current.resolve()
        except OSError:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNSAFE_PATH,
                    message=f"{source} resolves outside the Git repository",
                    source=source,
                )
            )
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.name)
        except OSError as exc:
            findings.append(
                Finding(
                    severity=Severity.ERROR,
                    code=E_UNSAFE_PATH,
                    message=f"cannot read {source}: {exc}",
                    source=source,
                )
            )
            continue
        for entry in entries:
            rel = _rel(entry, git_root)
            if _unsafe_entry(entry, git_root, findings, rel):
                continue
            if entry.is_dir():
                stack.append(entry)
            else:
                collected.append((entry, rel))
    collected.sort(key=lambda item: item[1])
    return collected


def _safe_iterdir(
    directory: Path,
    git_root: Path,
    findings: list[Finding],
    *,
    source: str,
) -> list[Path] | None:
    if _unsafe_entry(directory, git_root, findings, source):
        return None
    try:
        return sorted(directory.iterdir(), key=lambda item: item.name)
    except OSError as exc:
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_UNSAFE_PATH,
                message=f"cannot read {source}: {exc}",
                source=source,
            )
        )
        return None


def _unsafe_entry(
    path: Path,
    git_root: Path,
    findings: list[Finding],
    source: str,
) -> bool:
    if not _contained(path, git_root):
        findings.append(
            Finding(
                severity=Severity.ERROR,
                code=E_UNSAFE_PATH,
                message=f"{source} resolves outside the Git repository",
                source=source,
            )
        )
        return True
    return False


def _directory_has_entries(directory: Path) -> bool:
    try:
        next(directory.iterdir())
    except StopIteration:
        return False
    except OSError:
        return True
    return True


def _contained(path: Path, git_root: Path) -> bool:
    try:
        resolved = path.resolve()
        root = git_root.resolve()
        resolved.relative_to(root)
        return True
    except (ValueError, OSError):
        return False


def _rel(path: Path, git_root: Path) -> str:
    try:
        return path.relative_to(git_root).as_posix()
    except ValueError:
        try:
            return path.resolve().relative_to(git_root.resolve()).as_posix()
        except (ValueError, OSError):
            return path.as_posix()


def _attach_sources(
    report: ValidationReport,
    loaded: Sequence[_LoadedObject],
) -> list[Finding]:
    paths_by_id: dict[str, list[str]] = {}
    for item in loaded:
        paths_by_id.setdefault(item.obj.id, []).append(item.source)
    attached: list[Finding] = []
    for finding in report.findings:
        source = None
        if finding.object_id is not None:
            sources = paths_by_id.get(finding.object_id, [])
            if len(sources) == 1:
                source = sources[0]
        attached.append(replace(finding, source=source))
    return attached


def _sorted_findings(findings: Iterable[Finding]) -> tuple[Finding, ...]:
    return tuple(
        sorted(
            findings,
            key=lambda item: (
                _SEVERITY_ORDER[item.severity],
                item.source or "",
                item.object_id or "",
                item.code,
                item.field or "",
                item.reference or "",
                item.message,
            ),
        )
    )


def _schema_message(exc: ValidationError) -> str:
    errors = exc.errors()
    if not errors:
        return "schema validation failed"
    parts: list[str] = []
    for error in errors:
        loc = ".".join(str(item) for item in error.get("loc", ()))
        message = error.get("msg", "invalid value")
        parts.append(f"{loc}: {message}" if loc else message)
    return "; ".join(parts)


def _schema_field(exc: ValidationError) -> str | None:
    errors = exc.errors()
    if not errors:
        return None
    loc = errors[0].get("loc", ())
    if not loc:
        return None
    return ".".join(str(item) for item in loc)


def _is_field_name(key: object) -> bool:
    return isinstance(key, str)
