"""Tests for the noncanonical global project registry."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from research_os.capsule import init_project, load_project_identity, validate_project
from research_os.errors import RegistryConflictError, RegistryError
from research_os.registry import (
    SCHEMA_VERSION,
    legacy_registry_path,
    list_projects,
    register_project,
    registry_path,
)
from tests.fs_helpers import make_git_repo, snapshot_files, write_minimal_capsule


def test_registry_created_on_init(
    tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os.cli import main

    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.argv", ["researchctl", "init-project", str(repo)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code in {0, None}
    assert registry_path().is_file()
    entries = list_projects()
    assert len(entries) == 1
    assert entries[0].project_id == "sample-project"
    assert entries[0].path == str(repo.resolve())


def test_same_id_same_path_is_idempotent(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    first = register_project(project, git_root)
    second = register_project(project, git_root)
    assert first.project_id == second.project_id
    assert first.path == second.path
    assert len(list_projects()) == 1


def test_duplicate_id_live_different_path_errors(
    tmp_path: Path, data_home: Path
) -> None:
    first = make_git_repo(tmp_path / "first-project")
    second = make_git_repo(tmp_path / "second-project")
    git_a, project_a = init_project(first, project_id="shared-id")
    register_project(project_a, git_a)
    git_b, project_b = init_project(second, project_id="shared-id")
    with pytest.raises(RegistryConflictError):
        register_project(project_b, git_b)
    assert (second / ".research" / "project.yaml").is_file()


def test_same_path_different_id_errors(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    register_project(project, git_root)
    project_file = repo / ".research" / "project.yaml"
    data = yaml.safe_load(project_file.read_text(encoding="utf-8"))
    data["id"] = "other-id"
    project_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    git_root, changed = load_project_identity(repo)
    with pytest.raises(RegistryConflictError):
        register_project(changed, git_root)


def test_stale_missing_path_can_be_reregistered(
    tmp_path: Path, data_home: Path
) -> None:
    original = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(original)
    register_project(project, git_root)
    moved = tmp_path / "moved-project"
    original.rename(moved)
    git_root, project = load_project_identity(moved)
    entry = register_project(project, git_root)
    assert entry.path == str(moved.resolve())
    assert len(list_projects()) == 1


def test_projects_marks_missing_paths(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    register_project(project, git_root)
    snapshot_path = str(repo.resolve())
    import shutil

    shutil.rmtree(repo)
    entries = list_projects()
    assert len(entries) == 1
    assert entries[0].availability == "MISSING"
    assert entries[0].path == snapshot_path


def test_projects_missing_registry_is_empty_and_does_not_create_db(
    tmp_path: Path, data_home: Path
) -> None:
    assert list_projects() == ()
    assert not registry_path().exists()


def test_projects_does_not_update_last_seen(tmp_path: Path, data_home: Path) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    first = register_project(project, git_root)
    mtime = registry_path().stat().st_mtime_ns
    listed = list_projects()
    assert listed[0].last_seen == first.last_seen
    assert registry_path().stat().st_mtime_ns == mtime


def test_validate_and_status_do_not_update_last_seen(
    tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os.cli import main

    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    entry = register_project(project, git_root)
    mtime = registry_path().stat().st_mtime_ns

    monkeypatch.setattr("sys.argv", ["researchctl", "validate-project", str(repo)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code in {0, None}

    monkeypatch.setattr("sys.argv", ["researchctl", "status", str(repo)])
    with pytest.raises(SystemExit) as exc:
        main()
    assert exc.value.code in {0, None}

    listed = list_projects()
    assert listed[0].last_seen == entry.last_seen
    assert registry_path().stat().st_mtime_ns == mtime
    assert git_root == repo.resolve()


def test_deleting_registry_does_not_alter_project_files(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    register_project(project, git_root)
    before = snapshot_files(repo)
    registry_path().unlink()
    after = snapshot_files(repo)
    assert after == before
    assert not registry_path().exists()
    report = validate_project(repo)
    assert report.ok


def test_register_project_does_not_modify_yaml_or_markdown(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    before = snapshot_files(repo / ".research")
    git_root, project = load_project_identity(repo)
    register_project(project, git_root)
    after = snapshot_files(repo / ".research")
    assert after == before


def test_idempotent_register_updates_title_and_status(
    tmp_path: Path, data_home: Path
) -> None:
    repo = make_git_repo(tmp_path / "sample-project")
    git_root, project = init_project(repo)
    register_project(project, git_root)
    project_file = repo / ".research" / "project.yaml"
    data = yaml.safe_load(project_file.read_text(encoding="utf-8"))
    data["title"] = "Renamed"
    data["status"] = "paused"
    project_file.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    git_root, changed = load_project_identity(repo)
    entry = register_project(changed, git_root)
    assert entry.title == "Renamed"
    assert entry.status == "paused"
    assert len(list_projects()) == 1


# --- JSON store shape -----------------------------------------------------


def _register(tmp_path: Path, name: str) -> None:
    repo = make_git_repo(tmp_path / name)
    _, project = init_project(repo)
    register_project(project, repo)


def test_registry_file_is_json_and_sorted(tmp_path: Path, data_home: Path) -> None:
    import json

    _register(tmp_path, "zebra-project")
    _register(tmp_path, "alpha-project")
    store = json.loads(registry_path().read_text(encoding="utf-8"))
    assert store["schema_version"] == SCHEMA_VERSION
    assert [entry["project_id"] for entry in store["projects"]] == [
        "alpha-project",
        "zebra-project",
    ]
    assert set(store["projects"][0]) == {
        "project_id",
        "path",
        "title",
        "capsule_version",
        "status",
        "last_seen",
    }
    assert registry_path().read_text(encoding="utf-8").endswith("\n")


def test_serialization_is_deterministic_for_a_given_store() -> None:
    """The serializer is a deterministic function of its input.

    Deliberately not "register twice and diff the file": last_seen advances on
    every explicit registration by design, so that would only hold under a
    frozen clock. Determinism belongs to the serializer, freshness to last_seen.
    """

    from research_os.registry import _serialize

    rows = [
        {
            "project_id": "zebra-project",
            "path": "/tmp/zebra",
            "title": "Zebra",
            "capsule_version": 1,
            "status": "active",
            "last_seen": "2026-09-08T21:18:03Z",
        },
        {
            "project_id": "alpha-project",
            "path": "/tmp/alpha",
            "title": "Alpha",
            "capsule_version": 1,
            "status": "paused",
            "last_seen": "2026-09-08T21:18:04Z",
        },
    ]
    assert _serialize(rows) == _serialize(list(reversed(rows)))


def test_re_register_advances_last_seen(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """last_seen records the most recent explicit registration."""

    repo = make_git_repo(tmp_path / "sample-project")
    _, project = init_project(repo)
    first = register_project(project, repo)
    monkeypatch.setattr(
        "research_os.registry._utc_now",
        lambda: "2099-01-01T00:00:00Z",
    )
    second = register_project(project, repo)
    assert second.last_seen == "2099-01-01T00:00:00Z"
    assert second.last_seen != first.last_seen
    assert len(list_projects()) == 1


# --- structural validation ------------------------------------------------
#
# SQLite enforced shape through its schema; JSON enforces nothing, so every
# malformed store must surface as RegistryError rather than as a KeyError or
# TypeError from deep inside a caller.

_GOOD_ENTRY = {
    "project_id": "sample-project",
    "path": "/tmp/sample",
    "title": "Sample",
    "capsule_version": 1,
    "status": "active",
    "last_seen": "2026-09-08T21:18:03Z",
}


def _store(**overrides: object) -> str:
    import json

    store = {"schema_version": SCHEMA_VERSION, "projects": [dict(_GOOD_ENTRY)]}
    store.update(overrides)
    return json.dumps(store)


def _entry_missing(field: str) -> str:
    import json

    entry = dict(_GOOD_ENTRY)
    del entry[field]
    return json.dumps({"schema_version": SCHEMA_VERSION, "projects": [entry]})


def _entry_with(field: str, value: object) -> str:
    import json

    entry = dict(_GOOD_ENTRY)
    entry[field] = value
    return json.dumps({"schema_version": SCHEMA_VERSION, "projects": [entry]})


MALFORMED = {
    "not json": b"{not json at all",
    "not utf8": b'{"schema_version": 1, "projects": [], "x": "caf\xe9"}',
    "top level list": b"[]",
    "top level string": b'"nope"',
    "missing schema_version": b'{"projects": []}',
    "schema_version not int": b'{"schema_version": "1", "projects": []}',
    "missing projects": b'{"schema_version": 1}',
    "projects not list": b'{"schema_version": 1, "projects": {}}',
    "entry not object": _store(projects=["nope"]).encode(),
    "entry missing path": _entry_missing("path").encode(),
    "entry missing capsule_version": _entry_missing("capsule_version").encode(),
    "capsule_version is str": _entry_with("capsule_version", "1").encode(),
    "capsule_version is bool": _entry_with("capsule_version", True).encode(),
    "title is int": _entry_with("title", 7).encode(),
}


@pytest.mark.parametrize(("label", "payload"), sorted(MALFORMED.items()))
def test_malformed_registry_raises_registry_error(
    label: str,
    payload: bytes,
    data_home: Path,
) -> None:
    registry_path().write_bytes(payload)
    with pytest.raises(RegistryError) as exc:
        list_projects()
    assert type(exc.value) is RegistryError, label
    assert str(registry_path()) in str(exc.value) or "schema version" in str(exc.value)


def test_duplicate_project_id_in_store_is_rejected(data_home: Path) -> None:
    """SQLite gave this for free via PRIMARY KEY.

    register_project decides identity conflicts from these keys, so a store
    holding two rows for one id has no defined meaning.
    """

    import json

    second = dict(_GOOD_ENTRY, path="/tmp/other")
    registry_path().write_text(
        json.dumps({"schema_version": 1, "projects": [dict(_GOOD_ENTRY), second]}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate project id sample-project"):
        list_projects()


def test_duplicate_path_in_store_is_rejected(data_home: Path) -> None:
    """SQLite gave this for free via a unique index on path."""

    import json

    second = dict(_GOOD_ENTRY, project_id="other-project")
    registry_path().write_text(
        json.dumps({"schema_version": 1, "projects": [dict(_GOOD_ENTRY), second]}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="duplicate path /tmp/sample"):
        list_projects()


def test_unsupported_registry_schema_version_raises(data_home: Path) -> None:
    import json

    registry_path().write_text(
        json.dumps({"schema_version": 2, "projects": []}),
        encoding="utf-8",
    )
    with pytest.raises(RegistryError, match="unsupported project registry schema"):
        list_projects()


def test_malformed_registry_also_blocks_writes(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """A corrupt store must not be silently overwritten by the next register."""

    repo = make_git_repo(tmp_path / "sample-project")
    _, project = init_project(repo)
    registry_path().write_bytes(b"{not json")
    with pytest.raises(RegistryError):
        register_project(project, repo)


# --- legacy SQLite store --------------------------------------------------


LEGACY_BYTES = b"SQLite format 3\x00 not really"


def _write_legacy() -> Path:
    """Put a legacy SQLite store in place without teaching tests to read one."""

    legacy = legacy_registry_path()
    legacy.write_bytes(LEGACY_BYTES)
    return legacy


def test_legacy_sqlite_registry_is_reported_by_projects(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """Enumeration reports it: never read, import, delete, or silently ignore it.

    Starting from an empty JSON registry would make researchctl projects look
    as though the projects had vanished, which is why this one operation refuses
    rather than answering.
    """

    legacy = _write_legacy()

    with pytest.raises(RegistryError, match="legacy SQLite project registry") as exc:
        list_projects()
    assert "register-project" in str(exc.value)
    assert str(legacy) in str(exc.value)

    assert legacy.is_file()
    assert legacy.read_bytes() == LEGACY_BYTES
    assert not registry_path().exists()


def test_legacy_sqlite_does_not_block_register_project(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """The prescribed remediation must actually be performable.

    The report tells the researcher to re-register and then delete the old file.
    While registration was guarded by the same check, that first step was
    impossible and the project stayed unregistered forever.
    """

    legacy = _write_legacy()
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    _, project = load_project_identity(repo)

    entry = register_project(project, repo)

    assert entry.project_id == "sample-project"
    assert entry.path == str(repo.resolve())
    assert registry_path().is_file()
    assert [item.project_id for item in list_projects()] == ["sample-project"]
    assert legacy.read_bytes() == LEGACY_BYTES


def test_legacy_sqlite_does_not_block_init_project(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """init-project registers what it just created, legacy file or not."""

    from research_os.cli import main

    legacy = _write_legacy()
    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.argv", ["researchctl", "init-project", str(repo)])

    with pytest.raises(SystemExit) as exc:
        main()

    assert exc.value.code == 0
    assert [item.project_id for item in list_projects()] == ["sample-project"]
    assert legacy.read_bytes() == LEGACY_BYTES


def test_init_project_notes_the_leftover_legacy_file(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Registration silences the report forever, so say the file is still there.

    Advisory only: it goes to stderr and does not change the exit code.
    """

    from research_os.cli import main

    legacy = _write_legacy()
    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.argv", ["researchctl", "init-project", str(repo)])
    with pytest.raises(SystemExit) as exc:
        main()

    captured = capsys.readouterr()
    assert exc.value.code == 0
    assert str(legacy) in captured.err
    assert "can be deleted" in captured.err
    assert "Initialized project sample-project" in captured.out


