"""Noncanonical global project-discovery registry.

``project_registry.json`` is disposable infrastructure. It does not store
scientific objects and is never consulted when validating science. Deleting it
does not alter any project file; every entry can be rebuilt by running
``researchctl register-project`` again.

The store is plain JSON written by atomic replacement. It is deliberately not a
database: at this scale a database buys nothing but a dependency and a schema
migration story. Because JSON enforces no shape, everything read back is
validated structurally here, so a corrupt store fails as a ``RegistryError``
rather than as a raw ``KeyError`` or ``TypeError`` from deep inside a caller.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from research_os.errors import RegistryConflictError, RegistryError
from research_os.models import Project
from research_os.paths import data_home

REGISTRY_FILENAME = "project_registry.json"
LEGACY_REGISTRY_FILENAME = "project_registry.sqlite"
SCHEMA_VERSION = 1

_STR_FIELDS = ("project_id", "path", "title", "status", "last_seen")
_INT_FIELDS = ("capsule_version",)
_ENTRY_FIELDS = (*_STR_FIELDS, *_INT_FIELDS)


@dataclass(frozen=True, slots=True)
class RegistryEntry:
    """One project-discovery row plus live/missing availability."""

    project_id: str
    path: str
    title: str
    capsule_version: int
    status: str
    last_seen: str
    availability: str


def registry_path() -> Path:
    """Return the JSON registry path under the current data home."""

    return data_home() / REGISTRY_FILENAME


def legacy_registry_path() -> Path:
    """Return the superseded SQLite registry path under the data home."""

    return data_home() / LEGACY_REGISTRY_FILENAME


def register_project(project: Project, git_root: Path) -> RegistryEntry:
    """Insert or update discovery metadata for ``project`` at ``git_root``."""

    canonical = str(git_root.resolve())
    now = _utc_now()
    entries = _load_entries()

    by_id = {entry["project_id"]: entry for entry in entries}
    by_path = {entry["path"]: entry for entry in entries}

    existing_path = by_path.get(canonical)
    if existing_path is not None and existing_path["project_id"] != project.id:
        raise RegistryConflictError(
            f"path {canonical} is already registered as project "
            f"{existing_path['project_id']}; refusing to change identity"
        )

    existing_id = by_id.get(project.id)
    if existing_id is not None and existing_id["path"] != canonical:
        old_path = Path(existing_id["path"])
        if _live_capsule(old_path):
            raise RegistryConflictError(
                f"project {project.id} is already registered at "
                f"{existing_id['path']}, which still exists as a live capsule"
            )

    row = {
        "project_id": project.id,
        "path": canonical,
        "title": project.title,
        "capsule_version": int(project.capsule_version),
        "status": str(project.status),
        "last_seen": now,
    }
    remaining = [entry for entry in entries if entry["project_id"] != project.id]
    remaining.append(row)
    _write_entries(remaining)
    return _entry_from_row(row)


def list_projects() -> tuple[RegistryEntry, ...]:
    """Return registered projects in deterministic id order.

    Does not create or mutate the registry. A missing store is an empty
    registry, and reading never touches the file's modification time.
    """

    return tuple(_entry_from_row(row) for row in _load_entries())


def _utc_now() -> str:
    """Return the registration timestamp.

    Its own function so tests can pin the clock: ``last_seen`` advances on every
    explicit registration by design.
    """

    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _serialize(entries: list[dict[str, Any]]) -> str:
    """Return the canonical registry document for ``entries``.

    Deterministic as a function of its input: entries sorted by project id,
    object keys sorted, and a trailing newline, so the file stays diffable.
    """

    store = {
        "schema_version": SCHEMA_VERSION,
        "projects": sorted(entries, key=lambda entry: entry["project_id"]),
    }
    return json.dumps(store, ensure_ascii=False, indent=2, sort_keys=True) + "\n"


def _load_entries() -> list[dict[str, Any]]:
    path = registry_path()
    if not path.is_file():
        _reject_legacy_registry()
        return []
    try:
        raw = path.read_bytes()
    except OSError as exc:
        raise RegistryError(f"cannot read project registry: {exc}") from exc
    return _validated_entries(raw, path)


def _validated_entries(raw: bytes, path: Path) -> list[dict[str, Any]]:
    """Return the validated project rows held in ``raw``.

    JSON guarantees no shape, so the whole document is checked before any caller
    can index into it. Every rejection names the store and says it may simply be
    deleted, because the registry is rebuildable metadata, not science.
    """

    hint = f"delete {path} and re-register"
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RegistryError(
            f"cannot read project registry: not valid UTF-8: {exc.reason} "
            f"at byte {exc.start} ({hint})"
        ) from exc
    try:
        store = json.loads(text)
    except ValueError as exc:
        raise RegistryError(f"cannot read project registry: {exc} ({hint})") from exc
    if not isinstance(store, dict):
        raise RegistryError(
            "cannot read project registry: top level is "
            f"{type(store).__name__}, expected object ({hint})"
        )
    version = store.get("schema_version")
    if not _is_int(version):
        raise RegistryError(
            "cannot read project registry: schema_version is missing or not "
            f"an integer ({hint})"
        )
    if version != SCHEMA_VERSION:
        raise RegistryError(f"unsupported project registry schema version {version}")
    projects = store.get("projects")
    if not isinstance(projects, list):
        raise RegistryError(
            "cannot read project registry: projects is "
            f"{type(projects).__name__}, expected list ({hint})"
        )

    entries: list[dict[str, Any]] = []
    for index, entry in enumerate(projects):
        if not isinstance(entry, dict):
            raise RegistryError(
                f"cannot read project registry: entry {index} is "
                f"{type(entry).__name__}, expected object ({hint})"
            )
        for field in _ENTRY_FIELDS:
            if field not in entry:
                raise RegistryError(
                    f"cannot read project registry: entry {index} is missing "
                    f"{field} ({hint})"
                )
        for field in _STR_FIELDS:
            if not isinstance(entry[field], str):
                raise RegistryError(
                    f"cannot read project registry: entry {index} field "
                    f"{field} has type {type(entry[field]).__name__}, "
                    f"expected str ({hint})"
                )
        for field in _INT_FIELDS:
            if not _is_int(entry[field]):
                raise RegistryError(
                    f"cannot read project registry: entry {index} field "
                    f"{field} has type {type(entry[field]).__name__}, "
                    f"expected int ({hint})"
                )
        entries.append({field: entry[field] for field in _ENTRY_FIELDS})

    _reject_duplicates(entries, "project_id", "duplicate project id", hint)
    _reject_duplicates(entries, "path", "duplicate path", hint)
    return sorted(entries, key=lambda entry: entry["project_id"])


def _reject_duplicates(
    entries: list[dict[str, Any]],
    field: str,
    message: str,
    hint: str,
) -> None:
    """Reject a duplicated key that SQLite used to make impossible.

    ``register_project`` decides identity conflicts from these keys, so a store
    holding two rows for one id or one path has no defined meaning.
    """

    seen: set[str] = set()
    for entry in entries:
        value = entry[field]
        if value in seen:
            raise RegistryError(
                f"cannot read project registry: {message} {value} ({hint})"
            )
        seen.add(value)


def _write_entries(entries: list[dict[str, Any]]) -> None:
    """Replace the registry atomically.

    The temporary file is created in the destination directory so ``os.replace``
    is a same-filesystem rename: an interrupted write leaves either the previous
    store or the new one, never a truncated one.
    """

    path = registry_path()
    document = _serialize(entries)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise RegistryError(f"cannot create project registry: {exc}") from exc
    try:
        handle_fd, tmp_name = tempfile.mkstemp(
            dir=path.parent,
            prefix=".project_registry.",
            suffix=".tmp",
        )
    except OSError as exc:
        raise RegistryError(f"cannot create project registry: {exc}") from exc
    tmp = Path(tmp_name)
    try:
        with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
            handle.write(document)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        raise RegistryError(f"registry write failed: {exc}") from exc
    finally:
        tmp.unlink(missing_ok=True)


def _reject_legacy_registry() -> None:
    """Report a superseded SQLite registry instead of ignoring it.

    The old store is never read, imported, or deleted. Silently starting from an
    empty JSON registry would make ``researchctl projects`` look as though the
    projects had vanished, and importing it would keep a database dependency in
    the kernel for six columns of rebuildable metadata.
    """

    legacy = legacy_registry_path()
    if not legacy.is_file():
        return
    raise RegistryError(
        f"a legacy SQLite project registry exists at {legacy}. The project "
        "registry is now JSON and is disposable discovery metadata, not "
        "scientific state. Re-register each project with "
        "researchctl register-project <path>, then delete the legacy file. "
        "Nothing in your project files is affected."
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _live_capsule(path: Path) -> bool:
    try:
        return path.is_dir() and (path / ".research").exists()
    except OSError:
        return False


def _entry_from_row(row: dict[str, Any]) -> RegistryEntry:
    registered = Path(row["path"])
    availability = "AVAILABLE" if registered.exists() else "MISSING"
    return RegistryEntry(
        project_id=row["project_id"],
        path=row["path"],
        title=row["title"],
        capsule_version=int(row["capsule_version"]),
        status=row["status"],
        last_seen=row["last_seen"],
        availability=availability,
    )
