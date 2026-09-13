"""Builders for research-orchestration tests: real repositories, fake models only.

A research run dispatches to every other controller in the system, so these
tests run real Git repositories, real worktrees, real subprocesses, real
capsules, and real run directories. The only thing faked is the model.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

from research_os.automation.config import (
    DEFAULT_ALLOWED_CHECK_PROGRAMS,
    AutomationConfig,
)
from research_os.automation.models import Access, Budget, Role, RoleSetting
from research_os.experiment.config import (
    ExecutionLimits,
    ExperimentConfig,
    ProjectExperiments,
    SlurmSettings,
)
from research_os.experiment.spec import CommandSpec
from research_os.literature.config import LiteratureConfig
from research_os.research.controller import ResearchController
from tests.fake_providers import FakeProvider, ScriptedResponse

PROJECT_ID = "widget-study"

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
"""

EXPERIMENT_SCRIPT = """\
import json, pathlib, sys
seed = int(sys.argv[sys.argv.index("--seed") + 1])
pathlib.Path("results").mkdir(exist_ok=True)
pathlib.Path("results/fit.json").write_text(json.dumps({"seed": seed, "rmse": 0.1}))
print("fit complete")
"""


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def init_repo(path: Path, *, capsule: bool = True) -> Path:
    """A committed Git repository with a small task and, by default, a capsule."""

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    (path / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (path / "test_adder.py").write_text(MODULE_TESTS, encoding="utf-8")
    (path / "fit.py").write_text(EXPERIMENT_SCRIPT, encoding="utf-8")
    (path / "README.md").write_text("# widget study\n", encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-qm", "initial"], path)
    if capsule:
        from research_os.capsule import init_project
        from tests.proposal_helpers import default_objects, write_object

        init_project(path, project_id=PROJECT_ID, title="Widget study")
        for payload in default_objects():
            write_object(path, payload)
        _git(["add", "-A"], path)
        _git(["commit", "-qm", "capsule"], path)
    return path


def fake_config(
    *,
    planner: str = "fake",
    analyst: str = "fake",
    coder: str = "fake",
    reviewer: str = "fake",
    budget: Budget | None = None,
) -> AutomationConfig:
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
                tools=["Read", "Glob", "Grep"],
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
        explicit_roles=frozenset(),
    )


def offline_literature() -> LiteratureConfig:
    """A literature configuration that reaches nothing.

    Offline with no enabled sources, so a literature task in these tests
    exercises the real retrieval path and the real reporting of an incomplete
    retrieval without touching the network.
    """

    return LiteratureConfig(
        contact_email="tests@example.invalid",
        enabled_sources=(),
        offline=True,
        default_search_limit=5,
        fetch_fulltext=False,
        source=None,
    )


def fit_command(name: str = "fit-model", *, executor: str = "local") -> CommandSpec:
    return CommandSpec.model_validate(
        {
            "name": name,
            "argv": ["python3", "fit.py", "--seed", "{seed}"],
            "parameters": [
                {"name": "seed", "type": "integer", "required": True, "minimum": 0}
            ],
            "outputs": ["results/fit.json"],
            "checks": ["outputs_exist"],
            "timeout_seconds": 60,
            "executor": executor,
        }
    )


def experiment_config(*, commands: list[CommandSpec] | None = None) -> ExperimentConfig:
    declared = commands if commands is not None else [fit_command()]
    return ExperimentConfig(
        slurm=SlurmSettings(),
        limits=ExecutionLimits(),
        projects={
            PROJECT_ID: ProjectExperiments(
                commands={item.name: item for item in declared}
            )
        },
        source=None,
    )


def task(
    *,
    task_id: str = "T-001",
    kind: str = "literature",
    **overrides: Any,
) -> dict[str, Any]:
    """Return a schema-conforming planned task with every key present.

    The plan schema forbids extra keys and requires all of them, so the helper
    fills the whole shape and lets a caller override the parts that matter to
    the test.
    """

    payload: dict[str, Any] = {
        "id": task_id,
        "kind": kind,
        "title": "Read what is already known",
        "goal": "Find out whether anyone has settled this.",
        "depends_on": [],
        "query": "widget deformation under load",
        "read_paths": [],
        "allowed_paths": [],
        "acceptance_commands": [],
        "experiment_task": "",
        "experiment_parameters": {},
        "section": "",
        "question": "",
    }
    payload.update(overrides)
    return payload


def plan_payload(
    *,
    tasks: list[dict[str, Any]] | None = None,
    summary: str = "Read the literature, then decide.",
) -> dict[str, Any]:
    return {"summary": summary, "tasks": tasks if tasks is not None else [task()]}


def _task_of(defaults: dict[str, Any], overrides: dict[str, Any]) -> dict[str, Any]:
    """Build a planned task where a caller's value always wins.

    The kind helpers below preset the keys their kind needs. Forwarding those as
    keyword arguments would make an override collide with the preset rather than
    replace it, which is a confusing way for a test to fail.
    """

    return task(**{**defaults, **overrides})


def analysis_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "analysis",
            "title": "Understand the adder",
            "goal": "Explain what adder.py does and why its tests fail.",
            "query": "",
            "read_paths": ["adder.py", "test_adder.py"],
        },
        overrides,
    )


