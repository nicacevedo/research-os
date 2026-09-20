"""The three digests an idea has, and why each is computed the way it is.

**``content_digest``** — over the scientifically material fields of one idea
version, and nothing else. It is what a review binds to, so the set of fields
it covers *is* the definition of "a material revision". A change that moves it
stales every review; a change that does not, does not. Administrative fields --
status, the next action, the quality dimensions, spend, timestamps -- are
excluded for exactly the reason the kernel excludes them from a capsule
object's subject digest: a lifecycle change must not invalidate a review of
unchanged science.

**``canonical_digest``** — over an aggressively normalised projection of the
question and the core idea. Two phrasings of one idea collide here on purpose.
This is the deterministic half of deduplication, and it is deterministic
precisely so that restart, database reconnect, event replay and model
nondeterminism cannot change an idea's identity. A model's opinion about
sameness is recorded as an edge; it is never an identity.

**``basis_digest``** — over the scientific basis a stage would run against:
the content digest, the evidence set and the live review set. Two requests to
run one stage against one basis are one action, which is what makes a replayed
event, a reclaimed lease and a duplicated portfolio tick all harmless.

All three are version-tagged. A changed digest must distinguish changed science
from a changed algorithm, which is the same requirement
:mod:`research_os.digests` states for capsule objects.
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

#: Bumped when the *projection* changes -- a field added to or removed from
#: what is hashed. Never bumped for a code refactor that produces identical
#: bytes.
CONTENT_DIGEST_VERSION = "pidea-content-v1"
CANONICAL_DIGEST_VERSION = "pidea-canonical-v1"
BASIS_DIGEST_VERSION = "pidea-basis-v1"
OBJECTION_KEY_VERSION = "pidea-objection-v1"
EVIDENCE_SET_DIGEST_VERSION = "pidea-evidence-set-v1"

#: The fields of an idea version that are scientifically material, in the order
#: they appear in the specification. Declared as data rather than written into
#: the function, so a test can assert that every column of ``idea_versions``
#: is either in this tuple or deliberately excluded by the one below.
MATERIAL_FIELDS: tuple[str, ...] = (
    "title",
    "research_question",
    "core_idea",
    "mechanism",
    "why_it_matters",
    "falsifier",
    "adjudication_types",
    "closest_prior_work",
    "claimed_difference",
    "assumptions",
    "alternative_explanations",
    "open_uncertainties",
)

#: Deliberately excluded, with the reason each is excluded, because "we forgot"
#: and "we decided" look identical in a field list.
IMMATERIAL_FIELDS: dict[str, str] = {
    "next_best_action": "an operational plan, not a claim about the world",
    "dimensions": "scores assigned to the idea, not part of the idea",
    "addressed_objections": "a pointer to prior objections; changing it "
    "changes no scientific content",
    "content_digest": "the digest itself",
    "canonical_digest": "the digest itself",
    "origin_call_id": "provenance",
    "origin_role": "provenance",
    "origin_stage": "provenance",
    "created_at": "provenance",
    "idea_id": "identity, constant across versions",
    "version": "identity",
}

#: Keys the projections require that are not columns of ``idea_versions``.
#: ``project`` is one: it is the scope of the identity rather than part of the
#: idea, and the store supplies it from the ``ideas`` row.
PROJECTION_CONTEXT: tuple[str, ...] = ("project",)

#: Stopwords dropped by the canonical projection. Short and fixed: the purpose
#: is to make "a method for X" and "an approach to X" collide, not to do
#: linguistics. Extending it changes every canonical digest, which is why the
#: digest carries a version.
#:
#: **What must never go in here**, and the rule is absolute: no word whose
#: removal can flip a meaning. Negation (``no``, ``not``, ``never``,
#: ``without``), comparison (``same``, ``different``, ``more``, ``less``),
#: quantification (``all``, ``some``, ``any``, ``every``, ``each``) and
#: conditionality (``if``, ``only``, ``unless``) all stay, because "the
#: trajectories are the same" and "the trajectories are not the same" must not
#: normalise to one idea. ``if`` and ``than`` were in the first version of this
#: list and were removed for exactly that reason.
#:
#: What is in here is articles, copulas, auxiliaries, prepositions, pronouns,
#: discourse markers, and the handful of contentless research nouns that appear
#: in the title of everything ever written.
_STOPWORDS: frozenset[str] = frozenset(
    [
        "a",
        "about",
        "also",
        "among",
        "an",
        "analysis",
        "and",
        "approach",
        "are",
        "as",
        "at",
        "based",
        "be",
        "been",
        "being",
        "between",
        "but",
        "by",
        "can",
        "could",
        "did",
        "do",
        "does",
        "for",
        "framework",
        "from",
        "he",
        "hence",
        "her",
        "here",
        "hers",
        "him",
        "his",
        "however",
        "in",
        "into",
        "is",
        "it",
        "its",
        "itself",
        "may",
        "method",
        "might",
        "moreover",
        "must",
        "new",
        "novel",
        "of",
        "on",
        "or",
        "our",
        "over",
        "shall",
        "she",
        "should",
        "study",
        "technique",
        "that",
        "the",
        "their",
        "theirs",
        "them",
        "themselves",
        "then",
        "there",
        "these",
        "they",
        "this",
        "those",
        "thus",
        "to",
        "toward",
        "towards",
        "under",
        "us",
        "use",
        "uses",
        "using",
        "via",
        "was",
        "we",
        "were",
        "will",
        "with",
        "would",
        "you",
        "your",
    ]
)

_NON_TOKEN = re.compile(r"[^a-z0-9]+")


def _hash(version: str, payload: Any) -> str:
    """Canonical JSON, SHA-256, version-tagged. One construction, used thrice."""

    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return f"{version}:{hashlib.sha256(encoded).hexdigest()}"


def _text(value: object) -> str:
    return "" if value is None else str(value)


def _string_list(value: object) -> list[str]:
    """Canonicalise a reference or statement collection.

    ``None`` and ``[]`` both mean "none" and project to ``[]``, matching
    :mod:`research_os.digests`. Order is preserved for authored prose lists --
    the order an explorer put its alternatives in is content -- and is *not*
    sorted, because sorting would make a reordering invisible to a reviewer who
    read them in order.
    """

    if value is None:
        return []
    if isinstance(value, str):
        return [value]
    return [_text(item) for item in value]


def content_projection(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The fixed-key material projection of one idea version.

    ``project`` is a key of the projection and is required. Reviewed identity
    is project-local: an idea copied into another project must not arrive
    carrying the first project's reviews, which is exactly the rule
    :func:`research_os.digests.semantic_projection` states for capsule objects.
    """

    return {
        "project": _text(fields.get("project")),
        "title": _text(fields.get("title")),
        "research_question": _text(fields.get("research_question")),
        "core_idea": _text(fields.get("core_idea")),
        "mechanism": _text(fields.get("mechanism")),
        "why_it_matters": _text(fields.get("why_it_matters")),
        "falsifier": _text(fields.get("falsifier")),
        # Sorted: an adjudication type set is a set, and reordering it is not a
        # scientific change.
        "adjudication_types": sorted(
            set(_string_list(fields.get("adjudication_types")))
        ),
        "closest_prior_work": _text(fields.get("closest_prior_work")),
        "claimed_difference": _text(fields.get("claimed_difference")),
        "assumptions": _string_list(fields.get("assumptions")),
        "alternative_explanations": _string_list(
            fields.get("alternative_explanations")
        ),
        "open_uncertainties": _string_list(fields.get("open_uncertainties")),
    }


