"""Runtime models for the literature subsystem.

None of these is a scientific object. A Work is a record about a paper that
exists in the world; a Claim is a statement this project asserts and a human
accepted. They live in different stores for that reason: capsule objects are
Git-tracked files under a project's ``.research/``, and everything here lives in
a shared SQLite database under the Research OS data home, which a researcher can
delete without losing any science.

Everything a provider returned is **untrusted data**. A title, an abstract, a
venue name, and the full text of a PDF are all strings someone else wrote, and
they reach a model only through the literature packet, fenced and labelled. The
models here carry that content and its provenance; they never carry authority.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Self

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_validator,
    model_validator,
)

from research_os.automation.models import (
    FINDING_ID_RE,
    Confidence,
    Importance,
    utc_now,
)
from research_os.models import NonBlankStr


class SourceStatus(StrEnum):
    """What a source adapter was actually able to do.

    ``UNAVAILABLE`` is a first-class answer rather than an exception: a machine
    with no network, or without the credential a provider needs, should be able
    to run everything else and be told plainly which provider was skipped.
    """

    OK = "OK"
    UNAVAILABLE = "UNAVAILABLE"
    RATE_LIMITED = "RATE_LIMITED"
    FAILED = "FAILED"


class ExtractionStatus(StrEnum):
    """Whether the local text of a stored file could be established."""

    PENDING = "pending"
    EXTRACTED = "extracted"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"


class SourceProbe(BaseModel):
    """What local inspection established about one literature provider.

    The shape mirrors the automation :class:`ProviderProbe` on purpose: the
    question "can this machine actually use this thing, and how do I know" is
    the same question, and a reader who has learned to read one report should
    not have to learn a second format.
    """

    model_config = ConfigDict(extra="forbid")

    name: NonBlankStr
    base_url: str
    status: SourceStatus
    requires_credential: bool = False
    credential_env: str | None = None
    credential_present: bool = False
    contact_configured: bool = False
    rate_limit_note: str = ""
    detail: str = ""

    @property
    def usable(self) -> bool:
        return self.status is SourceStatus.OK


class AuthorRecord(BaseModel):
    """One author of one work, in the order the provider listed them."""

    model_config = ConfigDict(extra="forbid")

    position: int = Field(ge=0)
    name: str
    normalized_name: str = ""
    orcid: str | None = None


class WorkIdentifier(BaseModel):
    """One registered identifier, and which provider said so."""

    model_config = ConfigDict(extra="forbid")

    scheme: NonBlankStr
    value: NonBlankStr
    provider: NonBlankStr


class FieldProvenance(BaseModel):
    """Which provider supplied one field of one work, and when.

    Kept per field rather than per record because merging is per field: when
    Crossref has the DOI and the venue and arXiv has the abstract and the PDF,
    the merged work is not "from Crossref" or "from arXiv", and a reader asking
    where the abstract came from deserves an answer rather than a guess.

    Never discarded. A later provider that overwrites a field appends a row; it
    does not replace the earlier one.
    """

    model_config = ConfigDict(extra="forbid")

    field: NonBlankStr
    provider: NonBlankStr
    value_digest: str
    retrieved_at: str
    superseded: bool = False


class WorkRecord(BaseModel):
    """One scholarly work as Research OS holds it.

    ``key`` is the deterministic identity from :mod:`.identity`; everything else
    is content some provider asserted. The distinction matters when reading the
    fields: the key is a fact about how this row is addressed, and the title is
    a claim by whoever indexed the paper.
    """

    model_config = ConfigDict(extra="forbid")

    key: NonBlankStr
    title: str = ""
    normalized_title: str = ""
    publication_year: int | None = None
    venue: str | None = None
    work_type: str | None = None
    abstract: str | None = None
    doi: str | None = None
    arxiv_id: str | None = None
    openalex_id: str | None = None
    pmid: str | None = None
    open_access_url: str | None = None
    cited_by_count: int | None = None
    is_retracted: bool = False
    retraction_note: str | None = None
    first_seen: str = Field(default_factory=utc_now)
    last_updated: str = Field(default_factory=utc_now)
    authors: list[AuthorRecord] = Field(default_factory=list)
    identifiers: list[WorkIdentifier] = Field(default_factory=list)
    provenance: list[FieldProvenance] = Field(default_factory=list)

    @property
    def first_author(self) -> str | None:
        return self.authors[0].name if self.authors else None

    def citation(self) -> str:
        """Return a short human citation. Display only; never a stored digest."""

        author = self.first_author or "Unknown"
        year = self.publication_year or "n.d."
        pointer = self.doi or self.arxiv_id or self.openalex_id or self.key
        return f"{author} ({year}). {self.title or 'Untitled'}. {pointer}"


class SourceRecord(BaseModel):
    """One provider response about one work, kept verbatim.

    The payload is the evidence behind every merged field. It is stored as the
    provider returned it, digested, and never edited: a merge that looks wrong
    later has to be arguable from what the provider actually said.
    """

    model_config = ConfigDict(extra="forbid")

    record_id: int | None = None
    work_key: NonBlankStr
    provider: NonBlankStr
    provider_work_id: str | None = None
    request_url: str = ""
    retrieved_at: str
    payload_sha256: str
    payload: str


class SearchRecord(BaseModel):
    """One provider search, recorded whether or not it returned anything.

    A search that failed is as much a part of the record as one that
    succeeded: "we looked and the provider was down" and "we never looked" are
    different states of knowledge, and a literature review that cannot tell them
    apart is not reproducible.
    """

    model_config = ConfigDict(extra="forbid")

    search_id: int | None = None
    query: NonBlankStr
    provider: NonBlankStr
    requested_at: str
    parameters: str = "{}"
    cache_key: str = ""
    """What identifies this request for cache purposes, without the response URL.

    Separate from ``parameters`` on purpose. ``parameters`` records what
    happened, including the URL the provider was actually asked -- valuable
    provenance, and useless as a cache key, because a caller deciding whether to
    make a request does not yet know what URL the adapter will build. This is
    the part that is knowable beforehand.
    """

    status: SourceStatus = SourceStatus.OK
    result_count: int = 0
    detail: str = ""


class SearchHit(BaseModel):
    """One work a provider returned for one search, at the rank it gave it."""

    model_config = ConfigDict(extra="forbid")

    search_id: int
    rank: int = Field(ge=1)
    work_key: NonBlankStr
    provider_score: float | None = None


class FileRecord(BaseModel):
    """One retrieved file, addressed by the digest of its content."""

    model_config = ConfigDict(extra="forbid")

    file_sha256: str
    work_key: NonBlankStr
    provider: NonBlankStr
    source_url: str
    media_type: str
    byte_size: int = Field(ge=0)
    retrieved_at: str
    stored_path: str
    extraction_status: ExtractionStatus = ExtractionStatus.PENDING
    extraction_detail: str = ""

    @field_validator("file_sha256")
    @classmethod
    def _digest_shape(cls, value: str) -> str:
        if len(value) != 64 or any(item not in "0123456789abcdef" for item in value):
            raise ValueError("file_sha256 must be 64 lowercase hexadecimal characters")
        return value


class TextDocument(BaseModel):
    """The local text extracted from one stored file."""

    model_config = ConfigDict(extra="forbid")

    document_id: int | None = None
    work_key: NonBlankStr
    file_sha256: str
    kind: str = "fulltext"
    extracted_at: str
    char_count: int = Field(ge=0)
    extractor: str = ""
    extractor_version: str = ""


class TextChunk(BaseModel):
    """One indexed span of an extracted document."""

    model_config = ConfigDict(extra="forbid")

    chunk_id: int | None = None
    document_id: int
    work_key: NonBlankStr
    ordinal: int = Field(ge=0)
    char_start: int = Field(ge=0)
    char_end: int = Field(ge=0)
    text: str


class CitationEdge(BaseModel):
    """One work citing another, as one provider reported it."""

    model_config = ConfigDict(extra="forbid")

    citing_key: NonBlankStr
    cited_key: NonBlankStr
    provider: NonBlankStr


class Relevance(StrEnum):
    """How relevant one retrieved work is to the goal. Advisory: it gates nothing."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    NONE = "none"


