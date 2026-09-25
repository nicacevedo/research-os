"""Immutable bytes, addressed by content.

Everything large that an autonomous run produces or fetches goes here: a PDF, a
model's raw response, the exact prompt that produced it, stdout, stderr, a
result table, a figure, a dataset snapshot, a compiled document, a frozen review
packet. PostgreSQL holds the reference; Git holds only the lightweight
scientific object that cites it.

The reasons that ordering matters:

**A checkpoint must not contain a PDF.** LangGraph persists state at every
superstep. State that carries bytes is a checkpoint table that grows without
bound and a resume that has to deserialise a corpus. State carries
:class:`~research_os.runtime.interfaces.ArtifactRef` instead -- a hash, a media
type and a role.

**A prompt must not contain unbounded retrieved text.** What reaches a model is
a bounded excerpt behind the appropriate fence; what is *recorded* is the whole
artifact. Those are different requirements and conflating them either truncates
the evidence or blows up the context.

**Content addressing makes repeated work free and provenance exact.** Two runs
that fetch the same paper produce one artifact. A model call records the hash of
what went in and the hash of what came out, so "which inputs produced this
output" is answerable years later without trusting a filename.

Immutability is enforced by the naming, not by permissions: the path *is* the
hash of the contents, so there is no way to write different bytes to the same
name. Writing the same bytes twice is therefore safe by construction, which is
what makes concurrent writers and retried work uninteresting.
"""

from __future__ import annotations

import hashlib
import logging
import mimetypes
import os
import tempfile
from pathlib import Path, PurePosixPath

from research_os.automation.filescope import open_contained
from research_os.errors import ResearchOSError
from research_os.runtime.interfaces import ArtifactRef

LOG = logging.getLogger("research_os.runtime.artifacts")

#: Read files in chunks rather than whole, so hashing a multi-gigabyte dataset
#: snapshot does not require holding it in memory.
CHUNK_BYTES = 1024 * 1024


class ArtifactError(ResearchOSError):
    """Raised when artifact bytes cannot be stored or retrieved."""


class ArtifactMissingError(ArtifactError):
    """Raised when a referenced artifact is not in the store.

    A distinct class because it has a distinct meaning: the reference is
    syntactically fine and the bytes are gone. That is
    ``FailureClass.ARTIFACT_MISSING`` and it is not retryable -- retrying will
    not make the bytes reappear, and the honest outcome is to say what is
    missing.
    """


def hash_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def hash_file(path: Path) -> tuple[str, int]:
    """Return ``(sha256, size)`` for a file, read in chunks."""

    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(CHUNK_BYTES):
            digest.update(chunk)
            size += len(chunk)
    return digest.hexdigest(), size


