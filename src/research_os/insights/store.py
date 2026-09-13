"""Where promoted insights live: one YAML file each, readable and deletable.

Files rather than a database, and the reason is the thing this feature is
riskiest at. An insight is knowledge that will be quoted into other projects'
prompts, so a researcher needs to be able to read the whole set in an editor,
correct one, and delete one they no longer stand behind. A database makes all
three of those harder and buys nothing at this scale: promotion requires a human,
so the corpus is tens of items, not tens of thousands.

Retrieval is deterministic keyword scoring over those files, computed in Python.
There is no index to keep in sync and nothing that can go stale relative to the
files, which is worth more here than the speed an index would buy over a corpus
this size.

Nominations live separately, under runtime state rather than durable data. That
separation is the promotion boundary made structural: an agent writes into one
place, and the other place is only ever written by a human command.
"""

from __future__ import annotations

import hashlib
import os
import re
import tempfile
from pathlib import Path

import yaml

from research_os.errors import InsightNotFoundError, InsightStoreError
from research_os.insights.models import (
    INSIGHT_ID_RE,
    NOMINATION_ID_RE,
    InsightMatch,
    InsightNomination,
    InsightStatus,
    PromotedInsight,
)
from research_os.paths import data_home, state_home

INSIGHTS_DIRNAME = "insights"
NOMINATIONS_DIRNAME = "nominations"

#: How much each field counts toward a match.
#:
#: Stated here rather than buried, because a researcher who asks why one insight
#: outranked another deserves an answer they can read. A title match is the
#: strongest signal that an insight is *about* something; scope and assumptions
#: are present but quiet, since matching on them usually means the query shared
#: a word with the boundary rather than with the finding.
FIELD_WEIGHTS: dict[str, float] = {
    "title": 5.0,
    "statement": 3.0,
    "keywords": 3.0,
    "applicability": 1.5,
    "scope": 1.0,
    "assumptions": 0.5,
    "project": 0.5,
}

_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)

#: Words too common to discriminate between insights.
#:
#: A short, fixed list rather than a computed one: a corpus of tens of items has
#: no reliable statistics to derive stopwords from, and a fixed list is something
#: a reader can inspect.
STOPWORDS: frozenset[str] = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "but",
        "by",
        "can",
        "do",
        "for",
        "from",
        "had",
        "has",
        "have",
        "how",
        "in",
        "is",
        "it",
        "its",
        "may",
        "not",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "what",
        "when",
        "which",
        "will",
        "with",
        "would",
    }
)


def insights_root() -> Path:
    """Return the durable directory holding promoted insights."""

    return data_home() / INSIGHTS_DIRNAME


def nominations_root() -> Path:
    """Return the runtime directory holding agent nominations.

    Under the state home rather than the data home, because a nomination is a
    suggestion an agent made during a run. Deleting it loses nothing: the
    knowledge it points at is still in the project that produced it.
    """

    return state_home() / NOMINATIONS_DIRNAME


def make_insight_id(*, title: str, project_id: str, created_at: str) -> str:
    material = f"insight-v1\n{project_id}\n{title}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"INS-{stamp}-{digest}"


def make_nomination_id(*, title: str, project_id: str, created_at: str) -> str:
    material = f"nomination-v1\n{project_id}\n{title}\n{created_at}"
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = created_at.replace("-", "").replace(":", "")
    return f"NOM-{stamp}-{digest}"


