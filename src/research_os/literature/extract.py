"""Turning a stored file into indexable local text.

Deterministic and local. ``pdftotext`` from poppler is a normal local parser, it
is already on this machine, and it is run the way every other external command in
Research OS is run: an argument vector, no shell, a timeout, and a bounded
output. No OCR, because OCR is a different problem with different failure modes
and this subsystem does not need it: a scanned PDF with no text layer is
reported as unextractable rather than guessed at.

Failure is explicit and recorded. A file whose text could not be established
keeps an ``UNSUPPORTED`` or ``FAILED`` extraction status, and it simply does not
appear in full-text search. That is the honest outcome; the alternative -- a
silently empty document -- would make "not found" and "not readable" the same
answer, and a literature review cannot tell those apart afterwards.

Extracted text is untrusted. It is the words a stranger wrote, and it reaches a
model only through :mod:`.packet`.
"""

from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path

from research_os.automation.models import utc_now
from research_os.literature.models import (
    ExtractionStatus,
    TextChunk,
    TextDocument,
)
from research_os.textsafe import CONTROL_CHARS

#: The external extractor, and the only one.
PDFTOTEXT = "pdftotext"

#: How long one extraction may take. A PDF that needs longer than this is
#: pathological, and the run should say so rather than stall.
EXTRACT_TIMEOUT_SECONDS = 120

#: The most text kept from one file. Beyond this a document is truncated and
#: says so: an index entry is for finding a paper, not for holding a book.
MAX_TEXT_CHARS = 2_000_000

#: How long one indexed chunk is, and how much consecutive chunks overlap.
#:
#: The overlap exists because a sentence that straddles a boundary would
#: otherwise be findable by neither chunk. It is small, so the cost is a few
#: percent of duplicated text rather than a doubled index.
CHUNK_CHARS = 2_000
CHUNK_OVERLAP_CHARS = 200


@dataclass(frozen=True, slots=True)
class ExtractionResult:
    """What the local extractor established about one file."""

    status: ExtractionStatus
    text: str
    extractor: str
    extractor_version: str
    detail: str = ""
    truncated: bool = False

    @property
    def ok(self) -> bool:
        return self.status is ExtractionStatus.EXTRACTED


def extractor_available() -> bool:
    return shutil.which(PDFTOTEXT) is not None


def extractor_version() -> str:
    """Return the local poppler version, or an empty string if absent.

    Recorded with every document, because "which parser produced this text" is
    part of reproducing it: two poppler versions can disagree about column order
    in a two-column paper.
    """

    path = shutil.which(PDFTOTEXT)
    if path is None:
        return ""
    try:
        completed = subprocess.run(
            [path, "-v"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError):
        return ""
    combined = (completed.stderr or "") + (completed.stdout or "")
    for line in combined.splitlines():
        if line.lower().startswith("pdftotext version"):
            return line.strip()
    return ""


def extract(path: Path, *, media_type: str) -> ExtractionResult:
    """Extract the text of one stored file, or say why that was not possible."""

    kind = media_type.split(";", 1)[0].strip().lower()
    if kind in {"text/plain", "text/xml", "application/xml"}:
        return _extract_plain(path, kind)
    if kind == "application/pdf":
        return _extract_pdf(path)
    return ExtractionResult(
        status=ExtractionStatus.UNSUPPORTED,
        text="",
        extractor="",
        extractor_version="",
        detail=f"no local extractor handles {kind or 'an unknown media type'}",
    )


def _extract_plain(path: Path, kind: str) -> ExtractionResult:
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            extractor="builtin",
            extractor_version="",
            detail=f"cannot read the stored file: {exc}",
        )
    text, truncated = _bound(raw.decode("utf-8", errors="replace"))
    return ExtractionResult(
        status=ExtractionStatus.EXTRACTED,
        text=text,
        extractor="builtin",
        extractor_version=kind,
        truncated=truncated,
    )