class WorkAssessment(BaseModel):
    """A read-only analyst's structured reading of one retrieved work."""

    model_config = ConfigDict(extra="forbid")

    work_key: NonBlankStr
    relevance: Relevance
    contribution: str = ""
    methods: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    datasets: list[str] = Field(default_factory=list)
    findings: list[str] = Field(default_factory=list)
    limitations: list[str] = Field(default_factory=list)


class LiteratureFinding(BaseModel):
    """One conclusion the analyst drew across the retrieved works.

    ``work_keys`` is required to be non-empty by the report validator rather
    than optional here: a finding with no ground is not a finding about the
    literature, it is the model's prior, and letting one through would be how an
    ungrounded claim acquires a citation downstream.
    """

    model_config = ConfigDict(extra="forbid")

    id: NonBlankStr
    statement: NonBlankStr
    importance: Importance
    confidence: Confidence
    work_keys: list[str] = Field(default_factory=list)

    @field_validator("id")
    @classmethod
    def _finding_id_shape(cls, value: str) -> str:
        if FINDING_ID_RE.fullmatch(value) is None:
            raise ValueError(
                "a finding id must be 1-32 characters of letters, digits, "
                "'-', '_', or '.'"
            )
        return value


class Disagreement(BaseModel):
    """One place the retrieved works actually conflict."""

    model_config = ConfigDict(extra="forbid")

    statement: NonBlankStr
    work_keys: list[str] = Field(default_factory=list)