class InsightStore:
    """Read and write the promoted-insight corpus."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else insights_root()

    # -- reading ---------------------------------------------------------

    def path_for(self, insight_id: str) -> Path:
        if INSIGHT_ID_RE.fullmatch(insight_id) is None:
            raise InsightNotFoundError(
                f"{insight_id!r} is not a Research OS insight id"
            )
        return self.root / f"{insight_id}.yaml"

    def load(self, insight_id: str) -> PromotedInsight:
        target = self.path_for(insight_id)
        if not target.is_file():
            raise InsightNotFoundError(f"no insight {insight_id} under {self.root}")
        return self._read(target)

    def all(self) -> list[PromotedInsight]:
        """Return every insight, newest id last, including retired ones.

        Retired and superseded insights are returned rather than filtered.
        Callers that want only live knowledge ask for it; a store that hid the
        retired ones would make the mistake they record repeatable.
        """

        if not self.root.is_dir():
            return []
        found: list[PromotedInsight] = []
        for target in sorted(self.root.glob("INS-*.yaml")):
            found.append(self._read(target))
        return found

    def live(self) -> list[PromotedInsight]:
        return [item for item in self.all() if item.live]

    def _read(self, target: Path) -> PromotedInsight:
        try:
            raw = target.read_text(encoding="utf-8")
        except OSError as exc:
            raise InsightStoreError(f"cannot read {target}: {exc}") from exc
        try:
            data = yaml.safe_load(raw) or {}
        except yaml.YAMLError as exc:
            raise InsightStoreError(f"invalid YAML in {target}: {exc}") from exc
        try:
            insight = PromotedInsight.model_validate(data)
        except Exception as exc:
            raise InsightStoreError(f"invalid insight in {target}: {exc}") from exc
        if insight.insight_id != target.stem:
            raise InsightStoreError(
                f"{target} holds insight {insight.insight_id}; a file is named "
                "after the insight it contains"
            )
        return insight

    # -- writing ---------------------------------------------------------

    def write(self, insight: PromotedInsight, *, overwrite: bool = False) -> Path:
        """Write one insight atomically, refusing to overwrite unless asked."""

        target = self.path_for(insight.insight_id)
        if target.exists() and not overwrite:
            raise InsightStoreError(
                f"refusing to overwrite {target}; an insight is written once and "
                "changed deliberately"
            )
        document = _dump(insight)
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            handle_fd, tmp_name = tempfile.mkstemp(
                dir=self.root, prefix=f".{insight.insight_id}.", suffix=".tmp"
            )
        except OSError as exc:
            raise InsightStoreError(f"cannot write {target}: {exc}") from exc
        tmp = Path(tmp_name)
        try:
            with os.fdopen(handle_fd, "w", encoding="utf-8") as handle:
                handle.write(document)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(tmp, target)
        except OSError as exc:
            raise InsightStoreError(f"cannot write {target}: {exc}") from exc
        finally:
            tmp.unlink(missing_ok=True)
        return target

    def retire(self, insight_id: str, *, reason: str) -> PromotedInsight:
        """Mark one insight retired, keeping it readable.

        Retired rather than deleted, because an insight that turned out to be
        wrong is the one a future reader most needs to find.
        """

        from research_os.automation.models import utc_now

        insight = self.load(insight_id)
        updated = insight.model_copy(
            update={
                "status": InsightStatus.RETIRED,
                "retire_reason": reason,
                "updated_at": utc_now(),
            }
        )
        self.write(updated, overwrite=True)
        return updated

    def supersede(self, insight_id: str, *, successor_id: str) -> PromotedInsight:
        """Point one insight at the insight that replaced it."""

        from research_os.automation.models import utc_now

        successor = self.load(successor_id)
        insight = self.load(insight_id)
        updated = insight.model_copy(
            update={
                "status": InsightStatus.SUPERSEDED,
                "superseded_by": successor.insight_id,
                "updated_at": utc_now(),
            }
        )
        self.write(updated, overwrite=True)
        return updated

    # -- retrieval -------------------------------------------------------

    def search(
        self,
        query: str,
        *,
        limit: int = 10,
        include_retired: bool = False,
        exclude_project: str | None = None,
    ) -> list[InsightMatch]:
        """Return insights matching ``query``, best first, deterministically.

        ``exclude_project`` drops insights that came from the project doing the
        asking. A project reading back its own conclusions through the
        cross-project channel would be laundering them: they would arrive
        stripped of their capsule context and labelled as knowledge from
        somewhere else.
        """

        terms = [
            token
            for token in (item.casefold() for item in _WORD_RE.findall(query or ""))
            if token not in STOPWORDS
        ]
        if not terms:
            return []
        matches: list[InsightMatch] = []
        for insight in self.all():
            if not include_retired and not insight.live:
                continue
            if exclude_project and insight.source.project_id == exclude_project:
                continue
            score, matched = _score(insight, terms)
            if score > 0:
                matches.append(
                    InsightMatch(
                        insight=insight, score=round(score, 4), matched_terms=matched
                    )
                )
        matches.sort(key=lambda item: (-item.score, item.insight.insight_id))
        return matches[: max(1, limit)]


class NominationStore:
    """Read and write the agent nominations awaiting a human decision."""

    def __init__(self, root: Path | None = None) -> None:
        self.root = root if root is not None else nominations_root()

    def path_for(self, nomination_id: str) -> Path:
        if NOMINATION_ID_RE.fullmatch(nomination_id) is None:
            raise InsightNotFoundError(
                f"{nomination_id!r} is not a Research OS nomination id"
            )
        return self.root / f"{nomination_id}.yaml"

    def load(self, nomination_id: str) -> InsightNomination:
        target = self.path_for(nomination_id)
        if not target.is_file():
            raise InsightNotFoundError(
                f"no nomination {nomination_id} under {self.root}"
            )
        try:
            data = yaml.safe_load(target.read_text(encoding="utf-8")) or {}
        except (OSError, yaml.YAMLError) as exc:
            raise InsightStoreError(f"cannot read {target}: {exc}") from exc
        try:
            return InsightNomination.model_validate(data)
        except Exception as exc:
            raise InsightStoreError(f"invalid nomination in {target}: {exc}") from exc

    def all(self) -> list[InsightNomination]:
        if not self.root.is_dir():
            return []
        return [
            self.load(target.stem) for target in sorted(self.root.glob("NOM-*.yaml"))
        ]

    def pending(self) -> list[InsightNomination]:
        return [item for item in self.all() if item.pending]

    def write(self, nomination: InsightNomination) -> Path:
        target = self.path_for(nomination.nomination_id)
        document = yaml.safe_dump(
            nomination.model_dump(mode="json"),
            sort_keys=False,
            allow_unicode=True,
            default_flow_style=False,
            width=88,
        )
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            target.write_text(document, encoding="utf-8")
        except OSError as exc:
            raise InsightStoreError(f"cannot write {target}: {exc}") from exc
        return target


def _score(insight: PromotedInsight, terms: list[str]) -> tuple[float, list[str]]:
    """Return one insight's score for a query, and which terms matched.

    Term frequency by field with the stated weights, and nothing cleverer. The
    corpus is small by construction -- promotion requires a human -- so the
    ranking that matters is "did this insight talk about what I asked", which
    counting answers directly and explainably.
    """

    fields = {
        "title": insight.title,
        "statement": insight.statement,
        "keywords": " ".join(insight.keywords),
        "applicability": insight.applicability,
        "scope": insight.scope,
        "assumptions": " ".join(insight.assumptions),
        "project": insight.source.project_id.replace("-", " "),
    }
    tokenised = {
        name: [item.casefold() for item in _WORD_RE.findall(text)]
        for name, text in fields.items()
    }
    total = 0.0
    matched: set[str] = set()
    for term in terms:
        for name, tokens in tokenised.items():
            occurrences = tokens.count(term)
            if occurrences:
                total += FIELD_WEIGHTS[name] * occurrences
                matched.add(term)
    return total, sorted(matched)


def _dump(insight: PromotedInsight) -> str:
    """Return the YAML a human reads, in the order they need to read it.

    Title and statement first, then -- before anything else -- where it holds
    and what it assumed. The ordering is part of the safety property: a reader
    skimming the file meets the boundary before they meet the conclusion.
    """

    payload = insight.model_dump(mode="json")
    order = [
        "schema_version",
        "insight_id",
        "title",
        "statement",
        "scope",
        "assumptions",
        "applicability",
        "promotion_type",
        "confidence",
        "status",
        "source",
        "promoted_by",
        "created_at",
        "updated_at",
        "superseded_by",
        "retire_reason",
        "keywords",
        "nomination_id",
    ]
    ordered = {key: payload[key] for key in order if key in payload}
    ordered.update({key: value for key, value in payload.items() if key not in ordered})
    dumped = yaml.safe_dump(
        ordered,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
        width=88,
    )
    return dumped if dumped.endswith("\n") else dumped + "\n"
