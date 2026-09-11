"""Where a ``uv run`` acceptance check materialises its project environment.

``uv run`` builds the project environment before it runs anything. Left to
itself it builds ``<project>/.venv``, and inside an isolated worktree that
directory holds ``bin/python`` symlinks pointing at the uv-managed interpreter
outside it. Those are real outbound symlinks. The containment gate that runs
immediately before a repair worker is invoked refuses them, so a work order
whose checks used the advertised ``uv run pytest`` form could run its first
attempt and then never be repairable - the controller's own check had made the
worktree uncontained.

The fix is placement, not exemption. The controller tells uv where to put the
environment, in runtime-owned state outside every worktree, so nothing outbound
is ever created inside one. No pathname is excused from the gate: these tests
check both halves, that the controller's environment lands outside and that a
genuine outbound symlink inside still fails the work order exactly as before.

Real Git, real uv, real pytest. Only the model providers are fake.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any, NamedTuple

import pytest

from research_os.automation.checks import (
    UV_PROJECT_ENVIRONMENT,
    check_environment,
    run_acceptance_command,
)
from research_os.automation.controller import (
    UV_ENVIRONMENT_PARTS,
    AutomationController,
    ready_for_human_blockers,
)
from research_os.automation.models import (
    AcceptanceCommand,
    AutomationRun,
    RunState,
    WorkOrderStatus,
)
from research_os.automation.store import RunStore, runs_root
from research_os.errors import AutomationError
from tests.automation_helpers import fake_config, make_controller
from tests.fake_providers import FakeProvider, ScriptedResponse

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed on this machine"
)

STUB = (
    '"""A deliberately incomplete module."""\n\n\n'
    "def add(left, right):\n    raise NotImplementedError\n"
)
WRONG = (
    '"""A deliberately incomplete module."""\n\n\ndef add(left, right):\n    return 0\n'
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
    """Return the pytest requirement already resolved for this repository.

    Pinning to the version this suite is running under keeps the fixture
    project's resolution a uv cache hit, so these tests exercise real uv
    without depending on what the index happens to publish today.
    """

    import pytest as _pytest

    return f"pytest=={_pytest.__version__}"


def uv_project(path: Path, *, module: str = STUB) -> Path:
    """Create a committed Git project whose checks run through ``uv run``."""

    path.mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "uvfixture"\n'
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
    # uv writes a lock file beside the project. It is ignored rather than
    # committed so the fixture does not need a resolution baked into the test,
    # and an ignored file is not a changed path.
    (path / ".gitignore").write_text(
        ".venv/\nuv.lock\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (path / "adder.py").write_text(module, encoding="utf-8")
    (path / "test_adder.py").write_text(TESTS, encoding="utf-8")
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)
    return path


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


# -- A. the environment lands outside the worktree ---------------------------


def test_a_uv_check_creates_no_environment_inside_the_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    """The actual subprocess, the actual uv, the actual directory it makes."""

    project = uv_project(tmp_path / "project", module=FIXED)
    environment = tmp_path / "runtime" / "uv" / "T-001"

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=environment,
    )

    assert result.exit_code == 0, result.error
    assert not (project / ".venv").exists(), "uv built an environment in the worktree"
    assert environment.is_dir(), "the controller-owned environment was not created"
    assert (environment / "bin" / "python").exists()


def test_a_uv_check_without_a_placed_environment_still_builds_one_locally(
    automation_home: Path, tmp_path: Path
) -> None:
    """The behaviour being corrected, pinned so the correction cannot regress.

    Without a placed environment uv does what it has always done, and what it
    does is exactly the thing the containment gate refuses: an outbound symlink
    inside the project.
    """

    project = uv_project(tmp_path / "project", module=FIXED)

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=None,
    )

    assert result.exit_code == 0, result.error
    interpreter = project / ".venv" / "bin" / "python"
    assert interpreter.is_symlink()
    assert project.resolve() not in interpreter.resolve().parents


# -- F/G. which commands are touched, and whose value wins -------------------


def test_only_a_uv_command_gets_a_placed_environment() -> None:
    """A bare pytest or ruff runs in the environment it always ran in."""

    placed = Path("/runtime/uv/T-001")

    assert check_environment(["uv", "run", "pytest"], uv_project_environment=placed)
    assert check_environment(["pytest", "-q"], uv_project_environment=placed) is None
    assert (
        check_environment(["ruff", "check", "."], uv_project_environment=placed) is None
    )
    assert (
        check_environment(
            ["ruff", "format", "--check", "."], uv_project_environment=placed
        )
        is None
    )
    assert (
        check_environment(["uv", "run", "pytest"], uv_project_environment=None) is None
    )


def test_an_inherited_environment_setting_is_overridden(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A researcher's own shell setting must not place a work order's environment."""

    monkeypatch.setenv(UV_PROJECT_ENVIRONMENT, "/home/someone/elsewhere/.venv")
    placed = Path("/runtime/uv/T-001")

    environment = check_environment(
        ["uv", "run", "pytest"], uv_project_environment=placed
    )

    assert environment is not None
    assert environment[UV_PROJECT_ENVIRONMENT] == str(placed)