class FollowupSuggestion(BaseModel):
    """One identifier the analyst suggests retrieving next.

    A suggestion and nothing more. Naming it here retrieves nothing: a later
    deterministic fetch may act on it, and that fetch validates the identifier
    the same way every other identifier is validated.
    """

    model_config = ConfigDict(extra="forbid")

    identifier: NonBlankStr
    reason: str = ""


class LiteratureReport(BaseModel):
    """A literature analyst's validated output.

    The validator enforces the property the whole untrusted-content boundary
    rests on: every work this report cites was in the packet the analyst was
    given. A model cannot introduce a citation, cannot attribute a claim to a
    paper it was not shown, and cannot widen the evidence base of a review by
    recalling something from training.
    """

    model_config = ConfigDict(extra="forbid")

    query: NonBlankStr
    summary: NonBlankStr
    supplied_keys: list[str] = Field(default_factory=list)
    assessments: list[WorkAssessment] = Field(default_factory=list)
    findings: list[LiteratureFinding] = Field(default_factory=list)
    disagreements: list[Disagreement] = Field(default_factory=list)
    uncertainties: list[NonBlankStr] = Field(default_factory=list)
    recommended_followup: list[FollowupSuggestion] = Field(default_factory=list)
    provider: NonBlankStr
    model: str | None = None
    invocation_id: str = ""
    raw_output_path: str | None = None

    @model_validator(mode="after")
    def _citations_were_actually_supplied(self) -> Self:
        supplied = set(self.supplied_keys)
        cited: set[str] = set()
        for assessment in self.assessments:
            cited.add(assessment.work_key)
        for finding in self.findings:
            if not finding.work_keys:
                raise ValueError(
                    f"finding {finding.id} cites no work; a literature finding "
                    "must name the retrieved works it rests on"
                )
            cited.update(finding.work_keys)
        for item in self.disagreements:
            cited.update(item.work_keys)
        invented = sorted(cited - supplied)
        if invented:
            raise ValueError(
                "the literature analyst cited works that were not retrieved for "
                "this query: " + ", ".join(invented) + ". Only the works supplied "
                "in the packet may be cited, so a paper recalled from training "
                "cannot enter the record as though this run had found it"
            )
        ids = [item.id for item in self.findings]
        if len(set(ids)) != len(ids):
            raise ValueError("literature finding ids must not repeat")
        keys = [item.work_key for item in self.assessments]
        if len(set(keys)) != len(keys):
            raise ValueError("a work must not be assessed twice in one report")
        return self

    def cited_keys(self) -> tuple[str, ...]:
        """Return every work this report actually cites, in a stable order."""

        cited = {item.work_key for item in self.assessments}
        for finding in self.findings:
            cited.update(finding.work_keys)
        for item in self.disagreements:
            cited.update(item.work_keys)
        return tuple(sorted(cited))


class SearchResult(BaseModel):
    """One locally ranked search result.

    ``score`` is BM25 as SQLite computed it, so it is comparable within one
    query and meaningless across queries. It is reported rather than hidden
    because a ranking a reader cannot inspect is a ranking they have to trust.
    """

    model_config = ConfigDict(extra="forbid")

    work: WorkRecord
    score: float
    matched: str
    snippet: str = ""


class KnownItem(BaseModel):
    """One entry of a known-item retrieval fixture.

    The evaluation this feeds is a regression guard, not a benchmark: it asks
    whether a work a human already decided is relevant is still retrieved and
    still ranked near the top. It is deliberately small and deliberately not
    optimised against.
    """

    model_config = ConfigDict(extra="forbid")

    fixture: NonBlankStr
    query: NonBlankStr
    work_key: NonBlankStr
    note: str = ""


class EvaluationOutcome(BaseModel):
    """What one known-item query actually retrieved."""

    model_config = ConfigDict(extra="forbid")

    query: str
    expected: list[str]
    retrieved: list[str]
    found: list[str]
    missing: list[str]
    ranks: dict[str, int] = Field(default_factory=dict)

    @property
    def recall(self) -> float:
        if not self.expected:
            return 1.0
        return len(self.found) / len(self.expected)

    @property
    def ok(self) -> bool:
        return not self.missing