def code_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "code",
            "title": "Implement add",
            "goal": "Make add return the sum of its two arguments.",
            "query": "",
            "allowed_paths": ["adder.py"],
            "acceptance_commands": [["pytest", "-q"]],
        },
        overrides,
    )


def experiment_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "experiment",
            "title": "Fit the model",
            "goal": "Run the declared fit and see what it produces.",
            "query": "",
            "experiment_task": "fit-model",
            "experiment_parameters": {"seed": "7"},
        },
        overrides,
    )


def checkpoint_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "human_checkpoint",
            "title": "Decide whether to spend compute",
            "goal": "Ask the researcher before anything expensive runs.",
            "query": "",
            "question": "Should I run the fit on the cluster?",
        },
        overrides,
    )


def proposal_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "proposal",
            "title": "Propose what would settle it",
            "goal": "Propose the hypothesis and experiment that would settle Q-0001.",
            "query": "",
        },
        overrides,
    )


def paper_task(**overrides: Any) -> dict[str, Any]:
    return _task_of(
        {
            "kind": "paper",
            "title": "Draft the results section",
            "goal": "Write the results section from the accepted claim.",
            "query": "",
            "section": "results",
            "allowed_paths": ["paper/manuscript.md"],
        },
        overrides,
    )


def scripted(
    *,
    plan: dict[str, Any] | None = None,
    analyst: ScriptedResponse | None = None,
    coder: ScriptedResponse | None = None,
    reviewer: list[ScriptedResponse] | None = None,
    name: str = "fake",
    family: str = "fake-family",
) -> FakeProvider:
    """A provider scripted for a research run and anything it delegates."""

    from tests.automation_helpers import analysis_payload, review_payload

    return FakeProvider(
        name=name,
        family=family,
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=plan or plan_payload())],
            str(Role.ANALYST): [
                analyst or ScriptedResponse(structured=analysis_payload())
            ],
            str(Role.CODER): [
                coder
                or ScriptedResponse(
                    text="Implemented add in adder.py.",
                    write_files={"adder.py": FIXED_MODULE},
                )
            ],
            str(Role.REVIEWER): reviewer
            or [ScriptedResponse(structured=review_payload())],
        },
    )


def make_controller(
    provider: FakeProvider,
    *,
    config: AutomationConfig | None = None,
    experiments: ExperimentConfig | None = None,
) -> ResearchController:
    return ResearchController(
        providers={provider.name: provider},
        config=config or fake_config(),
        literature_config=offline_literature(),
        experiment_config=experiments
        if experiments is not None
        else ExperimentConfig(
            slurm=SlurmSettings(), limits=ExecutionLimits(), projects={}, source=None
        ),
    )
