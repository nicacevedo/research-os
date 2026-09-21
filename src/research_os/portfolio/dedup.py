"""Deduplication in four layers, none of whose identities is a model's output.

The brief's requirement, and the reason for it: *do not use a stochastic model
output as the sole durable identity.* A stochastic identity means the same idea
has a different identity on two runs, which breaks replay, restart and
deduplication simultaneously -- so the first three layers here are arithmetic
and the fourth records a model's opinion as an *edge* with provenance.

```text
1. exact content identity   content_digest equality
                            → this IS that version; no new idea
2. canonical hash           canonical_digest equality
                            → DUPLICATE_OF the existing idea
3. lineage-aware            canonical_digest matches inside the same lineage
                            family → DUPLICATE_OF, and the track ends
4. semantic adjudication    only when 1-3 miss AND the local similarity screen
                            exceeds the threshold → a model is asked
```

Layer 4's *gate* is the interesting part. A model call per candidate pair would
be the obvious design and would cost more than the exploration it protects. The
screen is character-trigram Jaccard over the same normalisation the canonical
digest uses -- pure Python, deterministic, no dependency, no embedding, no
vector store, which is invariant 1 and keeps `ARCHITECTURE.md` §12's postponed
list intact.

**A duplicate becomes a reference, not a disappearance.** Nothing explored is
deleted: the duplicate row is written, marked ``SUPERSEDED`` with a reason, and
given an edge to the survivor, so ``researchctl ideas lineage`` shows the fact
that the system had the idea twice.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum

from research_os.portfolio import digests as pdigests
from research_os.portfolio.config import PortfolioConfig
from research_os.portfolio.store import PortfolioStore


class DedupVerdict(StrEnum):
    """What the deterministic layers concluded, before any model is asked."""

    #: The candidate is byte-for-byte the science of an existing version.
    IDENTICAL = "IDENTICAL"
    #: Different words, same normalised content.
    CANONICAL_DUPLICATE = "CANONICAL_DUPLICATE"
    #: A canonical duplicate of something in its own lineage family, which is
    #: worse than an ordinary duplicate: it means a branch went in a circle.
    LINEAGE_DUPLICATE = "LINEAGE_DUPLICATE"
    #: Close enough that a model should decide. Nothing is concluded here.
    NEEDS_ADJUDICATION = "NEEDS_ADJUDICATION"
    #: Nothing like it.
    DISTINCT = "DISTINCT"


@dataclass(frozen=True, slots=True)
class DedupOutcome:
    verdict: DedupVerdict
    #: The idea this one duplicates, when there is one.
    match_idea_id: str | None = None
    #: The version of the match, for ``IDENTICAL`` only.
    match_version: int | None = None
    #: The local screen's score against the nearest neighbour, for the record.
    similarity: float = 0.0
    #: Every neighbour above the threshold, nearest first. What the adjudicator
    #: is shown when one is asked, bounded so a prompt cannot become a corpus.
    neighbours: tuple[tuple[str, float], ...] = ()
    detail: str = ""

    @property
    def is_duplicate(self) -> bool:
        return self.verdict in {
            DedupVerdict.IDENTICAL,
            DedupVerdict.CANONICAL_DUPLICATE,
            DedupVerdict.LINEAGE_DUPLICATE,
        }


#: The most neighbours shown to the adjudicator.
#:
#: A bound rather than a limit that will be hit. Twelve near-duplicates means
#: the exploration has collapsed, and showing forty of them to a model would
#: make the prompt the largest thing in the request without making the answer
#: better.
MAX_NEIGHBOURS = 6


def screen(
    store: PortfolioStore,
    *,
    project_id: str,
    fields: Mapping[str, object],
    config: PortfolioConfig,
    exclude: str | None = None,
    lineage_family: Sequence[str] = (),
) -> DedupOutcome:
    """Run the three deterministic layers and the local screen.

    Returns without asking any model. ``NEEDS_ADJUDICATION`` is the caller's
    signal to spend one; every other verdict is final and free.
    """

    digest_input = {**dict(fields), "project": project_id}
    content = pdigests.content_digest(digest_input)
    canonical = pdigests.canonical_digest(digest_input)

    exact = store.find_by_content_digest(project_id=project_id, content_digest=content)
    if exact is not None and exact[0] != exclude:
        return DedupOutcome(
            verdict=DedupVerdict.IDENTICAL,
            match_idea_id=exact[0],
            match_version=exact[1],
            similarity=1.0,
            detail="identical scientific content to an existing version",
        )

    match = store.find_by_canonical_digest(
        project_id=project_id, canonical_digest=canonical, exclude=exclude
    )
    if match is not None:
        family = set(lineage_family)
        verdict = (
            DedupVerdict.LINEAGE_DUPLICATE
            if match in family
            else DedupVerdict.CANONICAL_DUPLICATE
        )
        return DedupOutcome(
            verdict=verdict,
            match_idea_id=match,
            similarity=1.0,
            detail=(
                "a different phrasing of an idea already in this lineage"
                if verdict is DedupVerdict.LINEAGE_DUPLICATE
                else "a different phrasing of an idea already in this project"
            ),
        )

    question = pdigests.normalise(str(fields.get("research_question", "")))
    core = pdigests.normalise(str(fields.get("core_idea", "")))
    scored: list[tuple[str, float]] = []
    nearest = 0.0
    nearest_id = ""
    for idea_id, other_question, other_core in store.similarity_corpus(
        project_id=project_id, exclude=exclude
    ):
        score = max(
            pdigests.trigram_similarity(question, pdigests.normalise(other_question)),
            pdigests.trigram_similarity(core, pdigests.normalise(other_core)),
        )
        if score > nearest:
            nearest, nearest_id = score, idea_id
        if score >= config.thresholds.duplicate_similarity:
            scored.append((idea_id, score))
    scored.sort(key=lambda item: (-item[1], item[0]))

    if not scored:
        # The nearest score goes in the detail, and it is the reason this
        # branch is worth a number at all. The first dogfood produced two
        # ideas that are the same research direction in different words --
        # "is the pricing rule a re-derivation of known working-set theory"
        # asked twice, by two explorers -- and this layer said "nothing
        # close", because character-trigram Jaccard over the two phrasings is
        # 0.297 against a threshold of 0.72.
        #
        # Across 46 pairs from two real projects nothing exceeded 0.377, so
        # the semantic adjudicator -- layer 4, the one that exists precisely
        # for differently-worded duplicates -- was never consulted once.
        # `config.py` says 0.72 is "a starting value, not a measurement"; this
        # is the measurement, and choosing the number it implies is a
        # judgement about how aggressively research directions should be
        # merged, which is the researcher's.
        #
        # So the threshold is unchanged and the observation is no longer
        # invisible: a researcher reading `ideas show` sees what "nothing
        # close" actually meant.
        return DedupOutcome(
            verdict=DedupVerdict.DISTINCT,
            similarity=nearest,
            detail=(
                f"nothing close: nearest is {nearest:.2f} "
                f"({nearest_id or 'no other idea'}), below the "
                f"{config.thresholds.duplicate_similarity} threshold at which a "
                f"model is asked"
                if nearest_id
                else "nothing close: this is the first idea in the portfolio"
            ),
        )
    return DedupOutcome(
        verdict=DedupVerdict.NEEDS_ADJUDICATION,
        match_idea_id=scored[0][0],
        similarity=scored[0][1],
        neighbours=tuple(scored[:MAX_NEIGHBOURS]),
        detail=(
            f"{len(scored)} idea(s) above the {config.thresholds.duplicate_similarity} "
            f"similarity threshold; a model decides"
        ),
    )
