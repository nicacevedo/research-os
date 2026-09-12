"""When two provider records are the same paper, and when they are not.

Identity is the decision every other part of the literature subsystem rests on.
Get it wrong in one direction and the index fills with three copies of every
paper; get it wrong in the other and two distinct papers are silently merged and
one of them ceases to exist. So the rule is a precedence over registered
identifiers, with a strict fallback, and these tests pin both halves: what
normalises together, and what deliberately does not.
"""

from __future__ import annotations

import pytest

from research_os.literature.identity import (
    IDENTIFIER_PRECEDENCE,
    normalize_arxiv_id,
    normalize_author,
    normalize_doi,
    normalize_openalex_id,
    normalize_pmid,
    normalize_title,
    split_work_key,
    title_fallback_key,
    work_key,
)


@pytest.mark.parametrize(
    "value",
    [
        "10.1038/nature12373",
        "10.1038/NATURE12373",
        "https://doi.org/10.1038/nature12373",
        "http://dx.doi.org/10.1038/nature12373",
        "doi:10.1038/nature12373",
        "  10.1038/nature12373  ",
        "10.1038/nature12373.",
    ],
)
def test_every_way_a_provider_writes_one_doi_normalises_together(value: str) -> None:
    assert normalize_doi(value) == "10.1038/nature12373"


@pytest.mark.parametrize(
    "value",
    ["", None, "nature12373", "10.1038", "not a doi", "10.1038/with space"],
)
def test_something_that_is_not_a_doi_is_refused_rather_than_stored(
    value: str | None,
) -> None:
    """A field that sometimes holds a DOI is not an identifier."""

    assert normalize_doi(value) is None


@pytest.mark.parametrize(
    "value",
    [
        "2301.00001",
        "2301.00001v1",
        "2301.00001v17",
        "arXiv:2301.00001",
        "https://arxiv.org/abs/2301.00001v3",
        "https://arxiv.org/pdf/2301.00001v3.pdf",
    ],
)
def test_an_arxiv_version_is_not_a_different_paper(value: str) -> None:
    """v1 and v3 are one work at two moments, not two works."""

    assert normalize_arxiv_id(value) == "2301.00001"


def test_the_old_arxiv_identifier_scheme_still_resolves() -> None:
    assert normalize_arxiv_id("cond-mat/0011267v1") == "cond-mat/0011267"
    assert normalize_arxiv_id("https://arxiv.org/abs/math.GT/0309136") == (
        "math.gt/0309136"
    )


@pytest.mark.parametrize("value", ["", None, "2301", "not-an-id", "20301.000012"])
def test_a_malformed_arxiv_identifier_is_refused(value: str | None) -> None:
    assert normalize_arxiv_id(value) is None


def test_openalex_identifiers_normalise_to_the_bare_work_id() -> None:
    assert normalize_openalex_id("https://openalex.org/W2741809807") == "W2741809807"
    assert normalize_openalex_id("w2741809807") == "W2741809807"
    assert normalize_openalex_id("A5023888391") is None, "an author id is not a work"


def test_pubmed_identifiers_normalise_to_digits() -> None:
    assert normalize_pmid("PMID:12345678") == "12345678"
    assert normalize_pmid("12345678") == "12345678"
    assert normalize_pmid("PMC12345") is None


def test_titles_compare_on_their_words_and_nothing_else() -> None:
    left = normalize_title("Nanometre-scale Thermometry  in a Living Cell")
    right = normalize_title("NANOMETRE SCALE thermometry in a living cell!")

    assert left == right == "nanometre scale thermometry in a living cell"


def test_an_accent_does_not_make_two_titles_different() -> None:
    assert normalize_title("Réponse thermique") == normalize_title("Reponse thermique")


def test_a_non_latin_title_is_not_reduced_to_nothing() -> None:
    """Otherwise two unrelated papers would share one fallback identity.

    A pattern matching Latin letters alone would turn a Chinese or Cyrillic
    title into the few Latin words it happens to contain -- often none -- and the
    title-plus-author-plus-year fallback would then merge papers that have
    nothing to do with each other.
    """

    chinese = normalize_title("量子测温 in diamond")
    cyrillic = normalize_title("Квантовая термометрия")

    assert chinese == "量子测温 in diamond"
    assert cyrillic == "квантовая термометрия"
    assert title_fallback_key(
        title="量子测温", first_author="Li", year=2021
    ) != title_fallback_key(title="納米温度計", first_author="Li", year=2021)


def test_an_author_compares_on_surname_however_it_was_written() -> None:
    assert normalize_author("Jane Q. Roe") == "roe"
    assert normalize_author("Roe, Jane Q.") == "roe"
    assert normalize_author("J. Roe") == "roe"
    assert normalize_author("") == ""


# -- the fallback, and why it is strict --------------------------------------


def test_the_fallback_needs_a_title_an_author_and_a_year() -> None:
    """Two of the three is not enough to assert two papers are one."""

    assert title_fallback_key(title="A paper", first_author="Roe", year=None) is None
    assert title_fallback_key(title="A paper", first_author=None, year=2020) is None
    assert title_fallback_key(title="", first_author="Roe", year=2020) is None


def test_the_fallback_agrees_across_provider_spellings() -> None:
    left = title_fallback_key(
        title="Widget Dynamics Under Load", first_author="Jane Roe", year=2021
    )
    right = title_fallback_key(
        title="widget   dynamics under load", first_author="Roe, J.", year=2021
    )

    assert left is not None and left == right


def test_the_same_title_in_a_different_year_is_a_different_work() -> None:
    """Same-titled papers by different groups in different years are ordinary."""

    first = title_fallback_key(title="Scaling laws", first_author="Roe", year=2019)
    second = title_fallback_key(title="Scaling laws", first_author="Roe", year=2021)

    assert first != second


def test_the_same_title_by_a_different_group_is_a_different_work() -> None:
    first = title_fallback_key(title="Scaling laws", first_author="Roe", year=2021)
    second = title_fallback_key(title="Scaling laws", first_author="Smith", year=2021)

    assert first != second


# -- keys ---------------------------------------------------------------------


def test_a_key_round_trips_through_its_scheme_and_value() -> None:
    key = work_key("doi", "10.1000/xyz")

    assert key == "doi:10.1000/xyz"
    assert split_work_key(key) == ("doi", "10.1000/xyz")


def test_the_precedence_is_the_whole_rule() -> None:
    """Stated as one tuple so it cannot disagree with itself somewhere else."""

    assert IDENTIFIER_PRECEDENCE == ("doi", "arxiv", "openalex", "pmid")
