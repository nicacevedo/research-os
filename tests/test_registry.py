"""Tests for the noncanonical global project registry."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_os.capsule import init_project, load_project_identity, validate_project
from research_os.errors import RegistryConflictError
from research_os.registry import list_projects, register_project, registry_path
from tests.fs_helpers import make_git_repo, snapshot_files, write_minimal_capsule


def test_registry_created_on_init(tmp_path: Path, data_home: Path, monkeypatch: pytest.MonkeyPatch) -> None:
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


def test_duplicate_id_live_different_path_errors(tmp_path: Path, data_home: Path) -> None:
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


def test_stale_missing_path_can_be_reregistered(tmp_path: Path, data_home: Path) -> None:
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