def content_digest(fields: Mapping[str, Any]) -> str:
    """The digest a review binds to."""

    return _hash(CONTENT_DIGEST_VERSION, content_projection(fields))


def normalise(text: str) -> tuple[str, ...]:
    """Reduce prose to the sorted token multiset the canonical digest hashes.

    NFKC, casefold, drop everything outside ``[a-z0-9]``, drop stopwords, sort.
    Deliberately lossy in exactly the ways that make rephrasings collide:
    word order, articles, voice and punctuation all disappear, and the
    remaining content words do not.

    Returned as a tuple rather than joined, so the trigram similarity screen
    and the digest share one normalisation and cannot drift apart.
    """

    folded = unicodedata.normalize("NFKC", text).casefold()
    tokens = [token for token in _NON_TOKEN.split(folded) if token]
    return tuple(sorted(token for token in tokens if token not in _STOPWORDS))


def canonical_projection(fields: Mapping[str, Any]) -> dict[str, Any]:
    """The normalised projection two rephrasings of one idea share."""

    return {
        "project": _text(fields.get("project")),
        "question": list(normalise(_text(fields.get("research_question")))),
        "core": list(normalise(_text(fields.get("core_idea")))),
    }


def canonical_digest(fields: Mapping[str, Any]) -> str:
    """The deterministic deduplication key. Never a model's output."""

    return _hash(CANONICAL_DIGEST_VERSION, canonical_projection(fields))