def test_no_legacy_note_when_no_legacy_file(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from research_os.cli import main

    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.argv", ["researchctl", "init-project", str(repo)])
    with pytest.raises(SystemExit):
        main()

    assert capsys.readouterr().err == ""


def test_first_json_creation_over_legacy_is_well_formed(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """The store written over a legacy file is an ordinary store, not a variant."""

    _write_legacy()
    repo = make_git_repo(tmp_path / "sample-project")
    write_minimal_capsule(repo)
    _, project = load_project_identity(repo)
    register_project(project, repo)

    text = registry_path().read_text(encoding="utf-8")
    store = json.loads(text)
    assert store["schema_version"] == SCHEMA_VERSION
    assert [entry["project_id"] for entry in store["projects"]] == ["sample-project"]
    assert set(store["projects"][0]) == {
        "project_id",
        "path",
        "title",
        "capsule_version",
        "status",
        "last_seen",
    }
    assert text.endswith("\n")


def test_subsequent_registrations_with_legacy_present(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """Once JSON exists the legacy file is inert, and further projects register."""

    legacy = _write_legacy()
    first = make_git_repo(tmp_path / "alpha-project")
    second = make_git_repo(tmp_path / "beta-project")
    for repo in (first, second):
        write_minimal_capsule(repo)
        _, project = load_project_identity(repo)
        register_project(project, repo)

    assert [entry.project_id for entry in list_projects()] == [
        "alpha-project",
        "beta-project",
    ]
    assert legacy.read_bytes() == LEGACY_BYTES


def test_legacy_file_is_never_touched(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Byte-identical across every registry operation, including the failing one."""

    from research_os.cli import main

    legacy = _write_legacy()
    digest = hashlib.sha256(legacy.read_bytes()).hexdigest()
    stat_before = legacy.stat()

    with pytest.raises(RegistryError):
        list_projects()

    repo = make_git_repo(tmp_path / "sample-project")
    monkeypatch.setattr("sys.argv", ["researchctl", "init-project", str(repo)])
    with pytest.raises(SystemExit):
        main()
    list_projects()

    other = make_git_repo(tmp_path / "beta-project")
    write_minimal_capsule(other)
    _, project = load_project_identity(other)
    register_project(project, other)

    assert hashlib.sha256(legacy.read_bytes()).hexdigest() == digest
    assert legacy.stat().st_mtime_ns == stat_before.st_mtime_ns
    assert legacy.stat().st_size == stat_before.st_size


def test_legacy_sqlite_is_ignored_once_json_exists(
    tmp_path: Path,
    data_home: Path,
) -> None:
    """The check is self-clearing: it only fires while no JSON store exists."""

    repo = make_git_repo(tmp_path / "sample-project")
    _, project = init_project(repo)
    register_project(project, repo)
    legacy_registry_path().write_bytes(b"SQLite format 3\x00 not really")
    assert [entry.project_id for entry in list_projects()] == ["sample-project"]
    assert register_project(project, repo).project_id == "sample-project"


def test_no_sqlite_dependency_remains() -> None:
    """The kernel no longer uses SQLite in any form."""

    import research_os

    root = Path(research_os.__file__).parent
    offenders = [
        path.name
        for path in sorted(root.glob("*.py"))
        if "import sqlite3" in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert not any(
        "sqlite3" in path.read_text(encoding="utf-8")
        for path in sorted(root.glob("*.py"))
    )
