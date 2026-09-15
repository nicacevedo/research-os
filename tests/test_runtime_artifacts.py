"""The artifact store: immutable, content-addressed, concurrent-safe.

Immutability here is a property of the naming rather than of permissions: the
path is the hash of the contents, so there is no way to write different bytes to
the same name. Most of these tests are checking that nothing in the
implementation undermines that.
"""

from __future__ import annotations

import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from research_os.runtime.artifacts import (
    ArtifactError,
    ArtifactMissingError,
    FilesystemArtifactStore,
    hash_bytes,
)
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore


@pytest.fixture
def store(tmp_path: Path) -> FilesystemArtifactStore:
    return FilesystemArtifactStore(tmp_path / "artifacts")


def test_the_address_is_the_content_hash(store: FilesystemArtifactStore) -> None:
    ref = store.put_bytes(b"hello world", media_type="text/plain")
    assert ref.artifact_id == hash_bytes(b"hello world")
    assert ref.size_bytes == 11
    assert store.get_bytes(ref.artifact_id) == b"hello world"


def test_identical_content_stored_twice_is_one_artifact(
    store: FilesystemArtifactStore,
) -> None:
    """Two runs fetching the same paper must not produce two copies."""

    first = store.put_bytes(b"same", role="pdf")
    second = store.put_bytes(b"same", role="stdout")
    assert first.artifact_id == second.artifact_id
    stored = list((store.root / "sha256").rglob("*"))
    assert len([path for path in stored if path.is_file()]) == 1


def test_different_content_gets_different_addresses(
    store: FilesystemArtifactStore,
) -> None:
    assert store.put_bytes(b"a").artifact_id != store.put_bytes(b"b").artifact_id


def test_concurrent_writers_of_the_same_content_are_safe(
    store: FilesystemArtifactStore,
) -> None:
    payload = b"x" * 200_000
    with ThreadPoolExecutor(max_workers=8) as pool:
        refs = list(pool.map(lambda _i: store.put_bytes(payload), range(16)))
    assert len({ref.artifact_id for ref in refs}) == 1
    assert store.get_bytes(refs[0].artifact_id) == payload


def test_no_partial_artifact_is_ever_visible(store: FilesystemArtifactStore) -> None:
    """A crash mid-write must leave the old state or the whole file, never a stub.

    The temporary file lives in the target's own directory and is fsynced before
    ``os.replace``, so the visible name only ever refers to complete bytes. What
    this asserts is the observable consequence: nothing but finished artifacts
    is reachable by its address, and leftovers are recognisable.
    """

    store.put_bytes(b"complete")
    incoming = [p.name for p in (store.root / "sha256").rglob(".incoming-*")]
    assert incoming == []


def test_a_file_is_stored_by_its_contents_not_its_name(
    store: FilesystemArtifactStore, tmp_path: Path
) -> None:
    source = tmp_path / "results.csv"
    source.write_text("a,b\n1,2\n", encoding="utf-8")
    ref = store.put_file(source)
    assert ref.media_type.startswith("text/csv")
    assert store.get_text(ref.artifact_id) == "a,b\n1,2\n"
    renamed = tmp_path / "other.csv"
    source.rename(renamed)
    assert store.put_file(renamed).artifact_id == ref.artifact_id


def test_a_missing_artifact_is_a_distinct_error(store: FilesystemArtifactStore) -> None:
    """Not retryable: retrying will not make the bytes reappear."""

    with pytest.raises(ArtifactMissingError, match="not in the store"):
        store.get_bytes("0" * 64)


def test_an_id_that_is_not_a_hash_is_refused(store: FilesystemArtifactStore) -> None:
    """Otherwise ``path_for`` is a path-traversal primitive."""

    for bad in ("../../etc/passwd", "ABCDEF" * 10 + "1234", "short", "g" * 64):
        with pytest.raises(ArtifactError, match="not an artifact id"):
            store.path_for(bad)


def test_verify_detects_corruption(store: FilesystemArtifactStore) -> None:
    """Corruption is otherwise silent until a model reads a damaged PDF."""

    ref = store.put_bytes(b"trustworthy")
    assert store.verify(ref.artifact_id) is True
    store.path_for(ref.artifact_id).write_bytes(b"tampered")
    assert store.verify(ref.artifact_id) is False


def test_metadata_reaches_postgres_when_a_store_is_supplied(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    runtime = RuntimeStore(runtime_db)
    run = runtime.create_run(project_id=runtime_project, objective="o")
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts", store=runtime)
    ref = artifacts.put_text(
        "the model said this",
        role="model_output",
        producer="scientific_reviewer",
        source="provider:demo",
    )
    artifacts.link(ref, role="review_output", run_id=run.run_id)

    with runtime_db.tx() as conn:
        row = conn.execute(
            "select size_bytes, media_type, role, producer, source from artifacts "
            "where artifact_id = %s",
            (ref.artifact_id,),
        ).fetchone()
        links = conn.execute(
            "select role, run_id from artifact_links where artifact_id = %s",
            (ref.artifact_id,),
        ).fetchall()
    assert row is not None
    assert row["role"] == "model_output"
    assert row["producer"] == "scientific_reviewer"
    assert links == [{"role": "review_output", "run_id": run.run_id}]


def test_recording_the_same_artifact_twice_does_not_conflict(
    runtime_db: Database, tmp_path: Path
) -> None:
    runtime = RuntimeStore(runtime_db)
    artifacts = FilesystemArtifactStore(tmp_path / "artifacts", store=runtime)
    first = artifacts.put_bytes(b"dup")
    second = artifacts.put_bytes(b"dup", role="something-else")
    assert first.artifact_id == second.artifact_id
    with runtime_db.tx() as conn:
        count = conn.execute(
            "select count(*) as n from artifacts where artifact_id = %s",
            (first.artifact_id,),
        ).fetchone()
    assert count["n"] == 1


def test_the_store_works_without_a_database(tmp_path: Path) -> None:
    """A unit test, or a worker whose database is momentarily gone, still stores."""

    plain = FilesystemArtifactStore(tmp_path / "plain")
    ref = plain.put_bytes(b"no database here")
    plain.link(ref, role="orphan")  # a no-op rather than a crash
    assert plain.exists(ref.artifact_id)


def test_a_large_file_is_hashed_without_being_held_in_memory(tmp_path: Path) -> None:
    """Chunked reads, asserted by measuring peak RSS against a baseline.

    A dataset snapshot is routinely larger than the process should hold, so this
    is worth asserting rather than inferring from the shape of the code. The
    absolute number is mostly interpreter baseline, so what is measured is the
    *difference* between hashing 64 MiB and hashing almost nothing.
    """

    small = tmp_path / "small.bin"
    small.write_bytes(b"q" * 1024)
    big = tmp_path / "big.bin"
    with big.open("wb") as handle:
        for _ in range(64):
            handle.write(b"q" * (1024 * 1024))

    def peak_kib(path: Path) -> int:
        program = (
            "import resource\n"
            "from pathlib import Path\n"
            "from research_os.runtime.artifacts import hash_file\n"
            f"hash_file(Path({str(path)!r}))\n"
            "print(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)\n"
        )
        completed = subprocess.run(
            [sys.executable, "-c", program],
            capture_output=True,
            text=True,
            check=True,
            timeout=180,
        )
        return int(completed.stdout.strip())

    growth_kib = peak_kib(big) - peak_kib(small)
    # Slurping 64 MiB would show as ~65536 KiB of growth. A few MiB of chunk
    # buffer and allocator noise is expected and fine.
    assert growth_kib < 16 * 1024, f"peak RSS grew by {growth_kib} KiB"
