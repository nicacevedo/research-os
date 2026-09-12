"""The contract every literature provider adapter implements.

Two methods, mirroring the automation provider contract on purpose: ``probe``
says what this machine can actually do with this provider, and ``search`` /
``fetch`` do one bounded thing. A researcher who has read one probe report can
read the other.

An adapter's whole job is translation. It turns a provider's JSON or Atom into
the neutral :class:`ProviderRecord` shape the store ingests, and it keeps the
raw payload so the translation is auditable. It decides nothing about identity,
nothing about merging, and nothing about ranking -- those are in
:mod:`~research_os.literature.identity`, :mod:`~research_os.literature.store`,
and :mod:`~research_os.literature.search`, once, for every provider.

An adapter never raises for an unusable provider. ``UNAVAILABLE`` is a value,
because "we could not ask OpenAlex today" is a fact about a literature review
that has to survive into the record rather than aborting the run.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from research_os.literature.models import AuthorRecord, SourceProbe, SourceStatus


@dataclass(frozen=True, slots=True)
class ProviderRecord:
    """One work as one provider described it, in neutral form.

    ``payload`` is what the provider actually returned, kept so a merged field
    can be traced back to the sentence that produced it. ``fields``,
    ``identifiers``, and ``authors`` are this adapter's reading of that payload.
    Both are stored: the reading is what the index uses, and the payload is what
    settles an argument about the reading.
    """

    provider: str
    payload: dict[str, Any]
    fields: dict[str, Any] = field(default_factory=dict)
    identifiers: dict[str, str] = field(default_factory=dict)
    authors: list[AuthorRecord] = field(default_factory=list)
    provider_work_id: str | None = None
    request_url: str = ""
    pdf_url: str | None = None
    referenced_ids: tuple[str, ...] = ()
    """Identifiers this work cites, as the provider gave them.

    Left as provider strings rather than resolved keys: resolving them would
    mean creating rows for works nobody asked about. The store maps them when
    the cited work is actually known.
    """


@dataclass(frozen=True, slots=True)
class SourceResult:
    """What one adapter call established.

    ``status`` is the honest answer even when ``records`` is empty: a query that
    legitimately matched nothing and a provider that could not be reached are
    different facts, and a literature review that conflates them is not
    reproducible.
    """

    provider: str
    status: SourceStatus
    records: tuple[ProviderRecord, ...] = ()
    request_url: str = ""
    detail: str = ""
    rate_limit_note: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SourceStatus.OK


class SourceAdapter(Protocol):
    """A literature provider Research OS knows how to talk to."""

    name: str
    base_url: str

    def probe(self) -> SourceProbe: ...

    def search(self, query: str, *, limit: int = 20) -> SourceResult: ...

    def fetch(self, identifier: str) -> SourceResult: ...


def author_records(
    names: list[str], orcids: list[str | None] | None = None
) -> list[AuthorRecord]:
    """Return author rows in the order the provider listed them.

    Order is meaningful -- first authorship is a scientific fact about a paper --
    so it is preserved exactly and never sorted.
    """

    from research_os.literature.identity import normalize_author

    found: list[AuthorRecord] = []
    for position, name in enumerate(names):
        cleaned = " ".join(str(name).split())
        if not cleaned:
            continue
        orcid = orcids[position] if orcids and position < len(orcids) else None
        found.append(
            AuthorRecord(
                position=len(found),
                name=cleaned,
                normalized_name=normalize_author(cleaned),
                orcid=orcid,
            )
        )
    return found


def clean_text(value: Any, *, limit: int = 20_000) -> str | None:
    """Return one provider string as plain bounded text, or ``None``.

    Providers put newlines, tabs, and stray whitespace inside titles and
    abstracts. That is noise in an identifier-bearing field and a nuisance in a
    displayed one, so it is collapsed here rather than at each of the places
    that read it. The value stays untrusted; it is only tidier.
    """

    if value is None:
        return None
    text = " ".join(str(value).split())
    if not text:
        return None
    return text[:limit]


def first_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None
