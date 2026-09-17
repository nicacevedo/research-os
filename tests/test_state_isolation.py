"""The test suite must not be able to reach the researcher's real state.

The invariant, stated once: during this suite, no Research OS-owned mutable
path resolves outside pytest's temporary root, and nothing is written into
``~/.config/research-os``, ``~/.local/share/research-os``,
``~/.cache/research-os`` or ``~/.local/state/research-os``.

These tests check that behaviourally -- by calling the product's own path
functions and by writing real records through the real stores -- rather than by
asserting that some environment variable is set. An environment variable being
set says nothing about whether the code reads it.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from research_os import paths
from tests.conftest import (
    STATE_ENV_VARS,
    _assert_state_is_isolated,
    _real_state_inventory,
    resolve_path_entry_points,
)


def _basetemp(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return tmp_path_factory.getbasetemp().resolve()


def test_every_writable_path_resolves_inside_the_pytest_root(
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The whole inventory, not a sample: 25 entry points across 9 families."""

    basetemp = _basetemp(tmp_path_factory)
    resolved = resolve_path_entry_points()
    assert len(resolved) >= 25, "the inventory lost entries; a family may be unguarded"
    escaped = {
        entry: path
        for entry, path in resolved.items()
        if not (path.resolve() == basetemp or basetemp in path.resolve().parents)
    }
    assert escaped == {}


def test_the_inventory_covers_every_module_that_derives_a_writable_path() -> None:
    """A new path family must be added to the guard, not silently skipped.

    Greps the product for the four root functions and requires each importing
    module to appear in the guard's inventory. Without this, the guard would
    keep passing while a new store wrote wherever it liked.
    """

    source_root = Path(paths.__file__).parent
    roots = ("state_home", "data_home", "cache_home", "config_home")
    importers: set[str] = set()
    for module in sorted(source_root.rglob("*.py")):
        if module.name == "paths.py":
            continue
        text = module.read_text(encoding="utf-8")
        if not any(f"{root}()" in text for root in roots):
            continue
        relative = module.relative_to(source_root).with_suffix("")
        importers.add("research_os." + ".".join(relative.parts))

    covered = {entry.partition(":")[0] for entry in resolve_path_entry_points()}
    # ``diagnostics`` only reports the four roots, and the controllers derive
    # nothing of their own -- they call into the stores already covered here.
    exempt = {
        "research_os.diagnostics",
        "research_os.assessment.controller",
        "research_os.proposal.controller",
    }
    assert importers - covered - exempt == set()


@pytest.mark.parametrize("env_name", STATE_ENV_VARS)
def test_each_root_is_redirected_by_default(env_name: str) -> None:
    """No opt-in fixture: this test requests nothing and is still isolated."""

    value = os.environ.get(env_name, "")
    assert value, f"{env_name} is unset, so the resolver would fall back to real HOME"
    assert Path(value).is_dir()


def test_writing_through_the_real_stores_touches_nothing_real(tmp_path: Path) -> None:
    """The strongest form: write records, then look at the real directories.

    Exercises the two families that actually accumulated contamination, through
    the same store APIs the product uses, and checks the researcher's real
    directories directly rather than trusting where the paths pointed.
    """

    from research_os.assessment import store as assessment_store
    from research_os.research import store as research_store

    before = _real_state_inventory()

    project = tmp_path / "project"
    project.mkdir()
    created_at = "2026-09-17T00:00:00Z"

    assessment_id = assessment_store.make_assessment_id(
        project_path=str(project), goal="isolation probe", created_at=created_at
    )
    assessment_dir = assessment_store.assessments_root() / assessment_id
    assessment_dir.mkdir(parents=True)
    (assessment_dir / "assessment.json").write_text("{}\n", encoding="utf-8")

    run_id = research_store.make_research_run_id(
        project_path=str(project), goal="isolation probe", created_at=created_at
    )
    run_dir = research_store.research_root() / run_id
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text("{}\n", encoding="utf-8")

    assert assessment_dir.is_dir() and run_dir.is_dir()
    assert _real_state_inventory() - before == set()


