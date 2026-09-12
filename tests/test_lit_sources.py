"""The three provider adapters, driven through the real parsing code.

Each test hands the adapter the shape of payload its provider actually returns
and checks the translation into the neutral record. What matters is not that the
JSON is read at all, but that the awkward parts are read correctly: OpenAlex
ships an inverted index instead of an abstract, Crossref ships a JATS fragment
and expresses retraction through ``updated-by`` rather than a flag, and arXiv
ships Atom whose default parser expands entities.

Every unusable-provider path is a value rather than an exception, because a
literature review has to be able to say which provider it could not reach.
"""

from __future__ import annotations

from xml.etree import ElementTree

import pytest

from research_os.literature.models import SourceStatus
from research_os.literature.sources.arxiv import ArxivSource, assert_no_doctype
from research_os.literature.sources.crossref import CrossrefSource
from research_os.literature.sources.openalex import (
    API_KEY_ENV,
    OpenAlexSource,
    _abstract_from_inverted_index,
)
from tests.literature_helpers import (
    ScriptedClient,
    ScriptedResponse,
    arxiv_feed,
    crossref_work,
    json_response,
    openalex_work,
)

# -- OpenAlex -----------------------------------------------------------------


def openalex(responses: list[ScriptedResponse], **kwargs: object) -> OpenAlexSource:
    return OpenAlexSource(client=ScriptedClient(responses=responses), **kwargs)  # type: ignore[arg-type]


def test_openalex_translates_a_work_into_the_neutral_shape() -> None:
    source = openalex(
        [json_response("api.openalex.org", {"results": [openalex_work()]})]
    )

    result = source.search("widgets", limit=5)

    assert result.status is SourceStatus.OK
    record = result.records[0]
    assert record.identifiers == {
        "openalex": "https://openalex.org/W2000000001",
        "doi": "https://doi.org/10.1000/alpha",
    }
    assert record.fields["title"] == "A study of widget dynamics"
    assert record.fields["publication_year"] == 2021
    assert record.fields["venue"] == "Journal of Widgets"
    assert record.fields["cited_by_count"] == 12
    assert [item.name for item in record.authors] == ["Jane Roe", "Kim Smith"]
    assert record.pdf_url == "https://example.invalid/alpha.pdf"


def test_an_inverted_index_is_reassembled_in_the_original_word_order() -> None:
    """OpenAlex ships no abstract text, only word positions."""

    index = {"Widgets": [0, 3], "behave": [1], "predictably": [2]}

    assert _abstract_from_inverted_index(index) == "Widgets behave predictably Widgets"


def test_a_gap_in_the_inverted_index_is_left_as_a_gap() -> None:
    """Inventing a word would be fabricating part of a source."""

    assert _abstract_from_inverted_index({"alpha": [0], "gamma": [2]}) == "alpha gamma"
    assert _abstract_from_inverted_index({}) is None
    assert _abstract_from_inverted_index(None) is None


def test_openalex_reports_its_remaining_budget_rather_than_guessing() -> None:
    source = openalex(
        [
            json_response(
                "api.openalex.org",
                {"results": []},
                **{
                    "x-ratelimit-limit": "1000",
                    "x-ratelimit-remaining": "4",
                    "x-ratelimit-credits-used": "1",
                    "x-ratelimit-reset": "8000",
                },
            )
        ]
    )

    result = source.search("widgets")

    assert "budget 4/1000" in result.rate_limit_note
    assert "resets in 8000s" in result.rate_limit_note


def test_an_exhausted_budget_is_rate_limited_not_failed() -> None:
    source = openalex(
        [ScriptedResponse(match="api.openalex.org", status=429, body=b"")]
    )

    result = source.search("widgets")

    assert result.status is SourceStatus.RATE_LIMITED
    assert result.records == ()


def test_an_unknown_identifier_is_an_answer_not_a_failure() -> None:
    source = openalex(
        [ScriptedResponse(match="api.openalex.org", status=404, body=b"")]
    )

    result = source.fetch("10.1000/missing")

    assert result.status is SourceStatus.OK
    assert result.records == ()
    assert "no record" in result.detail