class FilesystemArtifactStore:
    """A content-addressed store on the local filesystem.

    Layout is two levels of hash prefix, which keeps any one directory to a few
    thousand entries on a store with millions of artifacts:

    ```text
    <root>/sha256/ab/cd/abcd...ff
    ```

    ``store`` is an optional :class:`~research_os.runtime.store.RuntimeStore`.
    When present, metadata is recorded in PostgreSQL so artifacts are
    listable and attributable; when absent the store still works, which is what
    lets a unit test use it without a database.
    """

    __slots__ = ("_root", "_store")

    def __init__(self, root: Path, *, store: object | None = None) -> None:
        self._root = Path(root)
        self._store = store

    @property
    def root(self) -> Path:
        return self._root

    def path_for(self, artifact_id: str) -> Path:
        """Where the bytes for ``artifact_id`` live.

        Validates the id first. A hash is the only thing that may be
        interpolated into a path here, and accepting anything else would make
        this a path-traversal primitive.
        """

        if len(artifact_id) != 64 or not all(
            character in "0123456789abcdef" for character in artifact_id
        ):
            raise ArtifactError(
                f"not an artifact id: {artifact_id!r} (expected 64 lowercase hex digits)"
            )
        return self._root / "sha256" / artifact_id[:2] / artifact_id[2:4] / artifact_id

    def exists(self, artifact_id: str) -> bool:
        return self.path_for(artifact_id).is_file()

    def _write(self, artifact_id: str, source: Path | bytes) -> int:
        """Place bytes at their content address, atomically and idempotently.

        If the target already exists the bytes are identical by construction --
        the path is their hash -- so the write is skipped rather than repeated.
        Otherwise a temporary file in the *same directory* is fsynced and
        ``os.replace``d into place, which is atomic on the same filesystem, so a
        crash mid-write leaves either the old state or the complete new file and
        never a truncated artifact.
        """

        target = self.path_for(artifact_id)
        if target.is_file():
            return target.stat().st_size
        target.parent.mkdir(parents=True, exist_ok=True)
        handle, temporary = tempfile.mkstemp(dir=target.parent, prefix=".incoming-")
        written = 0
        try:
            with os.fdopen(handle, "wb") as sink:
                if isinstance(source, bytes):
                    sink.write(source)
                    written = len(source)
                else:
                    with source.open("rb") as origin:
                        while chunk := origin.read(CHUNK_BYTES):
                            sink.write(chunk)
                            written += len(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            os.replace(temporary, target)
        except OSError as exc:
            Path(temporary).unlink(missing_ok=True)
            raise ArtifactError(
                f"could not store artifact {artifact_id}: {exc}"
            ) from None
        return written

    def _record(
        self,
        artifact_id: str,
        *,
        size_bytes: int,
        media_type: str,
        role: str | None,
        producer: str | None,
        source: str | None,
    ) -> None:
        if self._store is None:
            return
        with self._store.db.tx() as conn:  # type: ignore[attr-defined]
            conn.execute(
                """
                insert into artifacts (artifact_id, size_bytes, media_type, role, producer, source)
                values (%(artifact_id)s, %(size_bytes)s, %(media_type)s, %(role)s,
                        %(producer)s, %(source)s)
                on conflict (artifact_id) do nothing
                """,
                {
                    "artifact_id": artifact_id,
                    "size_bytes": size_bytes,
                    "media_type": media_type,
                    "role": role,
                    "producer": producer,
                    "source": source,
                },
            )

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str = "application/octet-stream",
        role: str | None = None,
        producer: str | None = None,
        source: str | None = None,
    ) -> ArtifactRef:
        artifact_id = hash_bytes(data)
        size = self._write(artifact_id, data)
        self._record(
            artifact_id,
            size_bytes=size,
            media_type=media_type,
            role=role,
            producer=producer,
            source=source,
        )
        return ArtifactRef(
            artifact_id=artifact_id, media_type=media_type, role=role, size_bytes=size
        )

    def put_text(
        self,
        text: str,
        *,
        media_type: str = "text/plain; charset=utf-8",
        role: str | None = None,
        producer: str | None = None,
        source: str | None = None,
    ) -> ArtifactRef:
        return self.put_bytes(
            text.encode("utf-8"),
            media_type=media_type,
            role=role,
            producer=producer,
            source=source,
        )

    def put_file(
        self,
        path: Path,
        *,
        media_type: str | None = None,
        role: str | None = None,
        producer: str | None = None,
        source: str | None = None,
    ) -> ArtifactRef:
        target = Path(path)
        if not target.is_file():
            raise ArtifactError(f"not a file: {target}")
        artifact_id, size = hash_file(target)
        self._write(artifact_id, target)
        guessed = (
            media_type
            or mimetypes.guess_type(target.name)[0]
            or ("application/octet-stream")
        )
        self._record(
            artifact_id,
            size_bytes=size,
            media_type=guessed,
            role=role,
            producer=producer,
            source=source or str(target),
        )
        return ArtifactRef(
            artifact_id=artifact_id, media_type=guessed, role=role, size_bytes=size
        )

    def put_contained(
        self,
        root: Path,
        relative: str,
        *,
        media_type: str | None = None,
        role: str | None = None,
        producer: str | None = None,
    ) -> ArtifactRef | None:
        """Store ``root/relative`` if it is contained there, else ``None``.

        For a directory a run could write. Opened once through
        :func:`open_contained`, and hashed while it is copied, so the bytes
        stored are the bytes hashed: :meth:`put_file` hashes, then reopens by
        path to copy, and anything that swapped the file in between would be
        stored under the first file's address.
        """

        handle = open_contained(root, relative)
        if handle is None:
            return None
        incoming = self._root / "sha256"
        try:
            incoming.mkdir(parents=True, exist_ok=True)
            descriptor, temporary = tempfile.mkstemp(dir=incoming, prefix=".incoming-")
        except OSError as exc:
            handle.close()
            raise ArtifactError(f"could not stage {relative}: {exc}") from None
        digest = hashlib.sha256()
        size = 0
        try:
            with handle, os.fdopen(descriptor, "wb") as sink:
                while chunk := handle.read(CHUNK_BYTES):
                    digest.update(chunk)
                    sink.write(chunk)
                    size += len(chunk)
                sink.flush()
                os.fsync(sink.fileno())
            artifact_id = digest.hexdigest()
            target = self.path_for(artifact_id)
            if target.is_file():
                Path(temporary).unlink(missing_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(temporary, target)
        except OSError as exc:
            Path(temporary).unlink(missing_ok=True)
            raise ArtifactError(f"could not store {relative}: {exc}") from None
        guessed = (
            media_type
            or mimetypes.guess_type(PurePosixPath(relative).name)[0]
            or "application/octet-stream"
        )
        self._record(
            artifact_id,
            size_bytes=size,
            media_type=guessed,
            role=role,
            producer=producer,
            source=str(root / relative),
        )
        return ArtifactRef(
            artifact_id=artifact_id, media_type=guessed, role=role, size_bytes=size
        )

    def get_bytes(self, artifact_id: str) -> bytes:
        target = self.path_for(artifact_id)
        if not target.is_file():
            raise ArtifactMissingError(f"artifact {artifact_id} is not in the store")
        return target.read_bytes()

    def get_text(self, artifact_id: str) -> str:
        return self.get_bytes(artifact_id).decode("utf-8")

    def verify(self, artifact_id: str) -> bool:
        """Re-hash the stored bytes and confirm they match their address.

        Exists for the integrity audit schedule. Corruption here is silent
        otherwise: the file is still there and still the right length, and the
        first thing to notice would be a model reading a damaged PDF.
        """

        target = self.path_for(artifact_id)
        if not target.is_file():
            return False
        actual, _size = hash_file(target)
        return actual == artifact_id

    def link(
        self,
        ref: ArtifactRef,
        *,
        role: str,
        run_id: str | None = None,
        work_id: str | None = None,
    ) -> None:
        """Attach an artifact to the run or work item that used it.

        Separate from storing it, because one artifact is legitimately used by
        several runs -- the same paper, the same dataset snapshot -- and the
        bytes should not be duplicated to record that.
        """

        if self._store is None:
            return
        with self._store.db.tx() as conn:  # type: ignore[attr-defined]
            conn.execute(
                # `project_id` is derived from the run here rather than passed
                # in, so it cannot disagree with it and no caller has to
                # remember. It is what keeps this link findable after the run
                # is pruned: the preregistration guard scopes by project, and
                # before 0015 it could only reach the project through the run.
                """
                insert into artifact_links
                    (artifact_id, run_id, work_id, role, project_id)
                values (
                    %(artifact_id)s, %(run_id)s, %(work_id)s, %(role)s,
                    (select project_id from research_runs where run_id = %(run_id)s)
                )
                on conflict do nothing
                """,
                {
                    "artifact_id": ref.artifact_id,
                    "run_id": run_id,
                    "work_id": work_id,
                    "role": role,
                },
            )
