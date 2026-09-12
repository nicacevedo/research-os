"""What a controller check is allowed to do to the project's dependency lock.

An acceptance command is an observation. The controller runs it to establish
whether a change is correct, and running it must not itself change the project.
``uv run`` breaks that on its own: it resolves dependencies before it runs
anything and writes ``uv.lock`` into the tree it is standing in, so a project
that commits a lock could have it rewritten by a check, and a project that
commits none could gain one that then shows up as the worker's doing.

Two behaviours are pinned here, in both directions:

* a lock the project already had comes out of the check byte-identical, and
  ``UV_FROZEN`` is what makes uv leave it alone rather than the controller
  putting it back afterwards;
* a lock the check itself created does not survive the check, and the run says
  in its ledger that dependencies were resolved rather than leaving an
  unexplained file behind.

Real Git, real uv, real pytest for everything that claims uv behaves a certain
way. Only the model providers are fake.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.checks import (
    check_environment,
    run_acceptance_command,
)
from research_os.automation.models import (
    AcceptanceCommand,
    AutomationRun,
    RunState,
    WorkOrderStatus,
)
from research_os.automation.uvlock import (
    LOCK_ABSENT,
    LOCK_REMOVED,
    LOCK_RESTORED,
    LOCK_RETAINED,
    LOCK_UNCHANGED,
    UV_FROZEN,
    UV_LOCK_FILENAME,
    UvLockGuard,
)
from tests.automation_helpers import fake_config, make_controller, review_payload
from tests.fake_providers import FakeProvider, ScriptedResponse

needs_uv = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed on this machine"
)

STUB = (
    '"""A deliberately incomplete module."""\n\n\n'
    "def add(left, right):\n    raise NotImplementedError\n"
)
FIXED = (
    '"""A deliberately incomplete module."""\n\n\n'
    "def add(left, right):\n    return left + right\n"
)
TESTS = """from adder import add


def test_add():
    assert add(2, 3) == 5
"""


def _pinned_pytest() -> str:
    """Return the pytest requirement already resolved for this repository."""

    import pytest as _pytest

    return f"pytest=={_pytest.__version__}"


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def uv_project(path: Path, *, module: str = STUB, commit_lock: bool = False) -> Path:
    """Create a committed Git project whose checks run through ``uv run``.

    ``commit_lock`` is the difference that matters here: a project that commits
    a resolved lock and one that does not are the two cases a controller check
    has to leave alone, and they need opposite handling.
    """

    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "uvlockfixture"\n'
        'version = "0.1.0"\n'
        'requires-python = ">=3.12"\n'
        "dependencies = []\n"
        "\n"
        "[dependency-groups]\n"
        f'dev = ["{_pinned_pytest()}"]\n'
        "\n"
        "[tool.uv]\n"
        "package = false\n",
        encoding="utf-8",
    )
    (path / ".gitignore").write_text(
        ".venv/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (path / "adder.py").write_text(module, encoding="utf-8")
    (path / "test_adder.py").write_text(TESTS, encoding="utf-8")
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    if commit_lock:
        subprocess.run(
            ["uv", "lock"], cwd=str(path), check=True, capture_output=True, text=True
        )
        assert (path / UV_LOCK_FILENAME).is_file()
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)
    return path


# -- the environment the controller hands uv ---------------------------------


def test_frozen_is_set_only_when_the_controller_asks_for_it() -> None:
    placed = Path("/runtime/uv/T-001")

    frozen = check_environment(
        ["uv", "run", "pytest"], uv_project_environment=placed, uv_frozen=True
    )
    thawed = check_environment(
        ["uv", "run", "pytest"], uv_project_environment=placed, uv_frozen=False
    )

    assert frozen is not None and frozen[UV_FROZEN] == "1"
    assert thawed is not None and UV_FROZEN not in thawed


def test_an_inherited_frozen_setting_is_removed_when_not_asked_for(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A researcher's shell must not decide a work order's lock behaviour.

    Left inherited this would be worse than cosmetic: a project that commits no
    lock cannot run under ``UV_FROZEN`` at all, so every check of it would fail
    with uv's "unable to find lockfile" error for a reason that has nothing to
    do with the change under test.
    """

    monkeypatch.setenv(UV_FROZEN, "1")

    environment = check_environment(
        ["uv", "run", "pytest"],
        uv_project_environment=Path("/runtime/uv/T-001"),
        uv_frozen=False,
    )

    assert environment is not None
    assert UV_FROZEN not in environment


