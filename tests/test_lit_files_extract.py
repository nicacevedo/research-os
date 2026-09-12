"""Storing a retrieved file, and establishing its text without guessing.

The file store's job is to make a retrieved PDF identifiable by what it contains
rather than by where it came from, and to refuse anything that is not what it
claims to be. The extractor's job is to turn a stored file into indexable text,
or to say clearly that it could not -- because a silently empty document makes
"this paper does not mention X" and "we could not read this paper" the same
answer.
"""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from research_os.errors import LiteratureError
from research_os.literature.extract import (
    CHUNK_CHARS,
    CHUNK_OVERLAP_CHARS,
    build_document,
    chunk_text,
    extract,
    extractor_available,
)
from research_os.literature.files import (
    MAX_FILE_BYTES,
    digest_bytes,
    normalize_media_type,
    object_path,
    read_file,
    store_file,
)
from research_os.literature.models import ExtractionStatus
from tests.literature_helpers import MINIMAL_PDF

needs_pdftotext = pytest.mark.skipif(
    shutil.which("pdftotext") is None,
    reason="pdftotext (poppler-utils) is not installed on this machine",
)


# -- content addressing -------------------------------------------------------


def test_a_file_is_stored_under_the_digest_of_its_bytes(tmp_path: Path) -> None:
    stored = store_file(MINIMAL_PDF, root=tmp_path, media_type="application/pdf")

    assert stored.sha256 == digest_bytes(MINIMAL_PDF)
    assert stored.path == object_path(tmp_path, stored.sha256)
    assert stored.path.read_bytes() == MINIMAL_PDF
    assert stored.already_present is False


def test_the_same_paper_from_two_providers_is_stored_once(tmp_path: Path) -> None:
    first = store_file(MINIMAL_PDF, root=tmp_path, media_type="application/pdf")
    second = store_file(
        MINIMAL_PDF, root=tmp_path, media_type="application/pdf; charset=binary"
    )

    assert second.sha256 == first.sha256
    assert second.already_present is True
    assert len(list(tmp_path.rglob("*"))) == 2, "one fanout directory, one object"


def test_a_login_wall_claiming_to_be_a_pdf_is_refused(tmp_path: Path) -> None:
    """Indexing it would put a publisher's cookie notice into the record."""

    html = b"<!DOCTYPE html><html><body>Please sign in to continue</body></html>"

    with pytest.raises(LiteratureError, match="does not begin like one"):
        store_file(html, root=tmp_path, media_type="application/pdf")
    assert list(tmp_path.rglob("*")) == []


def test_a_media_type_with_no_local_reader_is_refused(tmp_path: Path) -> None:
    with pytest.raises(LiteratureError, match="only"):
        store_file(b"MZ\x90\x00", root=tmp_path, media_type="application/octet-stream")


def test_an_empty_response_is_not_a_file(tmp_path: Path) -> None:
    with pytest.raises(LiteratureError, match="empty"):
        store_file(b"", root=tmp_path, media_type="application/pdf")


def test_an_oversized_file_is_refused(tmp_path: Path) -> None:
    oversized = b"%PDF-" + b"x" * MAX_FILE_BYTES

    with pytest.raises(LiteratureError, match="limit"):
        store_file(oversized, root=tmp_path, media_type="application/pdf")


def test_reading_a_stored_file_verifies_it_is_still_that_file(tmp_path: Path) -> None:
    """A digest that no longer describes its content is not an identifier."""

    stored = store_file(MINIMAL_PDF, root=tmp_path, media_type="application/pdf")
    stored.path.write_bytes(b"%PDF-tampered")

    with pytest.raises(LiteratureError, match="not the content that was retrieved"):
        read_file(tmp_path, stored.sha256)


def test_reading_an_absent_digest_is_an_error_not_an_empty_file(
    tmp_path: Path,
) -> None:
    with pytest.raises(LiteratureError, match="cannot read"):
        read_file(tmp_path, "0" * 64)


def test_media_types_normalise_past_their_parameters() -> None:
    assert normalize_media_type("Application/PDF; charset=UTF-8") == "application/pdf"
    assert normalize_media_type(None) == ""


# -- extraction ---------------------------------------------------------------


@needs_pdftotext
def test_a_real_pdf_yields_its_text_layer(tmp_path: Path) -> None:
    target = tmp_path / "paper.pdf"
    target.write_bytes(MINIMAL_PDF)

    result = extract(target, media_type="application/pdf")

    assert result.status is ExtractionStatus.EXTRACTED
    assert "widget deformation measured here" in result.text
    assert result.extractor == "pdftotext"
    assert result.extractor_version, "the parser version is part of reproducing this"


