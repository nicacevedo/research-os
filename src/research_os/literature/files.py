"""Content-addressed storage for retrieved scholarly files.

A retrieved PDF is evidence, and evidence is identified by what it contains
rather than by where it came from. So a file is stored under the SHA-256 of its
bytes: fetching the same paper from two providers stores it once, a file that
changed is a different file rather than a silent overwrite, and a citation that
names a digest names exactly one sequence of bytes forever.

The store is deliberately dumb about what it holds. It never opens, parses,
renders, or executes anything, and it never hands a path to a tool-enabled
worker. Text extraction is a separate, explicit step in :mod:`.extract`, and
what a model eventually sees is a fenced packet built in :mod:`.packet`.

Two refusals are worth naming because they are the ones that would otherwise
bite. A response body is capped before it ever reaches here, so a hostile server
cannot exhaust memory. And a media type the caller did not ask for is refused
rather than stored under a name that misdescribes it: a "PDF" that is actually
HTML is a login wall, not a paper, and indexing its text as the paper's would put
a publisher's cookie notice into a literature review.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from research_os.errors import LiteratureError

#: The largest file that is ever stored.
MAX_FILE_BYTES = 64 * 1024 * 1024

#: What a stored scholarly file is allowed to be.
#:
#: Narrow on purpose. These are the types the local extractor can actually read;
#: anything else would be stored as a blob nothing could ever use.
ALLOWED_MEDIA_TYPES: frozenset[str] = frozenset(
    {
        "application/pdf",
        "text/plain",
        "text/xml",
        "application/xml",
    }
)

#: The first bytes each allowed type must actually begin with.
#:
#: Checked because a publisher returning an HTML interstitial with
#: ``Content-Type: application/pdf`` is ordinary, and storing it would put a
#: cookie banner into the index as though it were a paper.
_MAGIC: dict[str, tuple[bytes, ...]] = {
    "application/pdf": (b"%PDF-",),
}


@dataclass(frozen=True, slots=True)
class StoredFile:
    """One file as it now exists in the content-addressed store."""

    sha256: str
    path: Path
    byte_size: int
    media_type: str
    already_present: bool


def digest_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def object_path(root: Path, sha256: str) -> Path:
    """Return where one digest is stored.

    Fanned out over the first two hex characters so a store with a hundred
    thousand papers does not become one directory with a hundred thousand
    entries, which some filesystems handle badly and every ``ls`` handles badly.
    """

    return root / sha256[:2] / sha256[2:]


def normalize_media_type(raw: str | None) -> str:
    """Return the bare media type from a Content-Type header."""

    if not raw:
        return ""
    return raw.split(";", 1)[0].strip().lower()


def store_file(
    payload: bytes,
    *,
    root: Path,
    media_type: str,
) -> StoredFile:
    """Store ``payload`` under its digest and return where it landed.

    Written through a temporary file and an atomic rename, so an interrupted
    download never leaves a truncated file sitting at a digest that promises
    complete content. A digest already present is returned untouched: the bytes
    cannot differ, because the digest is of the bytes.
    """

    kind = normalize_media_type(media_type)
    if kind not in ALLOWED_MEDIA_TYPES:
        raise LiteratureError(
            f"refusing to store a {kind or 'typeless'} file; Research OS stores "
            "only " + ", ".join(sorted(ALLOWED_MEDIA_TYPES))
        )
    if len(payload) > MAX_FILE_BYTES:
        raise LiteratureError(
            f"refusing to store {len(payload)} bytes; the limit for one "
            f"scholarly file is {MAX_FILE_BYTES}"
        )
    if not payload:
        raise LiteratureError("refusing to store an empty file")
    expected = _MAGIC.get(kind)
    if expected and not any(payload.startswith(item) for item in expected):
        raise LiteratureError(
            f"the response claims to be {kind} but does not begin like one; it is "
            "most likely an access-denied or cookie page rather than the paper, "
            "and indexing its text would put that page into the literature record"
        )

    sha256 = digest_bytes(payload)
    target = object_path(root, sha256)
    if target.is_file():
        return StoredFile(
            sha256=sha256,
            path=target,
            byte_size=target.stat().st_size,
            media_type=kind,
            already_present=True,
        )
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, suffix=".partial")
        with os.fdopen(handle, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, target)
    except OSError as exc:
        raise LiteratureError(
            f"cannot store a scholarly file at {target}: {exc}"
        ) from exc
    return StoredFile(
        sha256=sha256,
        path=target,
        byte_size=len(payload),
        media_type=kind,
        already_present=False,
    )


def read_file(root: Path, sha256: str) -> bytes:
    """Return the stored bytes for one digest, verifying them on the way out.

    The verification is not paranoia about the disk; it is what makes the digest
    an identifier rather than a filename. A stored object whose content no longer
    hashes to its own name is not the file anything cited.
    """

    target = object_path(root, sha256)
    try:
        payload = target.read_bytes()
    except OSError as exc:
        raise LiteratureError(f"cannot read stored file {sha256}: {exc}") from exc
    actual = digest_bytes(payload)
    if actual != sha256:
        raise LiteratureError(
            f"stored file {sha256} now hashes to {actual}; it is not the content "
            "that was retrieved and must not be used as if it were"
        )
    return payload
