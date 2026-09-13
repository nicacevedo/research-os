"""Where experiment runs live, and how one is addressed.

Runtime state, under the Research OS state home, like automation runs and
proposals. What makes this store slightly different is what it holds: an
experiment produced bytes, and the packet a human reads points at those bytes by
digest. So the store keeps the run record, the captured streams, the generated
job script, and the evidence packet -- and it does *not* keep the artifacts
themselves, which stay in the worktree where the experiment wrote them.

That separation is deliberate. Copying results into runtime state would double
the storage of every experiment and create a second copy that can drift from the
first. The digest is what makes the reference sound; a reader who needs the file
looks in the worktree the run names.
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
from research_os.errors import ExperimentRunNotFoundError, ExperimentStoreError
from research_os.experiment.models import (
    EXPERIMENT_RUN_ID_RE,
    EvidencePacket,
    ExperimentRun,
)
from research_os.paths import state_home

EXPERIMENTS_DIRNAME = "experiments"
RUN_FILENAME = "run.json"
PACKET_FILENAME = "evidence_packet.json"
EVENTS_FILENAME = "events.jsonl"
SCRIPT_FILENAME = "job.sbatch"


def experiments_root() -> Path:
    return state_home() / EXPERIMENTS_DIRNAME


def make_experiment_run_id(
    *, project_path: str, task_name: str, created_at: str, attempt: str = ""
) -> str:
    """Return the run id determined by these inputs.

    Deterministic like every other id here: the same project, task, and second
    produce the same id, so a duplicate is a visible collision rather than two
    directories holding one execution.

    ``attempt`` distinguishes executions that are deliberately *not* the same
    work despite sharing all of that. Automation run ids gained the same
    discriminator for the same reason, and experiments needed it once their
    record started being written before the worktree: a run that fails during
    preparation now leaves a directory, so retrying it in the same second asked
    for an id that already existed and was refused. Retrying promptly after a
    failure is the ordinary thing to do, so it must not be the one thing that
    cannot work.
    """

    material = (
        f"experiment-run-v1\n{project_path}\n{task_name}\n{created_at}\n{attempt}"
        if attempt
        else f"experiment-run-v1\n{project_path}\n{task_name}\n{created_at}"
    )
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"XRUN-{stamp}-{digest}"


class ExperimentStore:
    """Filesystem access to one experiment run directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def run_id(self) -> str:
        return self.directory.name

    @property
    def run_file(self) -> Path:
        return self.directory / RUN_FILENAME

    @property
    def packet_file(self) -> Path:
        return self.directory / PACKET_FILENAME

    @property
    def events_file(self) -> Path:
        return self.directory / EVENTS_FILENAME

    @property
    def script_file(self) -> Path:
        return self.directory / SCRIPT_FILENAME

    @classmethod
    def create(cls, run: ExperimentRun) -> ExperimentStore:
        directory = experiments_root() / run.run_id
        if directory.exists():
            raise ExperimentStoreError(
                f"experiment run directory already exists: {directory}"
            )
        try:
            directory.mkdir(parents=True)
            (directory / "logs").mkdir()
        except OSError as exc:
            raise ExperimentStoreError(
                f"cannot create experiment run directory: {exc}"
            ) from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(run)
        return store

    @classmethod
    def open(cls, run_id: str) -> ExperimentStore:
        if EXPERIMENT_RUN_ID_RE.fullmatch(run_id) is None:
            raise ExperimentRunNotFoundError(
                f"{run_id!r} is not a Research OS experiment run id"
            )
        directory = experiments_root() / run_id
        if not (directory / RUN_FILENAME).is_file():
            raise ExperimentRunNotFoundError(
                f"no experiment run {run_id} under {experiments_root()}"
            )
        return cls(directory)

    @classmethod
    def list_run_ids(cls) -> tuple[str, ...]:
        root = experiments_root()
        if not root.is_dir():
            return ()
        return tuple(
            sorted(
                entry.name
                for entry in root.iterdir()
                if (entry / RUN_FILENAME).is_file()
            )
        )

    def load(self) -> ExperimentRun:
        try:
            return ExperimentRun.model_validate_json(self._read(self.run_file))
        except ExperimentStoreError:
            raise
        except Exception as exc:
            raise ExperimentStoreError(
                f"invalid experiment run in {self.run_file}: {exc}"
            ) from exc

    def save(self, run: ExperimentRun) -> ExperimentRun:
        self._atomic_write(self.run_file, _document(run.model_dump(mode="json")))
        return run

    def save_packet(self, packet: EvidencePacket) -> EvidencePacket:
        self._atomic_write(self.packet_file, _document(packet.model_dump(mode="json")))
        return packet

    def load_packet(self) -> EvidencePacket | None:
        if not self.packet_file.is_file():
            return None
        try:
            return EvidencePacket.model_validate_json(self._read(self.packet_file))
        except ExperimentStoreError:
            raise
        except Exception as exc:
            raise ExperimentStoreError(
                f"invalid evidence packet in {self.packet_file}: {exc}"
            ) from exc

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
            raise ExperimentStoreError(
                f"path escapes the experiment run directory: {target}"
            )
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
            raise ExperimentStoreError(
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
                raise ExperimentStoreError(
                    f"corrupt event ledger line in {self.events_file}: {exc}"
                ) from exc

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ExperimentStoreError(f"cannot read {path}: {exc}") from exc

    def _atomic_write(self, target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise ExperimentStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise ExperimentStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


def _document(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
