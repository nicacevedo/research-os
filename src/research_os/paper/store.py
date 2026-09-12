"""Where a draft's record lives: runtime state, beside runs and proposals.

The prose itself is not here. It is in the isolated worktree the writer wrote
it in, on a branch, where a researcher reads it with ``git diff`` like any other
change. What lives here is everything needed to judge it: the sources it was
given, the manifest it declared, the deterministic check results, the review,
the prompts, and the ledger.

That split matters. Copying the draft into runtime state would create a second
copy that drifts from the branch, and the branch is the thing the researcher
will actually merge.
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
from research_os.errors import DraftNotFoundError, PaperStoreError
from research_os.paper.models import (
    DRAFT_ID_RE,
    DraftRecord,
    GroundingReport,
    SourcePacket,
    WritingReview,
)
from research_os.paths import state_home

DRAFTS_DIRNAME = "drafts"
DRAFT_FILENAME = "draft.json"
PACKET_FILENAME = "source_packet.json"
MANIFEST_FILENAME = "source_manifest.json"
GROUNDING_FILENAME = "grounding.json"
REVIEW_FILENAME = "review.json"
EVENTS_FILENAME = "events.jsonl"

_SUBDIRECTORIES = ("prompts", "model_outputs", "draft", "review")


def drafts_root() -> Path:
    return state_home() / DRAFTS_DIRNAME


def make_draft_id(*, project_path: str, section: str, created_at: str) -> str:
    material = f"draft-v1\n{project_path}\n{section}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"DRAFT-{stamp}-{digest}"


class DraftStore:
    """Filesystem access to one draft directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def draft_id(self) -> str:
        return self.directory.name

    @property
    def draft_file(self) -> Path:
        return self.directory / DRAFT_FILENAME

    @property
    def packet_file(self) -> Path:
        return self.directory / PACKET_FILENAME

    @property
    def manifest_file(self) -> Path:
        return self.directory / MANIFEST_FILENAME

    @property
    def grounding_file(self) -> Path:
        return self.directory / GROUNDING_FILENAME

    @property
    def review_file(self) -> Path:
        return self.directory / REVIEW_FILENAME

    @property
    def events_file(self) -> Path:
        return self.directory / EVENTS_FILENAME

    @classmethod
    def create(cls, draft: DraftRecord) -> DraftStore:
        directory = drafts_root() / draft.draft_id
        if directory.exists():
            raise PaperStoreError(f"draft directory already exists: {directory}")
        try:
            directory.mkdir(parents=True)
            for name in _SUBDIRECTORIES:
                (directory / name).mkdir()
        except OSError as exc:
            raise PaperStoreError(f"cannot create draft directory: {exc}") from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(draft)
        return store

    @classmethod
    def open(cls, draft_id: str) -> DraftStore:
        if DRAFT_ID_RE.fullmatch(draft_id) is None:
            raise DraftNotFoundError(f"{draft_id!r} is not a Research OS draft id")
        directory = drafts_root() / draft_id
        if not (directory / DRAFT_FILENAME).is_file():
            raise DraftNotFoundError(f"no draft {draft_id} under {drafts_root()}")
        return cls(directory)

    @classmethod
    def list_draft_ids(cls) -> tuple[str, ...]:
        root = drafts_root()
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in root.iterdir()
                if (entry / DRAFT_FILENAME).is_file()
            )
        )

    def load(self) -> DraftRecord:
        try:
            return DraftRecord.model_validate_json(self._read(self.draft_file))
        except PaperStoreError:
            raise
        except Exception as exc:
            raise PaperStoreError(
                f"invalid draft record in {self.draft_file}: {exc}"
            ) from exc

    def save(self, draft: DraftRecord) -> DraftRecord:
        self._atomic_write(self.draft_file, _document(draft.model_dump(mode="json")))
        if draft.manifest is not None:
            self._atomic_write(
                self.manifest_file, _document(draft.manifest.model_dump(mode="json"))
            )
        return draft

    def save_packet(self, packet: SourcePacket) -> SourcePacket:
        self._atomic_write(self.packet_file, _document(packet.model_dump(mode="json")))
        return packet

    def load_packet(self) -> SourcePacket | None:
        if not self.packet_file.is_file():
            return None
        return SourcePacket.model_validate_json(self._read(self.packet_file))

    def save_grounding(self, report: GroundingReport) -> GroundingReport:
        self._atomic_write(
            self.grounding_file, _document(report.model_dump(mode="json"))
        )
        return report

    def save_review(self, review: WritingReview) -> WritingReview:
        self._atomic_write(self.review_file, _document(review.model_dump(mode="json")))
        return review

    def write_text(self, relative_path: str, text: str) -> Path:
        target = self.path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, text)
        return target

    def path(self, *parts: str) -> Path:
        target = self.directory.joinpath(*parts)
        resolved = target.resolve()
        root = self.directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise PaperStoreError(f"path escapes the draft directory: {target}")
        return target

    def relative(self, path: Path) -> str:
        try:
            return path.resolve().relative_to(self.directory.resolve()).as_posix()
        except ValueError:
            return str(path)

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self.event_count() + 1,
            "ts": utc_now(),
            "draft_id": self.draft_id,
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
            raise PaperStoreError(
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
                raise PaperStoreError(
                    f"corrupt event ledger line in {self.events_file}: {exc}"
                ) from exc

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise PaperStoreError(f"cannot read {path}: {exc}") from exc

    def _atomic_write(self, target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise PaperStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise PaperStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


def _document(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
