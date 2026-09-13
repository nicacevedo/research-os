"""Builders for proposal tests: real capsules, real stores, fake providers only.

Every helper here produces a genuine Git repository with a genuine Research
Capsule, so the tests exercise the same validation, the same digests, and the
same promotion path a researcher would. What is faked is the model.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

from research_os.automation.config import (
    DEFAULT_ALLOWED_CHECK_PROGRAMS,
    AutomationConfig,
)
from research_os.automation.models import Access, Budget, RoleSetting
from research_os.capsule import init_project
from research_os.proposal.controller import ProposalController


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def init_capsule_project(
    path: Path,
    *,
    project_id: str = "widget-study",
    objects: list[dict[str, Any]] | None = None,
    charter: str = "# Charter\n\nUnderstand how widgets deform under load.\n",
    state: str = "# State\n\nOne open question, one draft hypothesis.\n",
) -> Path:
    """Create a committed Git project with a real capsule and some science in it."""

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    (path / "README.md").write_text("# widget study\n", encoding="utf-8")
    (path / "analysis.py").write_text(
        "def deformation(load):\n    return load\n", "utf-8"
    )
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)

    init_project(path, project_id=project_id, title="Widget study")
    (path / ".research" / "CHARTER.md").write_text(charter, encoding="utf-8")
    (path / ".research" / "STATE.md").write_text(state, encoding="utf-8")
    for payload in objects if objects is not None else default_objects():
        write_object(path, payload)
    _git(["add", "-A"], path)
    _git(["commit", "-m", "capsule"], path)
    return path


def write_object(project: Path, payload: dict[str, Any]) -> Path:
    """Write one capsule object where the capsule layout expects it."""

    from research_os.proposal.promote import EXPERIMENT_MANIFEST, TYPE_TO_DIRECTORY

    directory = project / ".research" / TYPE_TO_DIRECTORY[payload["type"]]
    if payload["type"] == "experiment":
        directory = directory / payload["id"]
        target = directory / EXPERIMENT_MANIFEST
    else:
        target = directory / f"{payload['id']}.yaml"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return target


def default_objects() -> list[dict[str, Any]]:
    """A small but real capsule: one open question, one draft hypothesis."""

    return [
        {
            "id": "Q-0001",
            "type": "question",
            "schema_version": 1,
            "status": "open",
            "title": "How does widget deformation scale with load?",
            "statement": "We do not know whether deformation is linear above 10N.",
        },
        {
            "id": "HYP-0001",
            "type": "hypothesis",
            "schema_version": 1,
            "status": "draft",
            "title": "Deformation is linear below 10N",
            "statement": "Widget deformation scales linearly with load below 10N.",
            "addresses": ["Q-0001"],
        },
    ]


def fake_config(
    *,
    planner: str = "fake",
    literature: str = "fake",
    reviewer: str = "fake",
    coder: str = "fake",
    budget: Budget | None = None,
) -> AutomationConfig:
    """Return a configuration whose read-only roles are bound to test doubles."""

    return AutomationConfig(
        roles={
            "planner": RoleSetting(
                provider=planner,
                model="fake-planner",
                read_only=True,
                access=Access.CONTEXT_ONLY,
                tools=[],
            ),
            "literature": RoleSetting(
                provider=literature,
                model="fake-literature",
                read_only=True,
                access=Access.CONTEXT_ONLY,
                tools=[],
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


def make_controller(
    providers: dict[str, Any],
    *,
    config: AutomationConfig | None = None,
    literature_store: Any = None,
) -> ProposalController:
    from research_os.literature.config import LiteratureConfig

    return ProposalController(
        providers=providers,
        config=config or fake_config(),
        literature_config=LiteratureConfig(
            contact_email="tests@example.invalid",
            enabled_sources=(),
            offline=True,
            default_search_limit=5,
            fetch_fulltext=False,
            source=None,
        ),
        literature_store=literature_store,
    )


def item(
    *,
    item_id: str = "PR-001",
    kind: str = "hypothesis",
    basis: str = "prospective",
    **overrides: Any,
) -> dict[str, Any]:
    """Return a schema-conforming proposed item."""

    payload: dict[str, Any] = {
        "item_id": item_id,
        "kind": kind,
        "title": "Deformation is sublinear above 10N",
        "statement": "Widget deformation grows more slowly than load above 10N.",
        "rationale": "The open question asks exactly this and nothing settles it.",
        "basis": basis,
        "addresses": ["Q-0001"],
        "grounded_in_literature": [],
        "grounded_in_findings": [],
        "falsification": "Observe linear deformation at 15N and 20N.",
        "expected_direction": "Deformation per newton falls above 10N.",
        "primary_metrics": ["deformation_mm"],
        "decision_rule": "Reject if the slope above 10N is within 5% of the slope below.",
        "required_inputs": ["bench measurements at 5N, 10N, 15N, 20N"],
        "required_code": ["analysis.py"],
        "runtime_class": "minutes",
        "resource_class": "laptop",
        "risks": ["the rig may not reach 20N"],
        "importance": "high",
        "confidence": "medium",
    }
    payload.update(overrides)
    return payload


def proposal_payload(
    *,
    items: list[dict[str, Any]] | None = None,
    uncertainties: list[dict[str, Any]] | None = None,
    next_actions: list[dict[str, Any]] | None = None,
    summary: str = "One hypothesis and one experiment would settle Q-0001.",
) -> dict[str, Any]:
    """Return a schema-conforming proposal payload."""

    return {
        "summary": summary,
        "items": items if items is not None else [item()],
        "uncertainties": uncertainties
        if uncertainties is not None
        else [
            {
                "statement": "The rig's maximum load is not recorded anywhere.",
                "what_would_settle_it": "Check the rig specification.",
                "blocks": [],
            }
        ],
        "next_actions": next_actions
        if next_actions is not None
        else [
            {
                "action": "Decide whether the 20N measurement is worth the rig time.",
                "kind": "human_decision",
                "rationale": "It is the only measurement that discriminates.",
                "addresses_items": ["PR-001"],
                "requires_human": True,
            }
        ],
    }


def assessment_payload(
    verdict: str = "PASS",
    *,
    findings: list[dict[str, str]] | None = None,
    summary: str | None = None,
) -> dict[str, Any]:
    return {
        "verdict": verdict,
        "summary": summary or f"scripted {verdict}",
        "findings": findings or [],
    }
