"""The src-layout trap, closed, against a real project and a real uv.

v1.0.0's build record states the trap as an operational note: *for a src-layout
Python project, an acceptance command must be ``uv run pytest``, not ``pytest``*.
That sentence is a warning to a human about a decision a model was being asked
to make. This file turns it into a decision the controller makes.

The fixture is deliberately discriminating. Its package lives under ``src/`` and
is installed by uv from the project's own lock file, so ``uv run pytest`` imports
it and a bare ``pytest`` cannot -- which is asserted here directly, so that the
end-to-end test below cannot pass for the wrong reason.

Real Git, real uv, real pytest, real worktrees. Only the model is fake.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

from research_os.automation.checkprofiles import resolve_check_profiles
from research_os.automation.checks import run_acceptance_command
from research_os.automation.config import DEFAULT_ALLOWED_CHECK_PROGRAMS
from research_os.automation.models import AcceptanceCommand
from research_os.automation.profile import CapabilityName, inspect_repository
from research_os.automation.projectcontext import resolve_project
from research_os.automation.store import RunStore
from research_os.research.models import ResearchBudget, ResearchState
from tests.automation_helpers import review_payload
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.research_helpers import (
    code_task,
    fake_config,
    make_controller,
    plan_payload,
)

pytestmark = pytest.mark.skipif(
    shutil.which("uv") is None, reason="uv is not installed on this machine"
)

BROKEN = '''"""A deliberately incomplete module."""


def add(left, right):
    return 0
'''

FIXED = '''"""A deliberately incomplete module."""


def add(left, right):
    return left + right
'''

TESTS = """from demo import add


def test_add():
    assert add(2, 3) == 5
"""


def _pinned_pytest() -> str:
    """Pin the pytest this suite already runs under, so uv resolves from cache."""

    import pytest as _pytest

    return f"pytest=={_pytest.__version__}"


PYPROJECT = """\
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[dependency-groups]
dev = ["{pytest}"]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"

[tool.hatch.build.targets.wheel]
packages = ["src/demo"]
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def src_layout_project(path: Path, *, module: str = BROKEN) -> Path:
    """A committed src-layout uv project whose tests only import under ``uv run``."""

    (path / "src" / "demo").mkdir(parents=True, exist_ok=True)
    (path / "tests").mkdir(parents=True, exist_ok=True)
    (path / "pyproject.toml").write_text(
        PYPROJECT.format(pytest=_pinned_pytest()), encoding="utf-8"
    )
    (path / ".gitignore").write_text(
        ".venv/\n__pycache__/\n.pytest_cache/\n", encoding="utf-8"
    )
    (path / "src" / "demo" / "__init__.py").write_text(module, encoding="utf-8")
    (path / "tests" / "test_demo.py").write_text(TESTS, encoding="utf-8")
    lock = subprocess.run(
        ["uv", "lock", "--offline"],
        cwd=str(path),
        capture_output=True,
        text=True,
        check=False,
    )
    if lock.returncode != 0:  # pragma: no cover - depends on the machine's uv cache
        pytest.skip(f"uv could not resolve the fixture offline: {lock.stderr[-400:]}")
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)
    return path


# -- the fixture discriminates -------------------------------------------


def test_the_old_wrong_profile_cannot_even_collect(tmp_path: Path) -> None:
    """Proof that this fixture would catch the v1.0.0 behaviour.

    A regression that passes under both the right and the wrong command is not a
    regression. This asserts the wrong one fails, so the test below means
    something when it passes.
    """

    project = src_layout_project(tmp_path / "project", module=FIXED)
    result = run_acceptance_command(
        AcceptanceCommand(argv=["pytest", "-q"]),
        cwd=project,
        timeout_seconds=300,
    )
    assert result.exit_code != 0
    stdout = Path(result.stdout_path).read_text() if result.stdout_path else ""
    assert result.exit_code != 0 or "No module named 'demo'" in stdout


