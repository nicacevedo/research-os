"""Tests for automatic Git worktree isolation and its exclusive lock."""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os.automation.gitutil import branch_exists, git
from research_os.automation.store import locks_root, worktrees_root
from research_os.automation.worktree import (
    assert_isolated,
    branch_name,
    create_worktree,
    lock_path,
    release_worktree,
    worktree_path,
)
from research_os.errors import WorktreeError, WorktreeIsolationError
from tests.automation_helpers import head, init_repo

RUN_ID = "RUN-20260909T101500Z-0a1b2c3d"


def test_names_are_deterministic() -> None:
    assert branch_name(RUN_ID, "T-001") == f"automation/{RUN_ID.lower()}/t-001"


def test_a_worktree_is_created_outside_the_project(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID,
        task_id="T-001",
        repository=repo,
        base_commit=head(repo),
    )

    target = Path(record.path)
    assert target.is_dir()
    assert target == worktree_path(RUN_ID, "T-001")
    assert worktrees_root() in target.parents
    assert repo not in target.parents
    assert record.branch == branch_name(RUN_ID, "T-001")
    assert branch_exists(repo, record.branch)
    assert (target / "adder.py").is_file()
    assert Path(record.lock_path).is_file()
    assert Path(record.lock_path).parent == locks_root()


def test_the_worktree_starts_at_the_recorded_base_commit(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    base = head(repo)
    (repo / "later.txt").write_text("added after the base\n", encoding="utf-8")
    git(["add", "-A"], cwd=repo)
    git(
        ["-c", "user.email=t@e.invalid", "-c", "user.name=t", "commit", "-m", "later"],
        cwd=repo,
    )

    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=base
    )

    assert head(Path(record.path)) == base
    assert not (Path(record.path) / "later.txt").exists()


def test_a_second_worktree_for_the_same_task_is_refused(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(
            run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
        )


def test_a_held_lock_refuses_a_second_concurrent_worker(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    target = worktree_path(RUN_ID, "T-001")
    lock = lock_path(target)
    lock.parent.mkdir(parents=True, exist_ok=True)
    lock.write_text('{"run_id": "RUN-other", "task_id": "T-009"}\n', encoding="utf-8")

    with pytest.raises(WorktreeError, match="already owned by run RUN-other"):
        create_worktree(
            run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
        )
    assert not target.exists()


def test_an_existing_branch_is_never_reused(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    git(["branch", branch_name(RUN_ID, "T-001")], cwd=repo)

    with pytest.raises(WorktreeError, match="already exists"):
        create_worktree(
            run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
        )


def test_isolation_rejects_the_canonical_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    impostor = record.model_copy(update={"path": str(repo)})

    with pytest.raises(WorktreeIsolationError, match="canonical worktree"):
        assert_isolated(impostor, canonical_repository=repo)


def test_isolation_rejects_a_path_inside_the_project(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    inside = repo / "nested"
    inside.mkdir()
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    impostor = record.model_copy(update={"path": str(inside)})

    with pytest.raises(WorktreeIsolationError, match="must live outside"):
        assert_isolated(impostor, canonical_repository=repo)


def test_isolation_rejects_a_missing_lock(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    Path(record.lock_path).unlink()

    with pytest.raises(WorktreeIsolationError, match="lock"):
        assert_isolated(record, canonical_repository=repo)


def test_isolation_rejects_a_lock_for_another_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    Path(record.lock_path).write_text('{"path": "/somewhere/else"}\n', encoding="utf-8")

    with pytest.raises(WorktreeIsolationError, match="belongs to"):
        assert_isolated(record, canonical_repository=repo)


def test_isolation_rejects_a_removed_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    release_worktree(record, repository=repo)

    with pytest.raises(WorktreeIsolationError, match="does not exist"):
        assert_isolated(record, canonical_repository=repo)


def test_release_removes_the_checkout_and_keeps_the_branch(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    released = release_worktree(record, repository=repo)

    assert not Path(record.path).exists()
    assert not Path(record.lock_path).exists()
    assert branch_exists(repo, record.branch)
    assert released.removed_at is not None


def test_a_released_lock_can_be_taken_again(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    release_worktree(record, repository=repo)
    git(["branch", "-D", record.branch], cwd=repo)

    again = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    assert Path(again.path).is_dir()


def test_two_tasks_get_two_separate_worktrees(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    first = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    second = create_worktree(
        run_id=RUN_ID, task_id="T-002", repository=repo, base_commit=head(repo)
    )

    assert first.path != second.path
    assert first.branch != second.branch
    assert first.lock_path != second.lock_path
    assert_isolated(first, canonical_repository=repo)
    assert_isolated(second, canonical_repository=repo)


def test_writing_in_a_worktree_leaves_the_project_untouched(
    automation_home: Path, tmp_path: Path
) -> None:
    repo = init_repo(tmp_path / "project")
    before = (repo / "adder.py").read_text(encoding="utf-8")
    record = create_worktree(
        run_id=RUN_ID, task_id="T-001", repository=repo, base_commit=head(repo)
    )
    (Path(record.path) / "adder.py").write_text("changed\n", encoding="utf-8")

    assert (repo / "adder.py").read_text(encoding="utf-8") == before
    assert git(["status", "--porcelain"], cwd=repo).stdout.strip() == ""