def test_an_inherited_setting_is_left_alone_for_a_non_uv_command(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(UV_PROJECT_ENVIRONMENT, "/home/someone/elsewhere/.venv")

    assert (
        check_environment(
            ["pytest", "-q"], uv_project_environment=Path("/runtime/uv/T")
        )
        is None
    )


def test_the_placed_environment_really_reaches_uv(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """End to end through the subprocess, with a hostile inherited value set."""

    monkeypatch.setenv(UV_PROJECT_ENVIRONMENT, str(tmp_path / "inherited"))
    project = uv_project(tmp_path / "project", module=FIXED)
    environment = tmp_path / "runtime" / "uv" / "T-001"

    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
        uv_project_environment=environment,
    )

    assert result.exit_code == 0, result.error
    assert environment.is_dir()
    assert not (tmp_path / "inherited").exists(), "the inherited value won"
    assert not (project / ".venv").exists()


# -- the controller path -----------------------------------------------------


class Started(NamedTuple):
    controller: AutomationController
    provider: FakeProvider
    repo: Path
    run: AutomationRun
    store: RunStore


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


def start(
    tmp_path: Path,
    *,
    coder: list[ScriptedResponse],
    review: list[dict[str, Any]] | None = None,
    module: str = STUB,
) -> Started:
    from research_os.automation.models import Role
    from tests.automation_helpers import review_payload

    repo = uv_project(tmp_path / "project", module=module)
    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=uv_plan())],
            str(Role.CODER): coder,
            str(Role.REVIEWER): [
                ScriptedResponse(structured=item)
                for item in (review or [review_payload()])
            ],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, run = controller.start(
        project_path=repo, goal="Implement the missing function."
    )
    return Started(controller, provider, repo, run, store)


def wrote(content: str) -> ScriptedResponse:
    return ScriptedResponse(text="done", write_files={"adder.py": content})


def worktree_of(run: AutomationRun) -> Path:
    return Path(run.order("T-001").worktree_path or "")


# -- B and C. the repair path a uv check used to make unreachable ------------


def test_a_uv_check_failure_still_reaches_a_repair_and_becomes_ready(
    automation_home: Path, tmp_path: Path
) -> None:
    """The whole defect, end to end: first attempt fails, repair runs, run ends ready.

    Before the environments were placed, the repair preflight found
    ``.venv/bin/python`` inside the worktree - created by the controller's own
    check - and refused the repair the run was entitled to.
    """

    ctx = start(tmp_path, coder=[wrote(WRONG), wrote(FIXED)])

    final = ctx.controller.execute(ctx.store)

    order = final.order("T-001")
    assert final.state is RunState.READY_FOR_HUMAN
    assert order.status is WorkOrderStatus.REVIEWED
    assert order.repair_attempts == 1, "the repair did not happen"
    assert ready_for_human_blockers(final) == []

    events = [item for item in ctx.store.iter_events()]
    kinds = [item["event"] for item in events]
    assert kinds.count("repair_started") == 1
    assert kinds.count("repair_completed") == 1

    checks = [item for item in events if item["event"] == "command_executed"]
    first = [item for item in checks if item["attempt"] == 1]
    second = [item for item in checks if item["attempt"] == 2]
    assert any(item["exit_code"] != 0 for item in first), "nothing failed first"
    assert all(item["exit_code"] == 0 for item in second), "the repair did not fix it"

    assert not (worktree_of(final) / ".venv").exists()


def test_the_repair_preflight_symlink_scan_stays_active(
    automation_home: Path, tmp_path: Path
) -> None:
    """The gate is not skipped; there is simply nothing outbound left to find."""

    from research_os.automation.filescope import outbound_symlinks

    ctx = start(tmp_path, coder=[wrote(WRONG), wrote(FIXED)])
    final = ctx.controller.execute(ctx.store)

    worktree = worktree_of(final)
    assert worktree.is_dir()
    assert outbound_symlinks(worktree) == ()