def basis_digest(
    *,
    content: str,
    evidence_ids: Iterable[str],
    review_ids: Iterable[str],
    stage_inputs: Mapping[str, Any] | None = None,
) -> str:
    """The digest that makes a stage's execution idempotent.

    Sorted sets, because "the evidence this rests on" is a set and the order it
    was written in is not part of the basis.
    """

    return _hash(
        BASIS_DIGEST_VERSION,
        {
            "content": content,
            "evidence": sorted(set(evidence_ids)),
            "reviews": sorted(set(review_ids)),
            "inputs": dict(stage_inputs or {}),
        },
    )


def evidence_set_digest(evidence_ids: Iterable[str]) -> str:
    """The digest a review binds to alongside the content digest.

    Over the *set* of evidence rows the reviewed version linked at review time.
    Without it, the evidence under a standing approval can be replaced wholesale
    and the approval still reads as current -- which is exactly the hole
    ``docs/CAPSULE.md`` closes for a Claim with ``evidence_digests``, and which
    the first draft of this layer reopened.

    The empty set has a digest of its own rather than being ``None``: "this
    review read no evidence" is a fact a gate must be able to check, and a null
    would make it indistinguishable from "we did not record what it read".
    """

    return _hash(EVIDENCE_SET_DIGEST_VERSION, sorted(set(evidence_ids)))


def objection_key(summary: str) -> str:
    """The stickiness key for one objection.

    Over the normalised text, so the same objection raised by a second reviewer
    -- or raised again after a revision that claimed to answer it -- is
    recognisably the same objection rather than a new one.
    """

    return _hash(OBJECTION_KEY_VERSION, list(normalise(summary)))


def trigram_similarity(left: Sequence[str], right: Sequence[str]) -> float:
    """Jaccard similarity over character trigrams of two normalised token runs.

    The gate on whether a model is asked about a possible duplicate at all.
    Pure Python, deterministic, no dependency, no embedding, no vector store --
    which is what invariant 1 ("local computation before LLM reasoning") asks
    for, and what keeps the postponed-technology list in ``ARCHITECTURE.md``
    §12 intact.

    Returns 0.0 when either side is empty, because "nothing is similar to
    nothing" is the answer that does not cause a model call.
    """

    left_grams = _trigrams(" ".join(left))
    right_grams = _trigrams(" ".join(right))
    if not left_grams or not right_grams:
        return 0.0
    intersection = len(left_grams & right_grams)
    union = len(left_grams | right_grams)
    return intersection / union if union else 0.0


def _trigrams(text: str) -> frozenset[str]:
    if len(text) < 3:
        return frozenset({text} if text else ())
    return frozenset(text[index : index + 3] for index in range(len(text) - 2))
