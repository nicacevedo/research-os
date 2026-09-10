"""Runtime run store: atomic JSON state plus an append-only event ledger.

Runs live under the Research OS state home, never inside a project capsule.
``DESIGN_INVARIANTS.md`` assigns runtime state to ``~/.local/state/research-os``
and canonical science to Git-tracked capsule files, so an automation run records
orchestration where orchestration belongs: deleting a run directory loses a
provenance ledger and costs nothing scientific.

There is no database. ``run.json`` is replaced whole by ``os.replace`` so an
interrupted write leaves the previous state intact, and ``events.jsonl`` is only
ever appended to, so the ledger cannot be silently rewritten.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from research_os.automation.models import RUN_ID_RE, AutomationRun, utc_now
from research_os.errors import RunNotFoundError, RunStoreError
from research_os.paths import state_home

RUNS_DIRNAME = "runs"
WORKTREES_DIRNAME = "worktrees"
LOCKS_DIRNAME = "locks"
RUN_FILENAME = "run.json"
EVENTS_FILENAME = "events.jsonl"
WORKTREES_FILENAME = "worktrees.json"

_SUBDIRECTORIES = (
    "context",
    "prompts",
    "model_outputs",
    "logs",
    "plan",
    "analysis",
    "checks",
    "execution",
    "reviews",
)


def runs_root() -> Path:
    """Return the directory holding every automation run."""

    return state_home() / RUNS_DIRNAME


def worktrees_root() -> Path:
    """Return the directory automation worktrees are created under.

    Deliberately outside every project repository: a write-enabled worker must
    never be handed the researcher's canonical checkout.
    """

    return state_home() / WORKTREES_DIRNAME


def locks_root() -> Path:
    """Return the directory holding exclusive worktree locks."""

    return state_home() / LOCKS_DIRNAME


def make_run_id(*, project_path: str, goal: str, created_at: str) -> str:
    """Return the run id determined by these inputs.

    Deterministic rather than random: the same project, goal, and second yield
    the same id, so a duplicate is a visible collision instead of a second
    directory holding the same work.
    """

    material = f"run-id-v1\n{project_path}\n{goal}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"RUN-{stamp}-{digest}"


class RunStore:
    """Filesystem access to one run directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def run_id(self) -> str:
        return self.directory.name

    @property
    def run_file(self) -> Path:
        return self.directory / RUN_FILENAME

    @property
    def events_file(self) -> Path:
        return self.directory / EVENTS_FILENAME

    @classmethod
    def create(cls, run: AutomationRun) -> RunStore:
        """Create the run directory tree and write the initial state."""

        directory = runs_root() / run.run_id
        if directory.exists():
            raise RunStoreError(f"run directory already exists: {directory}")
        try:
            directory.mkdir(parents=True)
            for name in _SUBDIRECTORIES:
                (directory / name).mkdir()
        except OSError as exc:
            raise RunStoreError(f"cannot create run directory: {exc}") from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(run)
        return store

    @classmethod
    def open(cls, run_id: str) -> RunStore:
        """Open an existing run directory.

        The id is checked against its grammar before it becomes a path, so a
        mistyped or crafted argument cannot address a directory outside the run
        store.
        """

        if RUN_ID_RE.fullmatch(run_id) is None:
            raise RunNotFoundError(f"{run_id!r} is not a Research OS run id")
        directory = runs_root() / run_id
        if not (directory / RUN_FILENAME).is_file():
            raise RunNotFoundError(f"no automation run {run_id} under {runs_root()}")
        return cls(directory)

    @classmethod
    def list_run_ids(cls) -> tuple[str, ...]:
        """Return every run id on disk, newest id last.

        Run ids begin with a UTC timestamp, so lexical order is chronological.
        """

        root = runs_root()
        if not root.is_dir():
            return ()
        found = [
            entry.name for entry in root.iterdir() if (entry / RUN_FILENAME).is_file()
        ]
        return tuple(sorted(found))

    def load(self) -> AutomationRun:
        """Return the persisted run state."""

        try:
            raw = self.run_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunStoreError(f"cannot read {self.run_file}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise RunStoreError(f"cannot parse {self.run_file}: {exc}") from exc
        try:
            return AutomationRun.model_validate(data)
        except Exception as exc:
            raise RunStoreError(f"invalid run state in {self.run_file}: {exc}") from exc

    def save(self, run: AutomationRun) -> AutomationRun:
        """Persist ``run`` atomically and return it with a fresh timestamp."""

        stamped = run.model_copy(update={"updated_at": utc_now()})
        document = (
            json.dumps(
                stamped.model_dump(mode="json"),
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            )
            + "\n"
        )
        self._atomic_write(self.run_file, document)
        self._write_worktree_projection(stamped)
        return stamped

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
        """Append one compact ledger entry and return it.

        Append-only by construction: the file is opened in append mode and never
        rewritten, so an earlier entry cannot be edited by a later action.
        """

        record: dict[str, Any] = {
            "seq": self.event_count() + 1,
            "ts": utc_now(),
            "run_id": self.run_id,
            "event": event,
        }
        record.update(fields)
        line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
        try:
            with self.events_file.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise RunStoreError(f"cannot append to {self.events_file}: {exc}") from exc
        return record

    def event_count(self) -> int:
        return sum(1 for _ in self.iter_events())

    def iter_events(self) -> Iterator[dict[str, Any]]:
        """Yield every ledger entry in append order."""

        if not self.events_file.is_file():
            return
        try:
            text = self.events_file.read_text(encoding="utf-8")
        except OSError as exc:
            raise RunStoreError(f"cannot read {self.events_file}: {exc}") from exc
        for line in text.splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise RunStoreError(
                    f"corrupt event ledger line in {self.events_file}: {exc}"
                ) from exc

    def path(self, *parts: str) -> Path:
        """Return an absolute path inside the run directory."""

        target = self.directory.joinpath(*parts)
        resolved = target.resolve()
        root = self.directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise RunStoreError(f"path escapes the run directory: {target}")
        return target

    def relative(self, path: Path) -> str:
        """Return ``path`` as a run-relative POSIX string when it is inside."""

        try:
            return path.resolve().relative_to(self.directory.resolve()).as_posix()
        except ValueError:
            return str(path)

    def write_text(self, relative_path: str, text: str) -> str:
        """Write an artifact under the run directory and return its stored path."""

        target = self.path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, text)
        return self.relative(target)

    def write_json(self, relative_path: str, payload: Any) -> str:
        document = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        return self.write_text(relative_path, document)

    def _write_worktree_projection(self, run: AutomationRun) -> None:
        """Mirror worktree records into a small standalone file.

        An operator looking for the checkout a failed run left behind should not
        have to read the whole run document to find it.
        """

        payload = {
            "run_id": run.run_id,
            "worktrees": [item.model_dump(mode="json") for item in run.worktrees],
        }
        document = (
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        )
        self._atomic_write(self.directory / WORKTREES_FILENAME, document)

    def _atomic_write(self, target: Path, text: str) -> None:
        """Replace ``target`` whole, never leaving a truncated file behind."""

        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent,
                prefix=f".{target.name}.",
                suffix=".tmp",
            )
        except OSError as exc:
            raise RunStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise RunStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
