"""Where technical assessments live: runtime state, never inside a project.

The same rule the proposal store follows, and here it matters more. An
assessment is a reading of somebody's repository; writing it into that
repository would put a machine's opinion in the researcher's Git history, and
writing it under ``.research/`` would put it where canonical scientific state
lives. It goes under the state home, beside runs and proposals, and deleting it
loses an opinion and a provenance ledger and nothing else.
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from research_os.assessment.models import ASSESSMENT_ID_RE, TechnicalAssessment
from research_os.automation.models import utc_now
from research_os.errors import AssessmentNotFoundError, AssessmentStoreError
from research_os.paths import state_home

ASSESSMENTS_DIRNAME = "assessments"
ASSESSMENT_FILENAME = "assessment.json"
EVENTS_FILENAME = "events.jsonl"


def assessments_root() -> Path:
    return state_home() / ASSESSMENTS_DIRNAME


def make_assessment_id(*, project_path: str, goal: str, created_at: str) -> str:
    """Return the assessment id determined by these inputs.

    Deterministic rather than random, exactly like a run id and a proposal id: a
    duplicate is then a visible collision instead of two directories holding the
    same reading.
    """

    material = f"assessment-id-v1\n{project_path}\n{goal}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"TA-{stamp}-{digest}"


class AssessmentStore:
    """Filesystem access to one assessment directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def assessment_id(self) -> str:
        return self.directory.name

    @property
    def assessment_file(self) -> Path:
        return self.directory / ASSESSMENT_FILENAME

    @property
    def events_file(self) -> Path:
        return self.directory / EVENTS_FILENAME

    @classmethod
    def create(cls, assessment: TechnicalAssessment) -> AssessmentStore:
        directory = assessments_root() / assessment.assessment_id
        if directory.exists():
            raise AssessmentStoreError(
                f"assessment directory already exists: {directory}"
            )
        try:
            directory.mkdir(parents=True)
            (directory / "prompts").mkdir()
            (directory / "model_outputs").mkdir()
        except OSError as exc:
            raise AssessmentStoreError(
                f"cannot create assessment directory: {exc}"
            ) from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(assessment)
        return store

    @classmethod
    def open(cls, assessment_id: str) -> AssessmentStore:
        if ASSESSMENT_ID_RE.fullmatch(assessment_id) is None:
            raise AssessmentNotFoundError(
                f"{assessment_id!r} is not a Research OS assessment id"
            )
        directory = assessments_root() / assessment_id
        if not (directory / ASSESSMENT_FILENAME).is_file():
            raise AssessmentNotFoundError(
                f"no assessment {assessment_id} under {assessments_root()}"
            )
        return cls(directory)

    @classmethod
    def list_assessment_ids(cls) -> tuple[str, ...]:
        root = assessments_root()
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in root.iterdir()
                if (entry / ASSESSMENT_FILENAME).is_file()
            )
        )

    def load(self) -> TechnicalAssessment:
        raw = self._read(self.assessment_file)
        try:
            return TechnicalAssessment.model_validate_json(raw)
        except Exception as exc:
            raise AssessmentStoreError(
                f"invalid assessment in {self.assessment_file}: {exc}"
            ) from exc

    def save(self, assessment: TechnicalAssessment) -> TechnicalAssessment:
        self._atomic_write(
            self.assessment_file, _document(assessment.model_dump(mode="json"))
        )
        return assessment

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self.event_count() + 1,
            "ts": utc_now(),
            "assessment_id": self.assessment_id,
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
            raise AssessmentStoreError(
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
                raise AssessmentStoreError(
                    f"corrupt event ledger line in {self.events_file}: {exc}"
                ) from exc

    def write_text(self, relative_path: str, text: str) -> str:
        target = self.path(*relative_path.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        self._atomic_write(target, text)
        return target.resolve().relative_to(self.directory.resolve()).as_posix()

    def path(self, *parts: str) -> Path:
        target = self.directory.joinpath(*parts)
        resolved = target.resolve()
        root = self.directory.resolve()
        if resolved != root and root not in resolved.parents:
            raise AssessmentStoreError(
                f"path escapes the assessment directory: {target}"
            )
        return target

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise AssessmentStoreError(f"cannot read {path}: {exc}") from exc

    def _atomic_write(self, target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise AssessmentStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise AssessmentStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


def _document(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
