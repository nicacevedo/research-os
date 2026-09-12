"""Builders for automation-controller tests.

Every helper here produces a real Git repository and a real run directory, so
the tests exercise the same worktree creation, subprocess execution, and atomic
writes the controller performs in production. Only the model providers are fake.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from research_os.automation.config import (
    DEFAULT_ALLOWED_CHECK_PROGRAMS,
    AutomationConfig,
)
from research_os.automation.controller import AutomationController
from research_os.automation.models import Access, Budget, RoleSetting

BROKEN_MODULE = '''"""A deliberately incomplete module."""


def add(left, right):
    raise NotImplementedError
'''

FIXED_MODULE = '''"""A deliberately incomplete module."""


def add(left, right):
    return left + right
'''

MODULE_TESTS = """from adder import add


def test_add_small():
    assert add(2, 3) == 5


def test_add_negative():
    assert add(-1, 1) == 0
"""


def init_repo(path: Path) -> Path:
    """Create a Git repository with a committed, deterministic Python task."""

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    (path / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (path / "test_adder.py").write_text(MODULE_TESTS, encoding="utf-8")
    (path / "README.md").write_text("# fixture repo\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)
    return path


def commit_all(path: Path, message: str = "capsule") -> str:
    """Commit everything in ``path`` and return the new commit sha."""

    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    _git(["add", "-A"], path)
    _git(["commit", "-m", message], path)
    return head(path)


def head(path: Path) -> str:
    return _git(["rev-parse", "HEAD"], path).stdout.strip()


def fake_config(
    *,
    planner: str = "fake",
    analyst: str = "fake",
    coder: str = "fake",
    reviewer: str = "fake",
    budget: Budget | None = None,
    explicit_roles: frozenset[str] = frozenset(),
    analyst_tools: list[str] | None = None,
) -> AutomationConfig:
    """Return a configuration bound to test doubles.

    The analyst is configured exactly as it ships: snapshot-read access with
    the three read-only file tools and nothing else, so a test that drives an
    analysis task exercises the real tool policy rather than a permissive one.
    """

    return AutomationConfig(
        roles={
            "planner": RoleSetting(
                provider=planner,
                model="fake-planner",
                read_only=True,
                access=Access.CONTEXT_ONLY,
                tools=[],
            ),
            "analyst": RoleSetting(
                provider=analyst,
                model="fake-analyst",
                read_only=True,
                access=Access.SNAPSHOT_READ,
                tools=analyst_tools or ["Read", "Glob", "Grep"],
            ),
            "coder": RoleSetting(
                provider=coder,
                model="fake-coder",
                read_only=False,
                access=Access.ISOLATED_WRITE,
                tools=["Read", "Write", "Edit"],
            ),
            "reviewer": RoleSetting(
                provider=reviewer,
                model="fake-reviewer",
                read_only=True,
                access=Access.CONTEXT_ONLY,
                tools=[],
            ),
        },
        budget=budget or Budget(),
        allowed_check_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
        source=None,
        explicit_roles=explicit_roles,
    )


def make_controller(
    providers: dict[str, Any],
    *,
    config: AutomationConfig | None = None,
) -> AutomationController:
    return AutomationController(
        providers=providers,
        config=config or fake_config(),
    )


def plan_payload(
    *,
    allowed: tuple[str, ...] = ("adder.py",),
    argv: tuple[str, ...] = ("pytest", "-q"),
    task_id: str = "T-001",
    read_only: bool = False,
    tasks: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Return a schema-conforming planner payload."""

    if tasks is None:
        tasks = [
            {
                "id": task_id,
                "title": "Implement add",
                "goal": "Make add return the sum of its two arguments.",
                "read_only": read_only,
                "allowed_paths": list(allowed),
                "forbidden_paths": ["test_adder.py"],
                "acceptance_commands": [
                    {"argv": list(argv), "description": "the supplied tests pass"}
                ],
                "expected_artifacts": ["adder.py"],
                "completion_condition": "pytest exits 0",
                "dependencies": [],
            }
        ]
    return {"summary": "Implement the missing function.", "tasks": tasks}


def analysis_task(
    *,
    task_id: str = "T-001",
    read_paths: tuple[str, ...] = ("adder.py", "test_adder.py"),
    read_only: bool = True,
) -> dict[str, Any]:
    """Return a schema-conforming analyst task for a planner payload."""

    return {
        "id": task_id,
        "title": "Investigate the failing behaviour",
        "goal": "Read the module and its tests and explain why add is wrong.",
        "role": "analyst",
        "read_only": read_only,
        "allowed_paths": [],
        "forbidden_paths": [],
        "read_paths": list(read_paths),
        "acceptance_commands": [],
        "expected_artifacts": [],
        "completion_condition": "structured findings naming the defective function",
        "dependencies": [],
    }


def coding_task(
    *,
    task_id: str = "T-002",
    allowed: tuple[str, ...] = ("adder.py",),
    argv: tuple[str, ...] = ("pytest", "-q"),
    dependencies: tuple[str, ...] = ("T-001",),
) -> dict[str, Any]:
    """Return a schema-conforming coder task for a planner payload."""

    return {
        "id": task_id,
        "title": "Implement add",
        "goal": "Make add return the sum of its two arguments.",
        "role": "coder",
        "read_only": False,
        "allowed_paths": list(allowed),
        "forbidden_paths": ["test_adder.py"],
        "read_paths": [],
        "acceptance_commands": [
            {"argv": list(argv), "description": "the supplied tests pass"}
        ],
        "expected_artifacts": ["adder.py"],
        "completion_condition": "pytest exits 0",
        "dependencies": list(dependencies),
    }


def research_plan_payload(
    *,
    read_paths: tuple[str, ...] = ("adder.py", "test_adder.py"),
    allowed: tuple[str, ...] = ("adder.py",),
) -> dict[str, Any]:
    """Return the canonical two-task research plan: analyse, then implement."""

    return {
        "summary": "Investigate the defect, then correct it.",
        "tasks": [
            analysis_task(read_paths=read_paths),
            coding_task(allowed=allowed),
        ],
    }


def analysis_payload(
    *,
    summary: str = "add raises NotImplementedError instead of returning a sum.",
    findings: list[dict[str, Any]] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    uncertainties: list[str] | None = None,
    recommended_action: str = "Return left + right from add in adder.py.",
) -> dict[str, Any]:
    """Return a schema-conforming analyst payload."""

    if findings is None:
        findings = [
            {
                "id": "F-001",
                "statement": "add raises NotImplementedError rather than adding.",
                "importance": "high",
                "file_refs": ["adder.py"],
                "confidence": "high",
            }
        ]
    if evidence is None:
        evidence = [
            {
                "finding_id": "F-001",
                "file_ref": "adder.py",
                "detail": "the body of add is a bare raise NotImplementedError",
            }
        ]
    return {
        "summary": summary,
        "findings": findings,
        "evidence": evidence,
        "uncertainties": uncertainties
        if uncertainties is not None
        else ["whether any caller depends on the exception"],
        "recommended_action": recommended_action,
    }


def review_payload(
    verdict: str = "PASS",
    *,
    findings: list[dict[str, str]] | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": f"scripted {verdict}",
        "findings": findings or [],
    }


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        check=True,
        capture_output=True,
        text=True,
    )
