"""Where research runs live: runtime state, beside runs, never inside a project.

A research run holds no scientific truth. It records what was planned, what was
dispatched, what each dispatch produced, and what a person was asked. Every
artifact it names -- an automation run, a proposal, an experiment run, a draft --
lives in the store that already owns that kind of thing, and this directory keeps
only the pointer. Deleting it loses an orchestration record and nothing in Git.

Small and file-based on purpose, exactly like the automation and proposal stores:
``run.json`` is replaced whole by ``os.replace`` and ``events.jsonl`` is only ever
appended to.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from research_os.automation.models import utc_now
from research_os.errors import ResearchRunNotFoundError, ResearchStoreError
from research_os.paths import state_home
from research_os.research.models import RESEARCH_RUN_ID_RE, ResearchRun

RESEARCH_DIRNAME = "research"
RUN_FILENAME = "run.json"
EVENTS_FILENAME = "events.jsonl"


def research_root() -> Path:
    return state_home() / RESEARCH_DIRNAME


def make_research_run_id(*, project_path: str, goal: str, created_at: str) -> str:
    """Return the research run id determined by these inputs.

    Deterministic rather than random, like every other id in this system: the
    same project, goal, and second produce the same id, so starting the same run
    twice in one second is a visible collision rather than two directories
    quietly doing the same work.
    """

    material = f"research-run-id-v1\n{project_path}\n{goal}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"RR-{stamp}-{digest}"


class ResearchStore:
    """Filesystem access to one research run directory."""

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
    def create(cls, run: ResearchRun) -> ResearchStore:
        directory = research_root() / run.run_id
        if directory.exists():
            raise ResearchStoreError(
                f"research run directory already exists: {directory}"
            )
        try:
            directory.mkdir(parents=True)
            (directory / "prompts").mkdir()
            (directory / "model_outputs").mkdir()
            (directory / "plan").mkdir()
        except OSError as exc:
            raise ResearchStoreError(
                f"cannot create research run directory: {exc}"
            ) from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(run)
        return store

    @classmethod
    def open(cls, run_id: str) -> ResearchStore:
        if RESEARCH_RUN_ID_RE.fullmatch(run_id) is None:
            raise ResearchRunNotFoundError(
                f"{run_id!r} is not a Research OS research run id"
            )
        directory = research_root() / run_id
        if not (directory / RUN_FILENAME).is_file():
            raise ResearchRunNotFoundError(
                f"no research run {run_id} under {research_root()}"
            )
        return cls(directory)

    @classmethod
    def list_run_ids(cls) -> tuple[str, ...]:
        """Return every research run id on disk, newest last.

        Research run ids begin with a UTC timestamp, so lexical order is
        chronological.
        """

        root = research_root()
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in root.iterdir()
                if (entry / RUN_FILENAME).is_file()
            )
        )

    def load(self) -> ResearchRun:
        raw = self._read(self.run_file)
        try:
            return ResearchRun.model_validate_json(raw)
        except Exception as exc:
            raise ResearchStoreError(
                f"invalid research run in {self.run_file}: {exc}"
            ) from exc

    def save(self, run: ResearchRun) -> ResearchRun:
        """Persist ``run`` whole, stamping the time it was last changed.

        Returned rather than discarded so every caller keeps working from the
        object that is actually on disk. The controller threads this return
        value through every step, which is what makes a crashed run's file an
        accurate account of what it had spent.
        """

        stamped = run.model_copy(update={"updated_at": utc_now()})
        self._atomic_write(self.run_file, _document(stamped.model_dump(mode="json")))
        return stamped

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
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
            raise ResearchStoreError(
                f"cannot append to {self.events_file}: {exc}"
            ) from exc
        return record

    def event_count(self) -> int:
        return sum(1 for _ in self.iter_events())

    def iter_events(self) -> Iterator[dict[str, Any]]:
        if not self.events_file.is_file():
            return
        for line in self._read(self.events_file).splitlines():
            if not line.strip():
                continue
            try:
                yield json.loads(line)
            except ValueError as exc:
                raise ResearchStoreError(
                    f"corrupt event ledger line in {self.events_file}: {exc}"
                ) from exc

    def write_text(self, relative_path: str, text: str) -> str:
        target = self.path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, text)
        return target.resolve().relative_to(self.directory.resolve()).as_posix()

    def write_json(self, relative_path: str, payload: Any) -> str:
        return self.write_text(relative_path, _document(payload))

    def read_json(self, relative_path: str) -> Any:
        target = self.path(*relative_path.split("/"))
        if not target.is_file():
            return None
        try:
            return json.loads(self._read(target))
        except ValueError as exc:
            raise ResearchStoreError(f"invalid JSON in {target}: {exc}") from exc

    def path(self, *parts: str) -> Path:
        """Return an absolute path inside the run directory.

        Refuses anything outside it. Nothing a model produced ever reaches this,
        but the guard is unconditional: a path helper that is only safe when its
        callers are careful is not a guard.
        """

        target = self.directory.joinpath(*parts)
        resolved = target.resolve()
        root = self.directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise ResearchStoreError(
                f"path escapes the research run directory: {target}"
            )
        return target

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ResearchStoreError(f"cannot read {path}: {exc}") from exc

    def _atomic_write(self, target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise ResearchStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise ResearchStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


def _document(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