def test_a_non_uv_command_is_still_left_alone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UV_FROZEN, "1")

    assert (
        check_environment(
            ["pytest", "-q"],
            uv_project_environment=Path("/runtime/uv/T-001"),
            uv_frozen=True,
        )
        is None
    )


# -- the guard, as a unit ----------------------------------------------------


def test_a_project_with_no_lock_may_not_be_frozen(tmp_path: Path) -> None:
    """uv refuses ``UV_FROZEN`` outright when there is no lock to freeze."""

    guard = UvLockGuard.observe(tmp_path)

    assert guard.frozen is False
    assert guard.present_before is False


def test_a_lock_the_checks_created_is_removed(tmp_path: Path) -> None:
    guard = UvLockGuard.observe(tmp_path)
    (tmp_path / UV_LOCK_FILENAME).write_text("version = 1\n", encoding="utf-8")

    outcome = guard.settle()

    assert outcome.action == LOCK_REMOVED
    assert outcome.notable
    assert not (tmp_path / UV_LOCK_FILENAME).exists()


def test_a_project_that_never_had_a_lock_reports_nothing(tmp_path: Path) -> None:
    outcome = UvLockGuard.observe(tmp_path).settle()

    assert outcome.action == LOCK_ABSENT
    assert not outcome.notable


def test_an_untouched_lock_is_left_exactly_as_it_was(tmp_path: Path) -> None:
    target = tmp_path / UV_LOCK_FILENAME
    target.write_text("version = 1\nrequires-python = '>=3.12'\n", encoding="utf-8")
    before = target.read_bytes()

    guard = UvLockGuard.observe(tmp_path)
    outcome = guard.settle()

    assert guard.frozen is True
    assert outcome.action == LOCK_UNCHANGED
    assert not outcome.notable
    assert target.read_bytes() == before


def test_a_rewritten_lock_is_put_back(tmp_path: Path) -> None:
    """Defence in depth behind ``UV_FROZEN``, not a substitute for it.

    If anything at all rewrote the project's pinned dependency set while the
    controller was merely checking a change, the bytes the controller found are
    re-established and the run says so.
    """

    target = tmp_path / UV_LOCK_FILENAME
    target.write_text("version = 1\n", encoding="utf-8")
    guard = UvLockGuard.observe(tmp_path)

    target.write_text("version = 1\n# resolved something else\n", encoding="utf-8")
    outcome = guard.settle()

    assert outcome.action == LOCK_RESTORED
    assert outcome.notable
    assert target.read_text(encoding="utf-8") == "version = 1\n"


def test_a_deleted_lock_is_put_back(tmp_path: Path) -> None:
    target = tmp_path / UV_LOCK_FILENAME
    target.write_text("version = 1\n", encoding="utf-8")
    guard = UvLockGuard.observe(tmp_path)

    target.unlink()
    outcome = guard.settle()

    assert outcome.action == LOCK_RESTORED
    assert target.read_text(encoding="utf-8") == "version = 1\n"


def test_a_symlinked_lock_is_never_written_through(tmp_path: Path) -> None:
    """Writing through it would be exactly the escape containment refuses."""

    outside = tmp_path / "outside"
    outside.mkdir()
    real = outside / "real.lock"
    real.write_text("version = 1\n", encoding="utf-8")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / UV_LOCK_FILENAME).symlink_to(real)

    guard = UvLockGuard.observe(worktree)
    outcome = guard.settle()

    assert guard.frozen is False
    assert outcome.action == LOCK_RETAINED
    assert (worktree / UV_LOCK_FILENAME).is_symlink()
    assert real.read_text(encoding="utf-8") == "version = 1\n"


# -- real uv ------------------------------------------------------------------