@needs_pdftotext
def test_a_pdf_with_no_text_layer_is_unsupported_rather_than_empty(
    tmp_path: Path,
) -> None:
    """A scan is not a paper that says nothing; Research OS does not run OCR."""

    target = tmp_path / "scan.pdf"
    target.write_bytes(
        b"%PDF-1.4\n1 0 obj<</Type/Catalog/Pages 2 0 R>>endobj\n"
        b"2 0 obj<</Type/Pages/Kids[3 0 R]/Count 1>>endobj\n"
        b"3 0 obj<</Type/Page/Parent 2 0 R/MediaBox[0 0 200 200]>>endobj\n"
        b"trailer<</Root 1 0 R>>\n"
    )

    result = extract(target, media_type="application/pdf")

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert "OCR" in result.detail
    assert result.text == ""


@needs_pdftotext
def test_something_that_is_not_a_pdf_fails_loudly(tmp_path: Path) -> None:
    target = tmp_path / "broken.pdf"
    target.write_bytes(b"%PDF-1.4\nthis is not really a pdf at all\n")

    result = extract(target, media_type="application/pdf")

    assert result.status in {ExtractionStatus.FAILED, ExtractionStatus.UNSUPPORTED}
    assert result.text == ""
    assert result.detail


def test_plain_text_is_extracted_without_an_external_parser(tmp_path: Path) -> None:
    target = tmp_path / "note.txt"
    target.write_text("widgets deform under load\n", encoding="utf-8")

    result = extract(target, media_type="text/plain")

    assert result.status is ExtractionStatus.EXTRACTED
    assert result.extractor == "builtin"
    assert "widgets deform" in result.text


def test_a_type_with_no_extractor_is_unsupported(tmp_path: Path) -> None:
    target = tmp_path / "thing.bin"
    target.write_bytes(b"\x00\x01")

    result = extract(target, media_type="application/octet-stream")

    assert result.status is ExtractionStatus.UNSUPPORTED
    assert "no local extractor" in result.detail


def test_control_characters_do_not_survive_extraction(tmp_path: Path) -> None:
    """The stored text and the displayed text should be the same thing."""

    target = tmp_path / "note.txt"
    target.write_bytes(b"before\x1b[2Jafter\nline two\n")

    result = extract(target, media_type="text/plain")

    assert "\x1b" not in result.text
    assert "before" in result.text and "after" in result.text
    assert "\n" in result.text, "line structure is content"


def test_the_extractor_reports_its_own_availability() -> None:
    assert extractor_available() is (shutil.which("pdftotext") is not None)


# -- chunking -----------------------------------------------------------------


def test_chunks_overlap_so_a_straddling_sentence_is_still_findable() -> None:
    text = "".join(f"{index:04d} " for index in range(2000))

    chunks = chunk_text(text, work_key="doi:10.1/x")

    assert len(chunks) > 1
    assert chunks[0].char_start == 0
    assert chunks[1].char_start == CHUNK_CHARS - CHUNK_OVERLAP_CHARS
    overlap = text[chunks[1].char_start : chunks[0].char_end]
    assert overlap and overlap in chunks[0].text and overlap in chunks[1].text


def test_chunk_ordinals_are_dense_and_start_at_zero() -> None:
    chunks = chunk_text("a" * 5000, work_key="doi:10.1/x")

    assert [item.ordinal for item in chunks] == list(range(len(chunks)))


def test_every_chunk_records_where_it_came_from() -> None:
    text = "x" * 4500

    chunks = chunk_text(text, work_key="doi:10.1/x")

    for chunk in chunks:
        assert chunk.work_key == "doi:10.1/x"
        assert text[chunk.char_start : chunk.char_end] == chunk.text


def test_empty_text_produces_no_chunks() -> None:
    assert chunk_text("   \n  ", work_key="doi:10.1/x") == []


def test_a_short_document_is_one_chunk() -> None:
    chunks = chunk_text("widgets deform under load", work_key="doi:10.1/x")

    assert len(chunks) == 1
    assert chunks[0].text == "widgets deform under load"


def test_an_overlap_that_is_not_smaller_than_the_chunk_is_refused() -> None:
    """Otherwise chunking would never advance."""

    with pytest.raises(ValueError, match="smaller"):
        chunk_text("abcdef", work_key="k", size=10, overlap=10)


@needs_pdftotext
def test_a_document_record_carries_the_parser_that_produced_it(
    tmp_path: Path,
) -> None:
    target = tmp_path / "paper.pdf"
    target.write_bytes(MINIMAL_PDF)
    result = extract(target, media_type="application/pdf")

    document, chunks = build_document(
        result, work_key="doi:10.1/x", file_sha256="a" * 64
    )

    assert document.extractor == "pdftotext"
    assert document.char_count == len(result.text)
    assert document.file_sha256 == "a" * 64
    assert chunks and chunks[0].work_key == "doi:10.1/x"