def test_the_guard_rejects_a_path_outside_the_pytest_root(
    tmp_path_factory: pytest.TempPathFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The guard is not vacuous, and the old default is what it rejects.

    Reproduces the defect exactly: unset the overrides, as the previous autouse
    fixture did, and the resolver returns the researcher's real directories.
    """

    # Scoped, so isolation is back before the call-phase hook checks it: this
    # is the one test in the suite that deliberately resolves a real path, and
    # the hook is right to fail if that is still true when the test returns.
    # Not ``monkeypatch.undo()``, which would revert the isolation itself.
    del monkeypatch
    with pytest.MonkeyPatch.context() as scoped:
        for name in STATE_ENV_VARS:
            scoped.delenv(name, raising=False)

        real_state = Path(os.path.expanduser("~")) / ".local" / "state" / "research-os"
        assert paths.state_home() == real_state

        with pytest.raises(
            AssertionError, match="resolved outside the pytest temporary root"
        ):
            _assert_state_is_isolated(_basetemp(tmp_path_factory))

    _assert_state_is_isolated(_basetemp(tmp_path_factory))


def test_default_resolution_can_still_be_tested_with_a_temporary_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path_factory: pytest.TempPathFactory,
) -> None:
    """The escape hatch for ``test_paths.py``, and it stays inside basetemp."""

    for name in STATE_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    for leaf in (".config", ".local", ".cache"):
        (tmp_path / leaf).mkdir(exist_ok=True)

    assert paths.state_home() == tmp_path / ".local" / "state" / "research-os"
    _assert_state_is_isolated(_basetemp(tmp_path_factory))


def test_the_real_state_inventory_sees_a_new_entry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The session-level guard is not vacuous either.

    Points ``$HOME`` at a temporary directory laid out like the real one, so
    the inventory can be checked against a file it is allowed to create.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    state = tmp_path / ".local" / "state" / "research-os" / "assessments"
    state.mkdir(parents=True)
    before = _real_state_inventory()
    (state / "TA-probe").mkdir()
    added = _real_state_inventory() - before
    assert added == {str(state / "TA-probe")}


def test_worktree_contents_are_not_inventoried(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A worktree is a repository checkout; its files are not contamination.

    Only the run-level entry is inventoried, which is what a new worktree adds.
    """

    monkeypatch.setenv("HOME", str(tmp_path))
    worktrees = tmp_path / ".local" / "state" / "research-os" / "worktrees"
    run = worktrees / "RUN-probe"
    run.mkdir(parents=True)
    before = _real_state_inventory()
    (run / "T-001").mkdir()
    (run / "T-001" / "deep.txt").write_text("x\n", encoding="utf-8")
    assert _real_state_inventory() - before == set()

    other = worktrees / "RUN-other"
    other.mkdir()
    assert _real_state_inventory() - before == {str(other)}


# --- the guard is wired, not just importable --------------------------------

pytest_plugins = ["pytester"]


def test_a_test_that_deisolates_itself_fails_the_run(pytester: pytest.Pytester) -> None:
    """End-to-end: run pytest on a test that reaches real state, and see it fail.

    The assertions above prove ``_assert_state_is_isolated`` rejects a real
    path. This proves the call-phase hook actually runs it, which is the part
    that would silently stop working if the hook were renamed, unregistered, or
    changed to a fixture finalizer that fires after ``monkeypatch`` undoes.
    """

    pytester.makeconftest(
        "import sys\n"
        f"sys.path.insert(0, {str(Path(__file__).parent.parent)!r})\n"
        "from tests.conftest import *  # noqa: F403\n"
    )
    pytester.makepyfile(
        """
        import os

        from research_os import paths


        def test_reaches_real_state():
            for name in (
                "RESEARCH_OS_CONFIG_HOME",
                "RESEARCH_OS_DATA_HOME",
                "RESEARCH_OS_CACHE_HOME",
                "RESEARCH_OS_STATE_HOME",
            ):
                os.environ.pop(name, None)
            # The test body itself passes: it only reads a path.
            assert paths.state_home().name == "research-os"
        """
    )
    result = pytester.runpytest_subprocess("-p", "no:cacheprovider")
    # The test body passed; the hook is what turned the run red, and it is
    # attributed to the call phase because that is where the hook wraps.
    result.assert_outcomes(passed=0, failed=1)
    assert result.ret != 0
    result.stdout.fnmatch_lines(["*resolved outside the pytest temporary root*"])