@needs_uv
def test_a_committed_lock_survives_a_real_uv_check_byte_for_byte(
    tmp_path: Path,
) -> None:
    project = uv_project(tmp_path / "project", module=FIXED, commit_lock=True)
    lock = project / UV_LOCK_FILENAME
    before = lock.read_bytes()
    guard = UvLockGuard.observe(project)

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=tmp_path / "runtime" / "uv" / "T-001",
        uv_frozen=guard.frozen,
    )

    assert guard.frozen is True
    assert result.exit_code == 0, result.error
    assert lock.read_bytes() == before
    assert guard.settle().action == LOCK_UNCHANGED


@needs_uv
def test_a_real_uv_check_writes_a_lock_into_a_lockless_project(
    tmp_path: Path,
) -> None:
    """The behaviour being corrected, pinned so the correction cannot regress."""

    project = uv_project(tmp_path / "project", module=FIXED)
    assert not (project / UV_LOCK_FILENAME).exists()

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=tmp_path / "runtime" / "uv" / "T-001",
        uv_frozen=False,
    )

    assert result.exit_code == 0, result.error
    assert (project / UV_LOCK_FILENAME).is_file(), "uv no longer writes a lock"


@needs_uv
def test_freezing_a_lockless_project_would_fail_every_check(tmp_path: Path) -> None:
    """Why ``frozen`` is conditional rather than always on.

    uv's own refusal, observed rather than assumed: asking for the frozen mode
    without a lock file is an error, so a controller that set it unconditionally
    would break every project that does not commit one.
    """

    project = uv_project(tmp_path / "project", module=FIXED)

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=tmp_path / "runtime" / "uv" / "T-001",
        uv_frozen=True,
    )

    assert result.exit_code != 0
    assert not (project / UV_LOCK_FILENAME).exists()


@needs_uv
def test_a_stale_committed_lock_is_not_re_resolved_by_a_check(tmp_path: Path) -> None:
    """A check reports; it does not update the project's dependency set."""

    project = uv_project(tmp_path / "project", module=FIXED, commit_lock=True)
    lock = project / UV_LOCK_FILENAME
    stale = lock.read_text(encoding="utf-8") + "\n# deliberately stale marker\n"
    lock.write_text(stale, encoding="utf-8")
    guard = UvLockGuard.observe(project)

    run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=tmp_path / "runtime" / "uv" / "T-001",
        uv_frozen=guard.frozen,
    )

    assert lock.read_text(encoding="utf-8") == stale
    assert guard.settle().action == LOCK_UNCHANGED


# -- through the controller ---------------------------------------------------


def uv_plan(argv: tuple[str, ...] = ("uv", "run", "pytest", "-q")) -> dict[str, Any]:
    return {
        "summary": "Implement add.",
        "tasks": [
            {
                "id": "T-001",
                "title": "Implement add",
                "goal": "Make add return the sum of its two arguments.",
                "role": "coder",
                "read_only": False,
                "allowed_paths": ["adder.py"],
                "forbidden_paths": ["test_adder.py"],
                "read_paths": [],
                "acceptance_commands": [
                    {"argv": list(argv), "description": "the supplied tests pass"}
                ],
                "expected_artifacts": ["adder.py"],
                "completion_condition": "pytest exits 0",
                "dependencies": [],
            }
        ],
    }


