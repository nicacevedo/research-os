"""Tests for symlink containment in the isolated worktree.

Git-level isolation is not filesystem-level isolation: a symlink committed in
the base tree points wherever it points, and a writer told to change that path
writes through it. These tests build real symlinks on disk rather than mocking
the filesystem, because the property under test is precisely what the
filesystem does with them.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_os.automation.filescope import (
    assert_contained_symlinks,
    outbound_symlinks,
)
from research_os.errors import SymlinkScopeError, WorktreeIsolationError


@pytest.fixture
def tree(tmp_path: Path) -> tuple[Path, Path]:
    """A worktree and a sibling directory that is outside it."""

    worktree = tmp_path / "worktree"
    outside = tmp_path / "outside"
    (worktree / "src").mkdir(parents=True)
    outside.mkdir()
    (outside / "secret.txt").write_text("canonical\n", encoding="utf-8")
    (worktree / "src" / "real.py").write_text("x = 1\n", encoding="utf-8")
    return worktree, outside


def test_a_tree_without_symlinks_is_contained(tree: tuple[Path, Path]) -> None:
    worktree, _ = tree
    assert outbound_symlinks(worktree) == ()
    assert_contained_symlinks(worktree)


def test_a_symlink_to_a_file_outside_the_worktree_is_reported(
    tree: tuple[Path, Path],
) -> None:
    worktree, outside = tree
    os.symlink(outside / "secret.txt", worktree / "src" / "notes.txt")

    assert outbound_symlinks(worktree) == ("src/notes.txt",)
    with pytest.raises(SymlinkScopeError, match="src/notes.txt"):
        assert_contained_symlinks(worktree)


def test_a_symlink_to_a_directory_outside_the_worktree_is_reported(
    tree: tuple[Path, Path],
) -> None:
    worktree, outside = tree
    os.symlink(outside, worktree / "vendor", target_is_directory=True)

    assert outbound_symlinks(worktree) == ("vendor",)


def test_a_symlink_inside_the_worktree_is_allowed(tree: tuple[Path, Path]) -> None:
    worktree, _ = tree
    os.symlink(worktree / "src" / "real.py", worktree / "src" / "alias.py")
    os.symlink(worktree / "src", worktree / "code", target_is_directory=True)

    assert outbound_symlinks(worktree) == ()
    assert_contained_symlinks(worktree)


def test_a_relative_symlink_that_climbs_out_is_reported(
    tree: tuple[Path, Path],
) -> None:
    """The link's text is irrelevant; where it lands is what matters."""

    worktree, _ = tree
    os.symlink(Path("../../outside/secret.txt"), worktree / "src" / "climb.txt")

    assert outbound_symlinks(worktree) == ("src/climb.txt",)


def test_a_dangling_symlink_out_of_the_worktree_is_reported(
    tree: tuple[Path, Path],
) -> None:
    """A link is judged by its target, not by whether that target exists yet."""

    worktree, outside = tree
    os.symlink(outside / "not-created-yet.txt", worktree / "src" / "later.txt")

    assert outbound_symlinks(worktree) == ("src/later.txt",)


def test_a_symlink_loop_out_of_the_worktree_is_reported(
    tree: tuple[Path, Path],
) -> None:
    worktree, _ = tree
    os.symlink(worktree / "src" / "b", worktree / "src" / "a")
    os.symlink(worktree / "src" / "a", worktree / "src" / "b")

    assert outbound_symlinks(worktree) == ("src/a", "src/b")


def test_the_scan_does_not_descend_through_a_symlinked_directory(
    tree: tuple[Path, Path],
) -> None:
    """A link to '/' must cost one lstat, not a walk of the whole filesystem."""

    worktree, _ = tree
    os.symlink(Path("/"), worktree / "root", target_is_directory=True)

    assert outbound_symlinks(worktree) == ("root",)


def test_nested_symlinks_are_all_reported(tree: tuple[Path, Path]) -> None:
    worktree, outside = tree
    (worktree / "src" / "deep" / "deeper").mkdir(parents=True)
    os.symlink(outside / "secret.txt", worktree / "src" / "deep" / "one.txt")
    os.symlink(outside / "secret.txt", worktree / "src" / "deep" / "deeper" / "two.txt")

    assert outbound_symlinks(worktree) == (
        "src/deep/deeper/two.txt",
        "src/deep/one.txt",
    )


def test_a_missing_worktree_is_a_scope_failure_not_a_silent_pass(
    tmp_path: Path,
) -> None:
    with pytest.raises(SymlinkScopeError):
        outbound_symlinks(tmp_path / "does-not-exist")


def test_the_failure_is_an_isolation_failure(tree: tuple[Path, Path]) -> None:
    """Callers that already handle isolation failures handle this one too."""

    assert issubclass(SymlinkScopeError, WorktreeIsolationError)