def test_a_body_that_is_not_json_fails_rather_than_being_guessed_at() -> None:
    source = openalex(
        [ScriptedResponse(match="api.openalex.org", body=b"<html>maintenance</html>")]
    )

    result = source.search("widgets")

    assert result.status is SourceStatus.FAILED
    assert "not JSON" in result.detail


def test_the_openalex_probe_spends_no_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A doctor command must not consume the budget it is reporting on."""

    monkeypatch.delenv(API_KEY_ENV, raising=False)
    transport = ScriptedClient()
    source = OpenAlexSource(client=transport)

    probe = source.probe()

    assert transport.requests == []
    assert probe.status is SourceStatus.OK
    assert probe.credential_present is False
    assert probe.credential_env == API_KEY_ENV


def test_an_api_key_is_read_from_the_environment_and_never_configured_inline(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(API_KEY_ENV, "  token-from-env  ")

    probe = OpenAlexSource(client=ScriptedClient()).probe()

    assert probe.credential_present is True


def test_the_deprecated_polite_pool_parameter_is_not_sent() -> None:
    """Verified against the live documentation: mailto was deprecated in 2026."""

    transport = ScriptedClient(
        contact_email="tests@example.invalid",
        responses=[json_response("api.openalex.org", {"results": []})],
    )

    OpenAlexSource(client=transport).search("widgets")

    url = transport.urls()[0]
    assert "mailto" not in url
    assert "tests@example.invalid" in transport.headers_for("openalex")["User-Agent"]


# -- Crossref -----------------------------------------------------------------


def crossref(responses: list[ScriptedResponse], **kwargs: object) -> CrossrefSource:
    return CrossrefSource(
        client=ScriptedClient(
            contact_email="tests@example.invalid", responses=responses
        )
    )


def test_crossref_translates_a_work_and_reads_its_date_parts() -> None:
    source = crossref([json_response("api.crossref.org", {"message": crossref_work()})])

    result = source.fetch("10.1000/alpha")

    record = result.records[0]
    assert record.identifiers == {"doi": "10.1000/alpha"}
    assert record.fields["publication_year"] == 2021
    assert record.fields["venue"] == "Journal of Widgets"
    assert [item.name for item in record.authors] == ["Jane Roe", "Kim Smith"]


def test_a_retraction_is_read_from_updated_by() -> None:
    """The signal that a paper should not be cited as established."""

    source = crossref(
        [
            json_response(
                "api.crossref.org",
                {
                    "message": crossref_work(
                        updated_by=({"type": "retraction", "DOI": "10.1000/notice"},)
                    )
                },
            )
        ]
    )

    record = source.fetch("10.1000/alpha").records[0]

    assert record.fields["is_retracted"] is True
    assert "retraction by 10.1000/notice" in record.fields["retraction_note"]


def test_a_retraction_notice_is_not_itself_a_retracted_paper() -> None:
    """``update-to`` points the other way and must not flag this work."""

    source = crossref(
        [
            json_response(
                "api.crossref.org",
                {
                    "message": crossref_work(
                        update_to=({"type": "retraction", "DOI": "10.1000/original"},)
                    )
                },
            )
        ]
    )

    record = source.fetch("10.1000/alpha").records[0]

    assert record.fields["is_retracted"] is False
    assert "is itself a retraction" in record.fields["retraction_note"]


def test_a_jats_abstract_becomes_plain_sentences() -> None:
    source = crossref(
        [
            json_response(
                "api.crossref.org",
                {
                    "message": crossref_work(
                        abstract=(
                            "<jats:p>Widgets &amp; gadgets <jats:italic>differ"
                            "</jats:italic>.</jats:p>"
                        )
                    )
                },
            )
        ]
    )

    record = source.fetch("10.1000/alpha").records[0]

    assert record.fields["abstract"] == "Widgets & gadgets differ ."


def test_crossref_is_addressed_by_doi_and_says_so_for_anything_else() -> None:
    source = crossref([])

    result = source.fetch("arXiv:2101.00001")

    assert result.status is SourceStatus.OK
    assert result.records == ()
    assert "addressed by DOI" in result.detail


def test_the_polite_pool_parameter_is_sent_when_a_contact_is_configured() -> None:
    transport = ScriptedClient(
        contact_email="tests@example.invalid",
        responses=[json_response("api.crossref.org", {"message": {"items": []}})],
    )

    CrossrefSource(client=transport).search("widgets")

    assert "mailto=tests%40example.invalid" in transport.urls()[0]


def test_without_a_contact_the_probe_says_the_pool_is_not_guaranteed() -> None:
    probe = CrossrefSource(client=ScriptedClient(contact_email=None)).probe()

    assert probe.contact_configured is False
    assert "anonymous pool" in probe.rate_limit_note


# -- arXiv --------------------------------------------------------------------


def arxiv(responses: list[ScriptedResponse]) -> ArxivSource:
    return ArxivSource(client=ScriptedClient(responses=responses))


def test_arxiv_translates_an_atom_entry() -> None:
    source = arxiv(
        [
            ScriptedResponse(
                match="export.arxiv.org",
                body=arxiv_feed(doi="10.1000/alpha", journal_ref="Phys. Rev. X 1"),
                headers={"content-type": "application/atom+xml"},
            )
        ]
    )

    record = source.search("widgets").records[0]

    assert record.identifiers == {"arxiv": "2101.00001", "doi": "10.1000/alpha"}
    assert record.fields["publication_year"] == 2021
    assert record.fields["venue"] == "Phys. Rev. X 1"
    assert record.fields["work_type"] == "preprint"
    assert record.pdf_url == "https://arxiv.org/pdf/2101.00001v2"


def test_a_preprint_with_no_journal_reference_is_venued_as_arxiv() -> None:
    source = arxiv([ScriptedResponse(match="export.arxiv.org", body=arxiv_feed())])

    record = source.search("widgets").records[0]

    assert record.fields["venue"] == "arXiv"


def test_an_arxiv_pdf_link_is_upgraded_to_https() -> None:
    source = arxiv([ScriptedResponse(match="export.arxiv.org", body=arxiv_feed())])

    record = source.search("widgets").records[0]

    assert record.pdf_url is not None
    assert record.pdf_url.startswith("https://")


def test_a_document_type_declaration_is_refused_before_parsing() -> None:
    """Python's default parser really does expand internal entities."""

    lolz = (
        '<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
        "<feed><entry><title>&lol;</title></entry></feed>"
    )

    with pytest.raises(ElementTree.ParseError, match="document type"):
        assert_no_doctype(lolz)


