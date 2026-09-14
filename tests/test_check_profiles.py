"""Who decides how this project's tests are run, and who must not.

The failure behind this file is in the v1.0.0 build record, recorded there as an
operational note because at the time it was one: a ``src``-layout Python project
whose tests only import under ``uv run``, a planner that wrote a bare ``pytest``,
a collection failure, a bounded repair spent on the import error, and a run that
failed closed having verified nothing.

Nothing about that was carelessness. It was a model being asked an environment
question it had no way to answer. These tests establish that it is no longer
asked, and that the answer it is given cannot be widened by anything it writes.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.checkprofiles import (
    CheckProfile,
    CheckProfileSpec,
    ProfileSource,
    render_check_profiles,
    resolve_check_profiles,
    resolve_required_checks,
)
from research_os.automation.config import (
    DEFAULT_ALLOWED_CHECK_PROGRAMS,
    AutomationConfig,
    ProjectSettings,
    default_config,
    load_config,
)
from research_os.automation.planner import parse_plan
from research_os.automation.profile import inspect_repository
from research_os.automation.projectcontext import resolve_project
from research_os.errors import AutomationError, CommandPolicyError, ResearchPlanError
from research_os.research.models import ResearchBudget
from research_os.research.planner import (
    ResearchPlan,
    build_research_plan_prompt,
    validate_research_plan,
)
from tests.research_helpers import code_task, plan_payload, task

PYPROJECT_UV = """\
[project]
name = "demo"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = []

[dependency-groups]
dev = ["pytest>=8", "ruff>=0.6"]

