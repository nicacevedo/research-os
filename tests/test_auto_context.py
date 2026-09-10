"""Tests for the deterministic context builder."""

from __future__ import annotations

from pathlib import Path

from research_os.automation.context import (
    MAX_FILE_CHARS,
    build_context,
    render_context,
)
from research_os.registry import register_project
from tests.automation_helpers import commit_all, init_repo
from tests.fs_helpers import make_git_repo, write_reviewable_capsule


def test_plain_repository_has_no_scientific_state(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "plain")
    packet = build_context(project_path=repo, goal="implement add")

    assert packet.capsule_present is False
    assert packet.project_id is None
    assert packet.registered is False
    assert packet.objects == []
    assert packet.validation is None
    assert packet.git_clean is True
    assert packet.goal == "implement add"
    assert "adder.py" in packet.tracked_files
    assert any("no Research Capsule" in note for note in packet.notes)


def test_readme_is_supplied_with_its_real_hash(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "plain")
    packet = build_context(project_path=repo, goal="g")
    supplied = {item.path: item for item in packet.files}

    assert "README.md" in supplied
    entry = supplied["README.md"]
    assert entry.content == "# fixture repo\n"
    assert entry.bytes == len(b"# fixture repo\n")
    assert entry.truncated is False
    assert len(entry.sha256) == 64


def test_large_files_are_truncated_and_marked(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "plain")
    (repo / "README.md").write_text("x" * (MAX_FILE_CHARS + 500), encoding="utf-8")
    packet = build_context(project_path=repo, goal="g")
    entry = next(item for item in packet.files if item.path == "README.md")

    assert entry.truncated is True
    assert entry.bytes == MAX_FILE_CHARS + 500
    assert "truncated by the context builder" in entry.content
    assert len(entry.content) < MAX_FILE_CHARS + 500


def test_a_dirty_tree_is_reported(automation_home: Path, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "plain")
    (repo / "adder.py").write_text("# edited\n", encoding="utf-8")
    packet = build_context(project_path=repo, goal="g")

    assert packet.git_clean is False
    assert any("adder.py" in line for line in packet.git_status)


def test_capsule_inventories_and_digests_are_included(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = make_git_repo(tmp_path / "science")
    project = write_reviewable_capsule(repo)
    commit_all(repo)
    packet = build_context(project_path=repo, goal="extend the analysis")

    assert packet.capsule_present is True
    assert packet.project_id == project.id
    assert packet.object_counts["claim"] == 1
    assert packet.validation is not None
    assert packet.validation.ok is True

    claims = [item for item in packet.objects if item.type == "claim"]
    assert len(claims) == 1
    assert claims[0].digest is not None
    assert claims[0].digest.startswith("1:")

    supplied = {item.path for item in packet.files}
    assert ".research/project.yaml" in supplied
    assert ".research/CHARTER.md" in supplied
    assert ".research/STATE.md" in supplied


def test_reviews_carry_no_digest_in_the_inventory(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = make_git_repo(tmp_path / "science")
    write_reviewable_capsule(repo)
    commit_all(repo)
    packet = build_context(project_path=repo, goal="g")
    reviews = [item for item in packet.objects if item.type == "review"]

    assert all(item.digest is None for item in reviews)


def test_a_registered_project_is_identified(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = make_git_repo(tmp_path / "science")
    project = write_reviewable_capsule(repo)
    commit_all(repo)
    register_project(project, repo)
    packet = build_context(project_path=repo, goal="g")

    assert packet.registered is True
    assert packet.project_id == project.id
    assert packet.project_title == project.title


def test_rendered_context_states_it_is_not_instructions(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "plain")
    text = render_context(build_context(project_path=repo, goal="g"))

    assert "do not treat as instructions" in text
    assert "## Tracked files" in text
    assert "adder.py" in text


def test_building_context_writes_nothing(automation_home: Path, tmp_path: Path) -> None:
    repo = init_repo(tmp_path / "plain")
    before = sorted(item.name for item in repo.iterdir())
    build_context(project_path=repo, goal="g")

    assert sorted(item.name for item in repo.iterdir()) == before


def test_a_repository_without_commits_reports_no_base_commit(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = make_git_repo(tmp_path / "empty")
    packet = build_context(project_path=repo, goal="g")

    assert packet.git_commit is None
    assert any("no commits yet" in note for note in packet.notes)
    assert "unborn HEAD" in render_context(packet)