def _extract_pdf(path: Path) -> ExtractionResult:
    executable = shutil.which(PDFTOTEXT)
    if executable is None:
        return ExtractionResult(
            status=ExtractionStatus.UNSUPPORTED,
            text="",
            extractor=PDFTOTEXT,
            extractor_version="",
            detail=(
                f"{PDFTOTEXT} is not installed, so PDF text cannot be established "
                "locally. Install poppler-utils to index full text."
            ),
        )
    try:
        completed = subprocess.run(
            # -q suppresses progress chatter; "-" writes to stdout so nothing is
            # created beside the stored object.
            [executable, "-q", "-enc", "UTF-8", str(path), "-"],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=EXTRACT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            extractor=PDFTOTEXT,
            extractor_version=extractor_version(),
            detail=f"extraction timed out after {EXTRACT_TIMEOUT_SECONDS}s",
        )
    except OSError as exc:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            extractor=PDFTOTEXT,
            extractor_version="",
            detail=f"{PDFTOTEXT} could not be started: {exc}",
        )
    version = extractor_version()
    if completed.returncode != 0:
        return ExtractionResult(
            status=ExtractionStatus.FAILED,
            text="",
            extractor=PDFTOTEXT,
            extractor_version=version,
            detail=(
                f"{PDFTOTEXT} exited {completed.returncode}: "
                + (completed.stderr or b"")
                .decode("utf-8", errors="replace")
                .strip()[:400]
            ),
        )
    text, truncated = _bound(
        (completed.stdout or b"").decode("utf-8", errors="replace")
    )
    if not text.strip():
        return ExtractionResult(
            status=ExtractionStatus.UNSUPPORTED,
            text="",
            extractor=PDFTOTEXT,
            extractor_version=version,
            detail=(
                "the PDF has no extractable text layer; it is most likely a scan. "
                "Research OS does not run OCR, so this file is not indexed."
            ),
        )
    return ExtractionResult(
        status=ExtractionStatus.EXTRACTED,
        text=text,
        extractor=PDFTOTEXT,
        extractor_version=version,
        truncated=truncated,
    )


def _bound(text: str) -> tuple[str, bool]:
    """Return the text with control characters and length brought under control.

    Control characters go because they are noise in an index and a hazard on the
    way to a terminal; the display boundary would neutralise them anyway, and
    removing them here keeps the stored text and the displayed text the same
    thing. The original PDF is untouched and still addressable by its digest.
    """

    cleaned = "".join(
        character
        if character not in CONTROL_CHARS or character in {"\n", "\t"}
        else " "
        for character in text
    )
    if len(cleaned) <= MAX_TEXT_CHARS:
        return cleaned, False
    return cleaned[:MAX_TEXT_CHARS], True


def chunk_text(
    text: str,
    *,
    work_key: str,
    document_id: int = 0,
    size: int = CHUNK_CHARS,
    overlap: int = CHUNK_OVERLAP_CHARS,
) -> list[TextChunk]:
    """Split extracted text into overlapping indexable spans.

    Character spans rather than sentences or sections. A scholarly PDF's
    structure after extraction is not reliable enough to split on -- headings
    merge into body text, columns interleave -- and a fixed span with a small
    overlap is deterministic, which is what makes an index reproducible.

    Every chunk records where it came from, so a search hit can be shown with
    its position in the document rather than as a floating quotation.
    """

    if size <= 0:
        raise ValueError("chunk size must be positive")
    if not 0 <= overlap < size:
        raise ValueError("chunk overlap must be smaller than the chunk size")
    body = text.strip()
    if not body:
        return []
    stride = size - overlap
    chunks: list[TextChunk] = []
    start = 0
    while start < len(body):
        end = min(start + size, len(body))
        piece = body[start:end]
        if piece.strip():
            chunks.append(
                TextChunk(
                    document_id=document_id,
                    work_key=work_key,
                    ordinal=len(chunks),
                    char_start=start,
                    char_end=end,
                    text=piece,
                )
            )
        if end >= len(body):
            break
        start += stride
    return chunks


def build_document(
    result: ExtractionResult,
    *,
    work_key: str,
    file_sha256: str,
    kind: str = "fulltext",
) -> tuple[TextDocument, list[TextChunk]]:
    """Return the document row and its chunks for one successful extraction."""

    document = TextDocument(
        work_key=work_key,
        file_sha256=file_sha256,
        kind=kind,
        extracted_at=utc_now(),
        char_count=len(result.text),
        extractor=result.extractor,
        extractor_version=result.extractor_version,
    )
    return document, chunk_text(result.text, work_key=work_key)
