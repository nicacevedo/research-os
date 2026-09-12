"""Deciding when two provider records describe the same piece of scholarship.

Three providers asked about one paper return three records. They disagree about
capitalisation, about whether the DOI carries a ``https://doi.org/`` prefix,
about whether the arXiv id has a version suffix, and about how an author's
initials are punctuated. None of that is a scientific judgement, so none of it
belongs in a model call: it is string normalisation, and it is done here, once,
deterministically.

The rule is a precedence, not a similarity score. A DOI is a registered
identifier for a specific work, so two records carrying the same DOI are the
same work and nothing else is consulted. Below that comes the arXiv id, then the
OpenAlex id. Only when a record carries none of those does a fallback on
normalised title, first author surname, and year decide -- and that fallback is
deliberately strict rather than fuzzy, because a false merge silently destroys a
distinct work, which is worse than a duplicate a human can see and collapse.

Nothing here is heuristic about *content*. There is no edit distance, no
tokenised title similarity, no embedding. Two titles either normalise to the
same string or they do not.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata

#: Identifier schemes, strongest first. The first one a record carries decides
#: its key, so this tuple is the whole precedence rule.
IDENTIFIER_PRECEDENCE: tuple[str, ...] = ("doi", "arxiv", "openalex", "pmid")

#: The scheme used when a record carries no registered identifier at all.
TITLE_SCHEME = "title"

_DOI_PREFIXES = (
    "https://doi.org/",
    "http://doi.org/",
    "https://dx.doi.org/",
    "http://dx.doi.org/",
    "doi:",
)

_ARXIV_PREFIXES = (
    "https://arxiv.org/abs/",
    "http://arxiv.org/abs/",
    "https://arxiv.org/pdf/",
    "http://arxiv.org/pdf/",
    "arxiv:",
)

_OPENALEX_PREFIXES = (
    "https://openalex.org/",
    "http://openalex.org/",
    "https://api.openalex.org/works/",
)

_ARXIV_VERSION_RE = re.compile(r"v[0-9]+$")
_NEW_ARXIV_RE = re.compile(r"^[0-9]{4}\.[0-9]{4,5}$")
_OLD_ARXIV_RE = re.compile(r"^[a-z-]+(\.[a-z]{2})?/[0-9]{7}$")
_OPENALEX_RE = re.compile(r"^w[0-9]+$")
_PMID_RE = re.compile(r"^[0-9]{1,9}$")
#: One word of a normalised title or name.
#:
#: ``[^\W_]`` rather than ``[a-z0-9]`` on purpose. Scholarship is not written
#: only in ASCII, and a pattern that matched Latin letters alone would reduce a
#: Chinese or Cyrillic title to the handful of Latin words it happens to contain
#: -- which would make two unrelated papers with the same author and year
#: normalise to the same fallback identity and be merged into one.
_WORD_RE = re.compile(r"[^\W_]+", re.UNICODE)


def normalize_doi(value: str | None) -> str | None:
    """Return a DOI as the bare, lowercased registered string, or ``None``.

    DOIs are case-insensitive by specification, so lowercasing is a
    normalisation rather than a guess. A value that does not have the ``10.``
    registrant shape is refused rather than stored: a field that sometimes holds
    a DOI and sometimes holds whatever a provider put there is not an
    identifier.
    """

    if not value:
        return None
    text = value.strip().lower()
    for prefix in _DOI_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.strip().rstrip(".")
    if not text.startswith("10.") or "/" not in text:
        return None
    if any(character.isspace() for character in text):
        return None
    return text


def normalize_arxiv_id(value: str | None) -> str | None:
    """Return an arXiv id without scheme, prefix, or version suffix.

    The version is dropped on purpose. ``2301.00001v1`` and ``2301.00001v3`` are
    the same work at two moments, and treating them as different works would
    duplicate every preprint that was ever revised. Which version was actually
    retrieved is recorded on the file and the source record, where it describes
    an artifact rather than an identity.
    """

    if not value:
        return None
    text = value.strip().lower()
    for prefix in _ARXIV_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = _ARXIV_VERSION_RE.sub("", text.removesuffix(".pdf").strip())
    if _NEW_ARXIV_RE.fullmatch(text) or _OLD_ARXIV_RE.fullmatch(text):
        return text
    return None


def normalize_openalex_id(value: str | None) -> str | None:
    """Return an OpenAlex work id as its bare ``W...`` form, uppercased."""

    if not value:
        return None
    text = value.strip().lower()
    for prefix in _OPENALEX_PREFIXES:
        if text.startswith(prefix):
            text = text[len(prefix) :]
            break
    text = text.split("?")[0].strip()
    if _OPENALEX_RE.fullmatch(text):
        return text.upper()
    return None


def normalize_pmid(value: str | None) -> str | None:
    """Return a PubMed id as bare digits, or ``None``."""

    if not value:
        return None
    text = value.strip().lower().removeprefix("pmid:").strip()
    return text if _PMID_RE.fullmatch(text) else None


def normalize_title(value: str | None) -> str:
    """Return the comparison form of a title: lowercase alphanumeric words.

    Punctuation, accents, markup-ish whitespace, and case all vary between
    providers for the same paper, and none of them distinguish two papers. What
    is left is the sequence of words, joined by single spaces, which either
    matches or does not.
    """

    if not value:
        return ""
    return " ".join(_WORD_RE.findall(_folded(value)))


def normalize_author(value: str | None) -> str:
    """Return the comparison form of one author name.

    Reduced to lowercase words, then to the surname alone when the name is
    given as "Last, First" or "First Last". Initials are dropped rather than
    compared: providers disagree about whether they are present, punctuated, or
    expanded, and a surname plus an exact title plus a year is already a strict
    fallback.
    """

    if not value:
        return ""
    text = _folded(value)
    if "," in text:
        text = text.split(",", 1)[0]
    words = _WORD_RE.findall(text)
    if not words:
        return ""
    return words[-1] if len(words) > 1 else words[0]


def title_fallback_key(
    *,
    title: str | None,
    first_author: str | None,
    year: int | None,
) -> str | None:
    """Return the fallback identity of a record with no registered identifier.

    Deliberately requires all three components. A record with a title and
    nothing else is not enough to assert identity against another record with
    the same title: same-titled papers by different groups in different years
    are ordinary, and merging them would destroy both.
    """

    normalized = normalize_title(title)
    surname = normalize_author(first_author)
    if not normalized or not surname or year is None:
        return None
    material = f"work-identity-v1\n{normalized}\n{surname}\n{year}"
    return hashlib.sha256(material.encode("utf-8")).hexdigest()[:32]


def _folded(value: str) -> str:
    """Return ``value`` decomposed, stripped of accents, and lowercased.

    The accents have to go, and they have to go *before* the words are split.
    NFKD decomposition turns ``é`` into ``e`` followed by a combining acute, and
    a word pattern that matches only letters and digits then treats that mark as
    a boundary -- so "Réponse" would compare as two words, and the same title
    from an accent-preserving provider and an accent-stripping one would look
    like two different papers. Dropping the combining marks is what makes them
    one.
    """

    decomposed = unicodedata.normalize("NFKD", value)
    without_marks = "".join(
        character for character in decomposed if not unicodedata.combining(character)
    )
    return without_marks.casefold()


def work_key(scheme: str, value: str) -> str:
    """Return the storage key for one identity.

    A single flat string rather than a composite, so it can be a primary key, a
    foreign key, a CLI argument, and a line in a provenance record without
    needing to be parsed differently in each place.
    """

    return f"{scheme}:{value}"


def split_work_key(key: str) -> tuple[str, str]:
    """Return the scheme and value of a storage key."""

    scheme, _, value = key.partition(":")
    return scheme, value