def _run(tmp_path: Path, *, commit_lock: bool) -> tuple[Any, Any, Path, AutomationRun]:
    from research_os.automation.models import Role

    repo = uv_project(tmp_path / "project", module=STUB, commit_lock=commit_lock)
    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=uv_plan())],
            str(Role.CODER): [
                ScriptedResponse(text="done", write_files={"adder.py": FIXED})
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")
    final = controller.execute(store)
    return controller, store, repo, final


@needs_uv
def test_a_run_leaves_no_controller_created_lock_in_the_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    """The whole point: a reviewer sees the change, not the controller's resolution."""

    _, store, _, final = _run(tmp_path, commit_lock=False)
    worktree = Path(final.order("T-001").worktree_path or "")

    assert final.state is RunState.READY_FOR_HUMAN
    assert final.order("T-001").status is WorkOrderStatus.REVIEWED
    assert not (worktree / UV_LOCK_FILENAME).exists()
    assert final.order("T-001").changed_paths == ["adder.py"]

    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(worktree),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert UV_LOCK_FILENAME not in status

    diff = store.path(*(final.order("T-001").diff_path or "").split("/")).read_text(
        encoding="utf-8"
    )
    assert UV_LOCK_FILENAME not in diff


@needs_uv
def test_the_removal_is_recorded_rather_than_silent(
    automation_home: Path, tmp_path: Path
) -> None:
    _, store, _, _ = _run(tmp_path, commit_lock=False)

    settled = [
        item for item in store.iter_events() if item["event"] == "uv_lock_settled"
    ]
    assert settled, "the controller resolved dependencies without saying so"
    assert all(item["action"] == LOCK_REMOVED for item in settled)
    assert all(item["task_id"] == "T-001" for item in settled)


@needs_uv
def test_a_committed_lock_comes_out_of_a_whole_run_unchanged(
    automation_home: Path, tmp_path: Path
) -> None:
    _, store, repo, final = _run(tmp_path, commit_lock=True)
    worktree = Path(final.order("T-001").worktree_path or "")

    assert final.state is RunState.READY_FOR_HUMAN
    assert (worktree / UV_LOCK_FILENAME).read_bytes() == (
        repo / UV_LOCK_FILENAME
    ).read_bytes()
    assert final.order("T-001").changed_paths == ["adder.py"]
    assert not [
        item for item in store.iter_events() if item["event"] == "uv_lock_settled"
    ], "an untouched lock should say nothing"

    frozen = {
        item.get("uv_frozen")
        for item in store.iter_events()
        if item["event"] == "command_executed" and "uv_frozen" in item
    }
    assert frozen == {True}, "the committed lock was not protected by UV_FROZEN"


@needs_uv
def test_a_lockless_project_runs_its_checks_unfrozen(
    automation_home: Path, tmp_path: Path
) -> None:
    _, store, _, _ = _run(tmp_path, commit_lock=False)

    frozen = {
        item.get("uv_frozen")
        for item in store.iter_events()
        if item["event"] == "command_executed" and "uv_frozen" in item
    }
    assert frozen == {False}


def test_a_plan_without_uv_never_touches_the_lock(
    automation_home: Path, tmp_path: Path
) -> None:
    """Nothing but a uv command resolves anything, so nothing else is guarded."""

    from research_os.automation.models import Role
    from tests.automation_helpers import (
        FIXED_MODULE,
        init_repo,
        plan_payload,
    )

    repo = init_repo(tmp_path / "bare")
    (repo / UV_LOCK_FILENAME).write_text("version = 1\n", encoding="utf-8")
    _git(["add", "-A"], repo)
    _git(["commit", "-m", "lock"], repo)

    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=plan_payload())],
            str(Role.CODER): [
                ScriptedResponse(text="done", write_files={"adder.py": FIXED_MODULE})
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")
    final = controller.execute(store)

    worktree = Path(final.order("T-001").worktree_path or "")
    assert (worktree / UV_LOCK_FILENAME).read_text(encoding="utf-8") == "version = 1\n"
    assert not [
        item for item in store.iter_events() if item["event"] == "uv_lock_settled"
    ]
    assert not [
        item
        for item in store.iter_events()
        if item["event"] == "command_executed" and "uv_frozen" in item
    ]


def test_the_lock_is_settled_even_when_a_check_raises(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A check sequence that blows up must not leave the project rewritten."""

    from research_os.automation import controller as controller_module

    repo = uv_project(tmp_path / "project", module=STUB)
    from research_os.automation.models import Role

    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=uv_plan())],
            str(Role.CODER): [
                ScriptedResponse(text="done", write_files={"adder.py": FIXED})
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")

    def explode(command: Any, **kwargs: Any) -> Any:
        Path(kwargs["cwd"] / UV_LOCK_FILENAME).write_text("version = 1\n", "utf-8")
        raise RuntimeError("the check harness failed")

    monkeypatch.setattr(controller_module, "run_acceptance_command", explode)

    with pytest.raises(RuntimeError, match="the check harness failed"):
        controller.execute(store)

    worktree = Path(store.load().order("T-001").worktree_path or "")
    assert not (worktree / UV_LOCK_FILENAME).exists()
    assert [item for item in store.iter_events() if item["event"] == "uv_lock_settled"]