def test_the_controller_places_the_environment_under_the_run(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start(tmp_path, coder=[wrote(FIXED)])
    final = ctx.controller.execute(ctx.store)

    placed = ctx.store.path(*UV_ENVIRONMENT_PARTS, "T-001")
    assert placed.is_dir()
    assert runs_root() in placed.parents
    assert ctx.store.directory in placed.parents
    worktree = worktree_of(final)
    assert worktree not in placed.parents and placed != worktree
    assert ctx.repo not in placed.parents

    recorded = {
        item.get("uv_project_environment")
        for item in ctx.store.iter_events()
        if item["event"] == "command_executed"
    }
    assert str(placed) in recorded, "the environment was not recorded for provenance"


def test_the_repair_reuses_the_environment_the_first_attempt_built(
    automation_home: Path, tmp_path: Path
) -> None:
    """One environment per task, not one per attempt."""

    ctx = start(tmp_path, coder=[wrote(WRONG), wrote(FIXED)])
    ctx.controller.execute(ctx.store)

    recorded = [
        item["uv_project_environment"]
        for item in ctx.store.iter_events()
        if item["event"] == "command_executed" and "uv_project_environment" in item
    ]
    assert recorded, "no uv check ran"
    assert len(set(recorded)) == 1, "attempts used different environments"
    attempts = {
        item["attempt"]
        for item in ctx.store.iter_events()
        if item["event"] == "command_executed"
    }
    assert attempts == {1, 2}


# -- E. one environment per run and task ------------------------------------


def test_two_tasks_and_two_runs_never_share_an_environment(
    automation_home: Path, tmp_path: Path
) -> None:
    first = start(tmp_path / "one", coder=[wrote(FIXED)])
    second = start(tmp_path / "two", coder=[wrote(FIXED)])

    paths = {
        first.store.path(*UV_ENVIRONMENT_PARTS, "T-001"),
        first.store.path(*UV_ENVIRONMENT_PARTS, "T-002"),
        second.store.path(*UV_ENVIRONMENT_PARTS, "T-001"),
    }
    assert len(paths) == 3
    assert first.store.directory != second.store.directory


# -- D. no pathname is excused from the gate ---------------------------------


def test_a_worker_created_outbound_symlink_still_fails_the_work_order(
    automation_home: Path, tmp_path: Path
) -> None:
    """Placing uv's environment elsewhere did not create a .venv exemption."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours\n", encoding="utf-8")

    ctx = start(
        tmp_path,
        coder=[
            ScriptedResponse(
                text="done",
                write_files={"adder.py": FIXED},
                create_symlinks={".venv/bin/python": str(outside / "secret.txt")},
            )
        ],
    )

    with pytest.raises(AutomationError, match="symlink"):
        ctx.controller.execute(ctx.store)

    final = ctx.store.load()
    assert final.state is RunState.FAILED
    assert final.order("T-001").status is WorkOrderStatus.FAILED
    violations = [
        item
        for item in ctx.store.iter_events()
        if item["event"] == "symlink_scope_violation"
    ]
    assert violations, "a .venv-shaped outbound symlink was excused"


def test_a_pre_existing_outbound_symlink_still_fails_the_work_order(
    automation_home: Path, tmp_path: Path
) -> None:
    """One that shipped in the base commit, under the very name uv would use."""

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("not yours\n", encoding="utf-8")

    repo = uv_project(tmp_path / "project", module=FIXED)
    link = repo / "vendored-python"
    link.symlink_to(outside / "secret.txt")
    _git(["add", "-A", "-f"], repo)
    _git(["commit", "-m", "vendored link"], repo)

    from research_os.automation.models import Role
    from tests.automation_helpers import review_payload

    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=uv_plan())],
            str(Role.CODER): [wrote(FIXED)],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")

    with pytest.raises(AutomationError, match="symlink"):
        controller.execute(store)

    assert store.load().state is RunState.FAILED


# -- runtime storage ---------------------------------------------------------


def test_cleanup_removes_the_check_environment_and_keeps_the_evidence(
    automation_home: Path, tmp_path: Path
) -> None:
    ctx = start(tmp_path, coder=[wrote(FIXED)])
    ctx.controller.execute(ctx.store)
    placed = ctx.store.path(*UV_ENVIRONMENT_PARTS, "T-001")
    assert placed.is_dir()

    _, removed = ctx.controller.cleanup(ctx.store)

    assert str(ctx.store.path(*UV_ENVIRONMENT_PARTS)) in removed
    assert not placed.exists()
    assert ctx.store.run_file.is_file()
    assert list(ctx.store.iter_events())
    assert ctx.store.path("checks", "T-001").is_dir()
    assert ctx.store.path("prompts").is_dir()
    assert ctx.store.path("model_outputs").is_dir()
    assert any(
        item["event"] == "check_environment_removed" for item in ctx.store.iter_events()
    )


def test_cleanup_reports_nothing_when_no_uv_check_ran(
    automation_home: Path, tmp_path: Path
) -> None:
    """A plan that never ran uv has no environment to remove."""

    from research_os.automation.models import Role
    from tests.automation_helpers import FIXED_MODULE, init_repo, review_payload
    from tests.automation_helpers import plan_payload as bare_plan

    repo = init_repo(tmp_path / "bare")
    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=bare_plan())],
            str(Role.CODER): [
                ScriptedResponse(text="done", write_files={"adder.py": FIXED_MODULE})
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")
    controller.execute(store)

    _, removed = controller.cleanup(store)

    assert not store.path(*UV_ENVIRONMENT_PARTS).exists()
    assert all("runtime" not in item for item in removed)


def test_the_placed_environment_is_not_reachable_through_the_worktree(
    automation_home: Path, tmp_path: Path
) -> None:
    """The writer's tools are confined to the worktree; the environment is not in it."""

    ctx = start(tmp_path, coder=[wrote(FIXED)])
    final = ctx.controller.execute(ctx.store)

    placed = ctx.store.path(*UV_ENVIRONMENT_PARTS, "T-001").resolve()
    worktree = worktree_of(final).resolve()
    assert worktree != placed
    assert worktree not in placed.parents
    assert not any(
        entry.name == ".venv" for entry in worktree.iterdir() if entry.is_dir()
    )
    assert os.path.commonpath([str(placed), str(worktree)]) != str(worktree)
