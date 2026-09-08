"""Noncanonical global project-discovery registry.

``project_registry.sqlite`` is disposable infrastructure. It does not store
scientific objects and is never consulted when validating science.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from research_os.errors import RegistryConflictError, RegistryError
from research_os.models import Project
from research_os.paths import data_home

REGISTRY_FILENAME = "project_registry.sqlite"
SCHEMA_VERSION = 1

_CREATE_PROJECTS = """
CREATE TABLE projects (
    project_id TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    title TEXT NOT NULL,
    capsule_version INTEGER NOT NULL,
    status TEXT NOT NULL,
    last_seen TEXT NOT NULL
)
"""


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
    """Return the SQLite path under the current data home."""

    return data_home() / REGISTRY_FILENAME


def register_project(project: Project, git_root: Path) -> RegistryEntry:
    """Insert or update discovery metadata for ``project`` at ``git_root``."""

    canonical = str(git_root.resolve())
    now = _utc_now()
    conn = _connect_rw()
    try:
        with conn:
            existing_id = conn.execute(
                "SELECT * FROM projects WHERE project_id = ?",
                (project.id,),
            ).fetchone()
            existing_path = conn.execute(
                "SELECT * FROM projects WHERE path = ?",
                (canonical,),
            ).fetchone()
            if existing_path is not None and existing_path["project_id"] != project.id:
                raise RegistryConflictError(
                    f"path {canonical} is already registered as project "
                    f"{existing_path['project_id']}; refusing to change identity"
                )
            if existing_id is not None and existing_id["path"] != canonical:
                old_path = Path(existing_id["path"])
                if _live_capsule(old_path):
                    raise RegistryConflictError(
                        f"project {project.id} is already registered at "
                        f"{existing_id['path']}, which still exists as a live capsule"
                    )
                conn.execute(
                    """
                    UPDATE projects
                    SET path = ?, title = ?, capsule_version = ?, status = ?,
                        last_seen = ?
                    WHERE project_id = ?
                    """,
                    (
                        canonical,
                        project.title,
                        int(project.capsule_version),
                        str(project.status),
                        now,
                        project.id,
                    ),
                )
            elif existing_id is not None:
                conn.execute(
                    """
                    UPDATE projects
                    SET title = ?, capsule_version = ?, status = ?, last_seen = ?
                    WHERE project_id = ? AND path = ?
                    """,
                    (
                        project.title,
                        int(project.capsule_version),
                        str(project.status),
                        now,
                        project.id,
                        canonical,
                    ),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO projects (
                        project_id, path, title, capsule_version, status, last_seen
                    )
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        project.id,
                        canonical,
                        project.title,
                        int(project.capsule_version),
                        str(project.status),
                        now,
                    ),
                )
        row = conn.execute(
            "SELECT * FROM projects WHERE project_id = ?",
            (project.id,),
        ).fetchone()
    except sqlite3.Error as exc:
        raise RegistryError(f"registry write failed: {exc}") from exc
    finally:
        conn.close()
    if row is None:
        raise RegistryError(f"registry write did not persist project {project.id}")
    return _entry_from_row(row)


def list_projects() -> tuple[RegistryEntry, ...]:
    """Return registered projects in deterministic id order.

    Does not create or mutate the registry. A missing database is an empty
    registry.
    """

    path = registry_path()
    if not path.is_file():
        return ()
    try:
        conn = sqlite3.connect(
            f"file:{path.resolve().as_posix()}?mode=ro",
            uri=True,
        )
    except sqlite3.Error as exc:
        raise RegistryError(f"cannot read project registry: {exc}") from exc
    conn.row_factory = sqlite3.Row
    try:
        try:
            rows = conn.execute(
                "SELECT * FROM projects ORDER BY project_id COLLATE BINARY"
            ).fetchall()
        except sqlite3.Error as exc:
            raise RegistryError(f"cannot read project registry: {exc}") from exc
        return tuple(_entry_from_row(row) for row in rows)
    finally:
        conn.close()


def _connect_rw() -> sqlite3.Connection:
    path = registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(path)
    except OSError as exc:
        raise RegistryError(f"cannot create project registry: {exc}") from exc
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        with conn:
            _ensure_schema(conn)
    except sqlite3.Error as exc:
        conn.close()
        raise RegistryError(f"cannot initialize project registry: {exc}") from exc
    return conn


def _ensure_schema(conn: sqlite3.Connection) -> None:
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version == 0:
        conn.execute(_CREATE_PROJECTS)
        conn.execute(
            "CREATE UNIQUE INDEX idx_projects_path ON projects(path)"
        )
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
        return
    if version != SCHEMA_VERSION:
        raise RegistryError(
            f"unsupported project registry schema version {version}"
        )


def _live_capsule(path: Path) -> bool:
    try:
        return path.is_dir() and (path / ".research").exists()
    except OSError:
        return False


def _utc_now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _entry_from_row(row: sqlite3.Row) -> RegistryEntry:
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
