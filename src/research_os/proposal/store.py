"""Where proposals live: runtime state, beside runs, never inside a project.

The same rule the automation run store follows, for the same reason. A proposal
is orchestration output: it records what a worker suggested, what an assessor
said about it, and what a human promoted. Deleting a proposal directory loses a
suggestion and a provenance ledger; it loses nothing scientific, because
anything scientific about it was promoted into the capsule and is in Git.

Small and file-based on purpose. ``proposal.json`` is replaced whole by
``os.replace``, ``events.jsonl`` is only appended to, and there is no database:
at this scale one would buy a dependency and a migration story in exchange for
nothing.
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
from research_os.errors import ProposalNotFoundError, ProposalStoreError
from research_os.paths import state_home
from research_os.proposal.models import (
    PROPOSAL_ID_RE,
    DeclineRecord,
    PromotionRecord,
    ProposalAssessment,
    ResearchProposal,
)

PROPOSALS_DIRNAME = "proposals"
PROPOSAL_FILENAME = "proposal.json"
ASSESSMENT_FILENAME = "assessment.json"
PROMOTIONS_FILENAME = "promotions.jsonl"
DECLINES_FILENAME = "declines.jsonl"
EVENTS_FILENAME = "events.jsonl"


def proposals_root() -> Path:
    return state_home() / PROPOSALS_DIRNAME


def make_proposal_id(*, project_path: str, goal: str, created_at: str) -> str:
    """Return the proposal id determined by these inputs.

    Deterministic rather than random, exactly like a run id: the same project,
    goal, and second produce the same id, so a duplicate is a visible collision
    instead of two directories holding the same suggestion.
    """

    material = f"proposal-id-v1\n{project_path}\n{goal}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"PROP-{stamp}-{digest}"


def reserved_proposal_id(*, reservation_key: str, moment: str | None = None) -> str:
    """Return the proposal id one caller-reserved identity always produces.

    For a caller that runs inside a crash-resuming runtime. ``make_proposal_id``
    is deterministic in project, goal and *second*, and the second is exactly
    what a retry does not reproduce -- so a retried proposal got a new id, a new
    directory, and the store had no way to tell that two directories held one
    logical decision.

    This derives the whole id, timestamp included, from the reservation key. The
    key is the action's stable identity -- the run, the cycle, the action, the
    grounding digest -- and never the attempt, so the retry computes the same id,
    finds the directory the interrupted attempt created, and adopts it.

    The timestamp in the id is therefore not when the proposal was made. That is
    a real cost and it is the right trade: ``created_at`` on the proposal itself
    records the time, and an id that sorts by attempt time is an id that cannot
    be recomputed. ``moment`` exists so a caller that wants the id to *sort*
    near its creation can supply a stamp it will also be able to reproduce.
    """

    digest = hashlib.sha256(
        f"reserved-proposal-v1\n{reservation_key}".encode()
    ).hexdigest()
    # A fixed, obviously-not-a-real-time stamp when the caller supplies none.
    # The id has to match PROPOSAL_ID_RE, and any plausible timestamp here
    # would be read as "when this was proposed" -- which it is not, because the
    # id must be recomputable by a retry that happens later.
    stamp = (
        "19700101T000000Z"
        if moment is None
        else moment.replace("-", "").replace(":", "")
    )
    return f"PROP-{stamp}-{digest[:8]}"


class ProposalStore:
    """Filesystem access to one proposal directory."""

    def __init__(self, directory: Path) -> None:
        self.directory = directory

    @property
    def proposal_id(self) -> str:
        return self.directory.name

    @property
    def proposal_file(self) -> Path:
        return self.directory / PROPOSAL_FILENAME

    @property
    def assessment_file(self) -> Path:
        return self.directory / ASSESSMENT_FILENAME

    @property
    def promotions_file(self) -> Path:
        return self.directory / PROMOTIONS_FILENAME

    @property
    def declines_file(self) -> Path:
        return self.directory / DECLINES_FILENAME

    @property
    def events_file(self) -> Path:
        return self.directory / EVENTS_FILENAME

    @classmethod
    def adopt_or_create(cls, proposal: ResearchProposal) -> tuple[ProposalStore, bool]:
        """Open the proposal this id already names, or create it. ``(store, created)``.

        For a caller with a reserved id whose previous attempt may have got as
        far as writing the directory. Adoption is deliberately narrow: the
        directory must already hold a *complete, valid* ``proposal.json``, and
        then the existing proposal is returned unchanged rather than
        overwritten, because the stored one is what a person may already have
        read.

        A directory that exists but holds no valid proposal is a half-written
        one, and it is moved aside rather than completed: completing it would
        mean guessing which of two attempts' content belongs there, and
        refusing it wedged the cycle permanently, because a reserved id cannot
        be regenerated.
        """

        directory = proposals_root() / proposal.proposal_id
        if (directory / PROPOSAL_FILENAME).is_file():
            store = cls(directory)
            store.load()
            return store, False
        if directory.exists():
            # A directory with no valid proposal in it: a crash inside
            # `create`, between the `mkdir` and the `save`. Moved aside rather
            # than completed or refused.
            #
            # Refusing was the previous behaviour and it was a permanent wedge,
            # which an adversarial review executed: `create` raises "proposal
            # directory already exists", and because a reserved id is a pure
            # function of the reservation key, *no* retry can ever get a
            # different one. That cycle could never produce its proposal again.
            #
            # Completing it would mean guessing which of two attempts' content
            # belongs there. Moving it aside is safe because it is runtime
            # state that nobody has read -- it has no `proposal.json`, so
            # nothing could have rendered it.
            aside = directory.with_name(f"{directory.name}.partial")
            index = 1
            while aside.exists():
                aside = directory.with_name(f"{directory.name}.partial-{index}")
                index += 1
            try:
                directory.rename(aside)
            except OSError as exc:
                raise ProposalStoreError(
                    f"{directory} holds no valid proposal and could not be "
                    f"moved aside: {exc}"
                ) from exc
        return cls.create(proposal), True

    @classmethod
    def create(cls, proposal: ResearchProposal) -> ProposalStore:
        directory = proposals_root() / proposal.proposal_id
        if directory.exists():
            raise ProposalStoreError(f"proposal directory already exists: {directory}")
        try:
            directory.mkdir(parents=True)
            (directory / "prompts").mkdir()
            (directory / "model_outputs").mkdir()
        except OSError as exc:
            raise ProposalStoreError(
                f"cannot create proposal directory: {exc}"
            ) from exc
        store = cls(directory)
        store.events_file.touch()
        store.save(proposal)
        return store

    @classmethod
    def open(cls, proposal_id: str) -> ProposalStore:
        if PROPOSAL_ID_RE.fullmatch(proposal_id) is None:
            raise ProposalNotFoundError(
                f"{proposal_id!r} is not a Research OS proposal id"
            )
        directory = proposals_root() / proposal_id
        if not (directory / PROPOSAL_FILENAME).is_file():
            raise ProposalNotFoundError(
                f"no proposal {proposal_id} under {proposals_root()}"
            )
        return cls(directory)

    @classmethod
    def list_proposal_ids(cls) -> tuple[str, ...]:
        """Return every proposal id on disk, newest last.

        Ordered by each proposal's own ``created_at``, not by its id. The id
        used to be the order -- it begins with a UTC timestamp, so lexical order
        was chronological -- and that stopped being true the moment the runtime
        started reserving identities. A reserved id's timestamp is derived from
        the reservation key so that a retry recomputes it, which means it cannot
        also be the attempt's clock; :func:`reserved_proposal_id` stamps
        ``19700101T000000Z`` rather than a plausible-looking lie. An adversarial
        review pointed out the consequence: every runtime proposal sorted to the
        front of the researcher's decision queue, before every proposal they
        made themselves, in digest order among themselves.

        So the order comes from the document. ``created_at`` is a real timestamp
        on every proposal, reserved or not. A file that cannot be read or has no
        usable timestamp falls back to its id, which keeps the listing total and
        keeps an unreadable proposal visible rather than dropping it.
        """

        root = proposals_root()
        if not root.is_dir():
            return ()
        entries = [
            entry for entry in root.iterdir() if (entry / PROPOSAL_FILENAME).is_file()
        ]
        return tuple(
            entry.name
            for entry in sorted(entries, key=lambda item: cls._order_key(item))
        )

    @staticmethod
    def _order_key(directory: Path) -> tuple[str, str]:
        """``(created_at, id)`` for one proposal directory, cheaply.

        The id is the tiebreak, so two proposals created in the same second have
        a stable order. Any failure to read the timestamp yields the empty
        string, which sorts such a proposal first -- visible, and in front of
        the queue rather than silently absent from it.
        """

        created = ""
        try:
            document = json.loads(
                (directory / PROPOSAL_FILENAME).read_text(encoding="utf-8")
            )
            if isinstance(document, dict):
                created = str(document.get("created_at") or "")
        except (OSError, ValueError):
            created = ""
        return (created, directory.name)

    def load(self) -> ResearchProposal:
        raw = self._read(self.proposal_file)
        try:
            return ResearchProposal.model_validate_json(raw)
        except Exception as exc:
            raise ProposalStoreError(
                f"invalid proposal in {self.proposal_file}: {exc}"
            ) from exc

    def save(self, proposal: ResearchProposal) -> ResearchProposal:
        self._atomic_write(
            self.proposal_file, _document(proposal.model_dump(mode="json"))
        )
        return proposal

    def save_assessment(self, assessment: ProposalAssessment) -> ProposalAssessment:
        self._atomic_write(
            self.assessment_file, _document(assessment.model_dump(mode="json"))
        )
        return assessment

    def load_assessment(self) -> ProposalAssessment | None:
        if not self.assessment_file.is_file():
            return None
        raw = self._read(self.assessment_file)
        try:
            return ProposalAssessment.model_validate_json(raw)
        except Exception as exc:
            raise ProposalStoreError(
                f"invalid assessment in {self.assessment_file}: {exc}"
            ) from exc

    def record_promotion(self, record: PromotionRecord) -> PromotionRecord:
        """Append one promotion. Append-only: a promotion is a thing that happened."""

        line = json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n"
        try:
            with self.promotions_file.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProposalStoreError(
                f"cannot append to {self.promotions_file}: {exc}"
            ) from exc
        self.append_event(
            "item_promoted",
            item_id=record.item_id,
            object_id=record.object_id,
            object_type=record.object_type,
            object_status=record.object_status,
            written_path=record.written_path,
            basis=str(record.basis),
        )
        return record

    def record_decline(self, record: DeclineRecord) -> DeclineRecord:
        """Append one decline. Append-only, exactly like a promotion.

        **The interactive-terminal check is here, in the writer, not only in
        the command.** An adversarial review pointed out that the structural
        test guarding this -- an AST scan of the runtime package for a call
        named ``record_decline`` -- is defeated by ``getattr(store, "record_" +
        "decline")`` or by ``operator.methodcaller``, and that the runtime
        already holds a live ``ProposalStore`` for the deduplication read. A
        guard that can be stepped around by spelling the name differently is a
        lint, not a guarantee. This makes it a guarantee: the writer itself
        refuses when nobody is at a terminal, so the only way to author a
        decline is for a person to be there, whatever the caller is called.

        The structural test is kept because it is still worth knowing if a
        runtime module starts reaching for this at all; it is now
        defence-in-depth rather than the whole defence.

        Beside ``promotions.jsonl`` rather than inside it, because the two are
        different facts and a reader that had to look at a ``kind`` field to
        tell them apart is a reader that can get it wrong. Both are appended,
        neither is ever rewritten, and a proposal that was declined and later
        reconsidered gets a *promotion* appended after the decline -- which is
        the honest history, not a mutation of it.
        """

        from research_os.cli import _is_interactive

        if not _is_interactive():
            raise ProposalStoreError(
                "a proposal decline is a human scientific decision and this "
                "process has no interactive terminal. Nothing was written. "
                "Run `researchctl propose decline` yourself."
            )
        line = json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n"
        try:
            with self.declines_file.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ProposalStoreError(
                f"cannot append to {self.declines_file}: {exc}"
            ) from exc
        self.append_event(
            "item_declined",
            item_id=record.item_id,
            reason=record.reason,
            declined_by=record.declined_by,
        )
        return record

    def declines(self) -> list[DeclineRecord]:
        if not self.declines_file.is_file():
            return []
        found: list[DeclineRecord] = []
        for line in self._read(self.declines_file).splitlines():
            if not line.strip():
                continue
            try:
                found.append(DeclineRecord.model_validate_json(line))
            except Exception as exc:
                raise ProposalStoreError(
                    f"corrupt decline record in {self.declines_file}: {exc}"
                ) from exc
        return found

    def decided_item_ids(self) -> set[str]:
        """Every item a human has acted on, promoted or declined.

        The question the runtime's cross-cycle deduplication actually asks. It
        used to ask only about promotions, so a proposal the researcher had
        read and rejected looked identical to one nobody had opened, and the
        runtime would neither re-propose about those findings nor let the
        rejected one go.
        """

        return {record.item_id for record in self.promotions()} | {
            record.item_id for record in self.declines()
        }

    def promotions(self) -> list[PromotionRecord]:
        if not self.promotions_file.is_file():
            return []
        found: list[PromotionRecord] = []
        for line in self._read(self.promotions_file).splitlines():
            if not line.strip():
                continue
            try:
                found.append(PromotionRecord.model_validate_json(line))
            except Exception as exc:
                raise ProposalStoreError(
                    f"corrupt promotion record in {self.promotions_file}: {exc}"
                ) from exc
        return found

    def append_event(self, event: str, **fields: Any) -> dict[str, Any]:
        record: dict[str, Any] = {
            "seq": self.event_count() + 1,
            "ts": utc_now(),
            "proposal_id": self.proposal_id,
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
            raise ProposalStoreError(
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
                raise ProposalStoreError(
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
            raise ProposalStoreError(f"path escapes the proposal directory: {target}")
        return target

    def _read(self, path: Path) -> str:
        try:
            return path.read_text(encoding="utf-8")
        except OSError as exc:
            raise ProposalStoreError(f"cannot read {path}: {exc}") from exc

    def _atomic_write(self, target: Path, text: str) -> None:
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=target.parent, prefix=f".{target.name}.", suffix=".tmp"
            )
        except OSError as exc:
            raise ProposalStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise ProposalStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)


def _document(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