[tool.ruff]
line-length = 88
"""


def _git(root: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=root, check=True, capture_output=True)


def repository(root: Path, files: dict[str, str]) -> Path:
    root.mkdir(parents=True, exist_ok=True)
    _git(root, "init", "-q")
    _git(root, "config", "user.email", "t@example.invalid")
    _git(root, "config", "user.name", "T")
    for relative, content in files.items():
        target = root / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    _git(root, "add", "-A")
    _git(root, "commit", "-q", "-m", "fixture")
    return root


def uv_src_repository(root: Path) -> Path:
    return repository(
        root,
        {
            "pyproject.toml": PYPROJECT_UV,
            "uv.lock": "version = 1\n",
            "src/demo/__init__.py": "",
            "tests/test_demo.py": "from demo import __name__ as n\n",
        },
    )


def facts_of(root: Path) -> Any:
    return inspect_repository(root)


def discover(root: Path) -> tuple[tuple[CheckProfile, ...], bool]:
    facts = facts_of(root)
    return resolve_check_profiles(
        tracked=facts.tracked,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        allowed_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
    )


# -- discovery -----------------------------------------------------------


def test_a_uv_project_discovers_the_uv_invocation(tmp_path: Path) -> None:
    """The whole point of the release, in one assertion."""

    profiles, explicit = discover(uv_src_repository(tmp_path / "src"))
    assert explicit is False
    by_id = {item.check_id: item for item in profiles}
    assert by_id["tests"].argv == ["uv", "run", "pytest", "-q"]
    assert by_id["lint"].argv == ["uv", "run", "ruff", "check", "."]
    assert by_id["format"].argv == ["uv", "run", "ruff", "format", "--check", "."]
    assert all(item.source is ProfileSource.DISCOVERED for item in profiles)


def test_the_discovered_test_command_is_never_a_bare_pytest(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    for profile in profiles:
        assert profile.argv[0] == "uv"
        assert profile.argv[:2] == ["uv", "run"]


def test_no_lock_file_means_no_discovered_profile(tmp_path: Path) -> None:
    """``uv run`` is only right for a project uv can materialise."""

    root = repository(
        tmp_path / "nolock",
        {"pyproject.toml": PYPROJECT_UV, "src/demo/__init__.py": ""},
    )
    profiles, explicit = discover(root)
    assert profiles == ()
    assert explicit is False


def test_a_project_with_no_python_discovers_nothing(tmp_path: Path) -> None:
    root = repository(tmp_path / "julia", {"Project.toml": 'name = "Demo"\n'})
    assert discover(root)[0] == ()


def test_ruff_is_only_offered_when_the_project_declares_it(tmp_path: Path) -> None:
    root = repository(
        tmp_path / "noruff",
        {
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\n'
                'dependencies = []\n\n[dependency-groups]\ndev = ["pytest>=8"]\n'
            ),
            "uv.lock": "version = 1\n",
            "tests/test_demo.py": "def test_x():\n    assert True\n",
        },
    )
    assert {item.check_id for item in discover(root)[0]} == {"tests"}


def test_a_tests_directory_alone_does_not_justify_uv_run_pytest(
    tmp_path: Path,
) -> None:
    """Found by the delta review.

    Offering `uv run pytest -q` to a project that declares pytest nowhere
    reintroduces the v1.0.0 trap through the profile instead of through the
    planner: the command cannot spawn, the plan is *forced* to name the check
    because a profile exists, the bounded repair burns on an environment error,
    and the run fails closed having verified nothing.

    `_dependency_names` already reads every list uv could have resolved the tool
    from, so a project where `uv run pytest` works is a project that declares
    pytest.
    """

    root = repository(
        tmp_path / "undeclared",
        {
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\ndependencies = []\n'
            ),
            "uv.lock": "version = 1\n",
            "tests/test_demo.py": "def test_x():\n    assert True\n",
            "src/demo/__init__.py": "",
        },
    )
    profiles, _ = discover(root)
    assert profiles == (), [item.argv for item in profiles]


def test_a_tool_ruff_table_alone_does_not_justify_uv_run_ruff(
    tmp_path: Path,
) -> None:
    """ruff configured but installed globally or via pre-commit, not declared."""

    root = repository(
        tmp_path / "globalruff",
        {
            "pyproject.toml": (
                '[project]\nname = "demo"\nversion = "0.1.0"\n'
                'dependencies = []\n\n[dependency-groups]\ndev = ["pytest>=8"]\n'
                "\n[tool.ruff]\nline-length = 88\n"
            ),
            "uv.lock": "version = 1\n",
            "tests/test_demo.py": "def test_x():\n    assert True\n",
        },
    )
    assert {item.check_id for item in discover(root)[0]} == {"tests"}


def test_a_discovered_profile_never_contradicts_the_capability_beside_it(
    tmp_path: Path,
) -> None:
    """The profile and the profiles must not disagree in one prompt.

    A `tests` profile beside `pytest_available: no` is two controller-authored
    lines contradicting each other, under a heading that says they are facts.
    Asserted as an invariant over every fixture in this file rather than as one
    case, because the disagreement was introduced by an extra disjunct nobody
    read twice.
    """

    from research_os.automation.profile import CapabilityName, build_project_profile

    roots = [
        uv_src_repository(tmp_path / "uv"),
        repository(tmp_path / "plain", {"README.md": "# x\n"}),
        repository(
            tmp_path / "undeclared2",
            {
                "pyproject.toml": (
                    '[project]\nname = "d"\nversion = "0.1"\ndependencies = []\n'
                ),
                "uv.lock": "version = 1\n",
                "tests/test_a.py": "def test_a():\n    assert True\n",
            },
        ),
        repository(tmp_path / "nolock2", {"pyproject.toml": PYPROJECT_UV}),
    ]
    for root in roots:
        facts = facts_of(root)
        profiles, _ = discover(root)
        profile = build_project_profile(
            project_path=root,
            facts=facts,
            check_profile_ids=tuple(item.check_id for item in profiles),
        )
        ids = {item.check_id for item in profiles}
        if "tests" in ids:
            assert profile.has(CapabilityName.PYTEST_AVAILABLE), root
        if "lint" in ids or "format" in ids:
            assert profile.has(CapabilityName.RUFF_AVAILABLE), root


def test_discovery_is_stable(tmp_path: Path) -> None:
    root = uv_src_repository(tmp_path / "src")
    assert discover(root) == discover(root)


# -- precedence ----------------------------------------------------------


def configured(
    root: Path, spec: dict[str, CheckProfileSpec]
) -> tuple[CheckProfile, ...]:
    facts = facts_of(root)
    profiles, explicit = resolve_check_profiles(
        tracked=facts.tracked,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        allowed_programs=DEFAULT_ALLOWED_CHECK_PROGRAMS,
        configured=spec,
    )
    assert explicit is True
    return profiles


def test_configuration_replaces_discovery_entirely(tmp_path: Path) -> None:
    """Two declared checks means two checks, not two plus whatever was found."""

    root = uv_src_repository(tmp_path / "src")
    profiles = configured(
        root,
        {
            "tests": CheckProfileSpec(argv=["uv", "run", "pytest", "-q", "tests"]),
            "lint": CheckProfileSpec(argv=["uv", "run", "ruff", "check", "src"]),
        },
    )
    assert [item.check_id for item in profiles] == ["lint", "tests"]
    assert profiles[1].argv == ["uv", "run", "pytest", "-q", "tests"]
    assert all(item.source is ProfileSource.EXPLICIT_CONFIG for item in profiles)


def test_configuration_may_declare_an_optional_check(tmp_path: Path) -> None:
    root = uv_src_repository(tmp_path / "src")
    profiles = configured(
        root,
        {
            "format": CheckProfileSpec(
                argv=["ruff", "format", "--check", "."], required=False
            )
        },
    )
    assert profiles[0].required is False


# -- a profile is not an escalation --------------------------------------


def test_a_configured_command_the_policy_refuses_is_a_configuration_error(
    tmp_path: Path,
) -> None:
    root = uv_src_repository(tmp_path / "src")
    with pytest.raises(CommandPolicyError):
        configured(root, {"tests": CheckProfileSpec(argv=["bash", "-c", "pytest"])})


def test_configuration_cannot_introduce_a_forbidden_program(tmp_path: Path) -> None:
    path = tmp_path / "automation.yaml"
    path.write_text(
        "schema_version: 1\n"
        "projects:\n"
        "  demo:\n"
        "    check_profiles:\n"
        "      tests:\n"
        "        argv: [python, -c, 'import os; os.system(\"rm -rf /\")']\n",
        encoding="utf-8",
    )
    with pytest.raises(AutomationError, match="not a command the controller may run"):
        load_config(path)


def test_a_narrowed_allowlist_removes_the_uv_profiles(tmp_path: Path) -> None:
    """Discovery proposes; the command policy disposes."""

    facts = facts_of(uv_src_repository(tmp_path / "src"))
    profiles, _ = resolve_check_profiles(
        tracked=facts.tracked,
        dependencies=facts.dependencies,
        tool_sections=facts.tool_sections,
        allowed_programs=("pytest", "ruff"),
    )
    assert profiles == ()


def test_a_traversing_configured_path_is_refused(tmp_path: Path) -> None:
    root = uv_src_repository(tmp_path / "src")
    with pytest.raises(CommandPolicyError, match="traverses"):
        configured(
            root, {"tests": CheckProfileSpec(argv=["uv", "run", "pytest", "../../etc"])}
        )


# -- resolution ----------------------------------------------------------


def test_resolution_follows_the_project_order_not_the_request(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    resolved = resolve_required_checks(profiles, ["format", "tests"])
    assert [item.check_id for item in resolved] == ["tests", "format"]


def test_resolving_an_unknown_check_is_refused(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    with pytest.raises(CommandPolicyError, match="no profile for"):
        resolve_required_checks(profiles, ["tests", "typecheck"])


def test_a_repeated_request_collapses(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    resolved = resolve_required_checks(profiles, ["tests", "tests"])
    assert len(resolved) == 1


def test_rendering_shows_the_argv_a_planner_cannot_change(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    rendered = render_check_profiles(profiles)
    assert "tests: uv run pytest -q" in rendered
    assert render_check_profiles(()).strip().startswith("(none")


# -- the plan contract ---------------------------------------------------


def validate(payload: dict[str, Any], profiles: tuple[CheckProfile, ...]) -> None:
    validate_research_plan(
        ResearchPlan.model_validate(payload),
        budget=ResearchBudget(),
        declared_experiments=frozenset(),
        check_profiles=profiles,
    )


def test_a_plan_writing_its_own_command_is_refused_where_a_profile_exists(
    tmp_path: Path,
) -> None:
    """The src-layout regression, as a rule.

    The old wrong behaviour -- a planner writing ``["pytest", "-q"]`` for a
    project whose tests need ``uv run`` -- is not merely discouraged here. It
    does not validate.
    """

    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    payload = plan_payload(
        tasks=[code_task(acceptance_commands=[["pytest", "-q"]])],
    )
    with pytest.raises(ResearchPlanError, match="controller-owned validation"):
        validate(payload, profiles)


def test_a_plan_naming_check_ids_validates(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    payload = plan_payload(
        tasks=[code_task(acceptance_commands=[], required_checks=["tests", "lint"])]
    )
    validate(payload, profiles)


def test_a_code_task_with_no_checks_at_all_is_refused(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    payload = plan_payload(tasks=[code_task(acceptance_commands=[])])
    with pytest.raises(ResearchPlanError, match="names no required_checks"):
        validate(payload, profiles)


def test_a_plan_naming_a_check_the_project_lacks_is_refused(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    payload = plan_payload(
        tasks=[code_task(acceptance_commands=[], required_checks=["typecheck"])]
    )
    with pytest.raises(ResearchPlanError, match="no profile for"):
        validate(payload, profiles)


def test_check_ids_without_profiles_are_refused(tmp_path: Path) -> None:
    payload = plan_payload(
        tasks=[code_task(acceptance_commands=[], required_checks=["tests"])]
    )
    with pytest.raises(ResearchPlanError, match="no controller-owned validation"):
        validate(payload, ())


def test_a_project_with_no_profiles_still_names_its_own_commands() -> None:
    validate(plan_payload(tasks=[code_task()]), ())


def test_only_a_code_task_may_require_checks(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    payload = plan_payload(tasks=[task(kind="literature", required_checks=["tests"])])
    with pytest.raises(ResearchPlanError, match="only a code task"):
        validate(payload, profiles)


def test_the_prompt_tells_the_planner_not_to_write_commands(tmp_path: Path) -> None:
    profiles, _ = discover(uv_src_repository(tmp_path / "src"))
    prompt = build_research_plan_prompt(
        goal="Fix the adder.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=[],
        check_profiles=profiles,
    )
    assert "Do not write a command yourself" in prompt
    assert "tests: uv run pytest -q" in prompt
    assert 'must set "required_checks"' in prompt


def test_the_prompt_asks_for_commands_when_there_are_no_profiles() -> None:
    prompt = build_research_plan_prompt(
        goal="Fix the adder.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=[],
        check_profiles=(),
    )
    assert 'must set "acceptance_commands"' in prompt
    assert "Do not write a command yourself" not in prompt


# -- model authority -----------------------------------------------------


def test_a_model_cannot_mark_its_own_acceptance_command_optional() -> None:
    """The one plan field a worker must not reach.

    Every other planning decision is about what to do. This one is about whether
    the controller's verdict counts, and a worker that can mark its acceptance
    command optional has been handed the gate it was supposed to pass.
    """

    from research_os.errors import PlanValidationError

    payload = {
        "summary": "Implement it.",
        "tasks": [
            {
                "id": "T-001",
                "title": "Implement add",
                "goal": "Return the sum.",
                "role": "coder",
                "read_only": False,
                "allowed_paths": ["adder.py"],
                "acceptance_commands": [
                    {
                        "argv": ["pytest", "-q"],
                        "description": "tests",
                        "required": False,
                    }
                ],
                "expected_artifacts": [],
                "completion_condition": "tests pass",
                "dependencies": [],
            }
        ],
    }
    with pytest.raises(PlanValidationError, match="optional"):
        parse_plan(structured=payload, text=None)


def test_a_controller_built_plan_may_declare_an_optional_check() -> None:
    from research_os.automation.planner import PlannedCommand

    assert PlannedCommand(argv=["ruff", "check", "."], required=False).required is False


def test_configured_profiles_reach_the_project_resolution(tmp_path: Path) -> None:
    root = uv_src_repository(tmp_path / "src")
    base = default_config()
    config = AutomationConfig(
        roles=base.roles,
        budget=base.budget,
        allowed_check_programs=base.allowed_check_programs,
        source=None,
        explicit_roles=frozenset(),
        projects={
            "demo": ProjectSettings(
                check_profiles={
                    "tests": CheckProfileSpec(
                        argv=["uv", "run", "pytest", "-q", "tests"]
                    )
                }
            )
        },
    )
    resolved = resolve_project(project_path=root, config=config, project_id="demo")
    assert resolved.check_ids == ("tests",)
    assert resolved.check_profiles[0].argv == ["uv", "run", "pytest", "-q", "tests"]