def test_a_paper_that_merely_mentions_a_doctype_is_not_refused() -> None:
    """Only the prolog is inspected, so quoting the characters is fine."""

    assert_no_doctype(
        '<feed xmlns="http://www.w3.org/2005/Atom"><entry>'
        "<title>Parsing &lt;!DOCTYPE&gt; safely</title></entry></feed>"
    )


def test_a_hostile_feed_fails_the_call_rather_than_the_process() -> None:
    source = arxiv(
        [
            ScriptedResponse(
                match="export.arxiv.org",
                body=(
                    b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol">]>'
                    b"<feed><entry><title>&lol;</title></entry></feed>"
                ),
            )
        ]
    )

    result = source.search("widgets")

    assert result.status is SourceStatus.FAILED
    assert "Atom XML" in result.detail


def test_arxiv_is_addressed_by_arxiv_id_and_says_so_for_anything_else() -> None:
    result = arxiv([]).fetch("10.1000/alpha")

    assert result.status is SourceStatus.OK
    assert result.records == ()
    assert "addressed by one" in result.detail


def test_the_arxiv_probe_states_the_published_interval() -> None:
    probe = ArxivSource(client=ScriptedClient()).probe()

    assert "three seconds" in probe.rate_limit_note
    assert probe.requires_credential is False


# -- every adapter ------------------------------------------------------------


@pytest.mark.parametrize(
    "build",
    [
        lambda transport: OpenAlexSource(client=transport),
        lambda transport: CrossrefSource(client=transport),
        lambda transport: ArxivSource(client=transport),
    ],
)
def test_offline_makes_every_source_unavailable_rather_than_broken(build) -> None:
    source = build(ScriptedClient(offline=True))

    probe = source.probe()
    result = source.search("widgets")

    assert probe.status is SourceStatus.UNAVAILABLE
    assert result.status is SourceStatus.UNAVAILABLE
    assert result.records == ()