def test_the_controller_owned_profile_does_collect(tmp_path: Path) -> None:
    project = src_layout_project(tmp_path / "project", module=FIXED)
    result = run_acceptance_command(
        AcceptanceCommand(argv=["uv", "run", "pytest", "-q"]),
        cwd=project,
        timeout_seconds=600,
        uv_project_environment=tmp_path / "runtime" / "uv",
    )
    assert result.exit_code == 0, result.error


# -- the controller identifies it ----------------------------------------


def test_the_profile_says_src_layout_and_offers_uv(tmp_path: Path) -> None:
    project = src_layout_project(tmp_path / "project")
    resolved = resolve_project(project_path=project, config=fake_config())
    assert resolved.profile.has(CapabilityName.PYTHON_SRC_LAYOUT)
    assert resolved.profile.has(CapabilityName.UV_LOCK)
    assert resolved.profile.has(CapabilityName.VALIDATION_PROFILES)
    by_id = {item.check_id: item for item in resolved.check_profiles}
    assert by_id["tests"].argv == ["uv", "run", "pytest", "-q"]


def test_discovery_needs_the_lock_this_fixture_actually_has(tmp_path: Path) -> None:
    project = src_layout_project(tmp_path / "project")
    facts = inspect_repository(project)
    assert "uv.lock" in facts.tracked
    profiles, explicit = resolve_check_profiles(
        tracked=facts.tracked,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        allowed_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
    )
    assert explicit is False
    assert profiles[0].argv[:2] == ["uv", "run"]


# -- end to end ----------------------------------------------------------


def test_a_research_run_verifies_a_src_layout_project_without_guessing(
    automation_home: Path, tmp_path: Path
) -> None:
    """Planner -> Coder -> controller-owned checks -> Reviewer -> READY_FOR_HUMAN.

    The plan names ``["tests"]``. It never writes a command, and the command it
    never wrote is the one that would have failed.
    """

    project = src_layout_project(tmp_path / "project")
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            "planner": [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            code_task(
                                title="Implement add",
                                goal="Make add return the sum of its arguments.",
                                allowed_paths=["src/demo/__init__.py"],
                                acceptance_commands=[],
                                required_checks=["tests"],
                            )
                        ],
                        summary="Implement add and let the project's own tests judge it.",
                    )
                )
            ],
            "coder": [
                ScriptedResponse(
                    text="Implemented add.",
                    write_files={"src/demo/__init__.py": FIXED},
                )
            ],
            "reviewer": [ScriptedResponse(structured=review_payload())],
        },
    )
    controller = make_controller(provider)
    store, run = controller.start(
        project_path=project,
        goal="Make the adder work and have the project's own checks prove it.",
        budget=ResearchBudget(max_tasks=1, max_model_calls=6, max_write_tasks=1),
    )
    run = controller.execute(store)

    assert run.state is ResearchState.READY_FOR_HUMAN, run.failure_reason
    task = run.tasks[0]
    assert task.required_checks == ["tests"]
    assert task.acceptance_commands == []
    assert task.resolved_checks == [["uv", "run", "pytest", "-q"]]

    resolved = [
        item for item in store.iter_events() if item["event"] == "checks_resolved"
    ]
    assert resolved and resolved[0]["argv"] == [["uv", "run", "pytest", "-q"]]

    # The command the controller actually ran, and what it actually returned.
    # Reaching READY_FOR_HUMAN is not by itself proof that the project's own
    # tests were executed and passed; this is.
    inner = RunStore.open(task.artifact_id).load()
    order = inner.work_orders[0]
    ran = [item for item in order.check_results if item.argv[:2] == ["uv", "run"]]
    assert ran, [item.argv for item in order.check_results]
    assert ran[0].argv == ["uv", "run", "pytest", "-q"]
    assert ran[0].exit_code == 0, ran[0].error
    assert order.repair_attempts == 0

    # The canonical checkout is untouched: the work happened in a worktree.
    assert (project / "src" / "demo" / "__init__.py").read_text() == BROKEN
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(project),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert status == ""
