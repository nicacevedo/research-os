"""A throwaway paper project: real capsule, real accepted science, real manuscript.

The fixture builds a project with a genuinely accepted Claim -- accepted the way
R0 requires, with a concluded human approve Review binding the claim's digest,
its evidence digests, and its experiment digests -- because a packet builder
tested against a faked acceptance would be testing nothing. It also builds a
Claim whose approval has gone *stale*, which is the case the packet must withhold.
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
from research_os.capsule import init_project, validate_project
from research_os.digests import subject_digest
from research_os.models import Reviewable
from research_os.proposal.promote import EXPERIMENT_MANIFEST, TYPE_TO_DIRECTORY

PROJECT_ID = "widget-paper"

MANUSCRIPT = """\
# Widget deformation under load

## Abstract

(to be written)

## Results

(to be written)

## Limitations

(to be written)
"""


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    )


def write_object(project: Path, payload: dict[str, Any]) -> Path:
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


def science_objects() -> list[dict[str, Any]]:
    """A hypothesis, an experiment, supporting and contrary evidence, two claims.

    Deliberately includes contrary evidence on the accepted claim: a writer
    omitting it is the failure the checks exist to catch, and a fixture without
    any could not exercise that.
    """

    return [
        {
            "id": "Q-0001",
            "type": "question",
            "schema_version": 1,
            "status": "open",
            "title": "How does widget deformation scale with load?",
            "statement": "Whether deformation stays linear above 10N is unsettled.",
        },
        {
            "id": "HYP-0001",
            "type": "hypothesis",
            "schema_version": 1,
            "status": "supported",
            "title": "Deformation is linear below 10N",
            "statement": "Widget deformation scales linearly with load below 10N.",
            "falsification": "Observe a non-linear response below 10N.",
            "addresses": ["Q-0001"],
            "supporting_evidence": ["EVI-0001"],
        },
        {
            "id": "EXP-0001",
            "type": "experiment",
            "schema_version": 1,
            "status": "completed",
            "title": "Bench loading sweep",
            "purpose": "Measure deformation at 2N, 5N, and 9N.",
            "hypotheses": ["HYP-0001"],
            "predictions": [
                {
                    "hypothesis": "HYP-0001",
                    "predicted_outcome": "Deformation per newton is constant to 3 percent.",
                    "discriminates": True,
                }
            ],
            "primary_metrics": ["deformation_mm"],
            "decision_rule": (
                "Reject linearity if deformation per newton varies by more than "
                "3 percent across the sweep."
            ),
            "provenance": {
                "code": "analysis/sweep.py",
                "config": "configs/sweep.toml",
                "data": "bench-run-2026-03",
                "git_commit": "abc1234",
            },
        },
        {
            "id": "EVI-0001",
            "type": "evidence",
            "schema_version": 1,
            "status": "active",
            "title": "Sweep deformation is constant per newton",
            "statement": (
                "Across 2N, 5N, and 9N deformation per newton varied by 1.4 percent."
            ),
            "kind": "experiment",
            "experiment": "EXP-0001",
        },
        {
            "id": "EVI-0002",
            "type": "evidence",
            "schema_version": 1,
            "status": "active",
            "title": "One outlier widget deformed 9 percent more at 9N",
            "statement": (
                "Widget 7 deformed 9 percent more than the others at 9N and was "
                "not excluded."
            ),
            "kind": "other",
            "notes": "Recorded during the same bench session.",
        },
    ]


def accepted_claim() -> dict[str, Any]:
    return {
        "id": "CLAIM-0001",
        "type": "claim",
        "schema_version": 1,
        "status": "accepted",
        "title": "Deformation is linear below 10N in this bench setup",
        "statement": (
            "In this bench setup, widget deformation is linear in applied load "
            "below 10N."
        ),
        "hypotheses": ["HYP-0001"],
        "supporting_evidence": ["EVI-0001"],
        "contrary_evidence": ["EVI-0002"],
        "contrary_evidence_addressed": (
            "Widget 7 was retained; excluding it does not change the conclusion."
        ),
    }


def draft_claim() -> dict[str, Any]:
    return {
        "id": "CLAIM-0002",
        "type": "claim",
        "schema_version": 1,
        "status": "draft",
        "title": "Linearity extends above 10N",
        "statement": "Deformation remains linear above 10N.",
        "hypotheses": ["HYP-0001"],
    }


def init_paper_project(
    path: Path,
    *,
    stale_approval: bool = False,
) -> Path:
    """Build a project whose accepted Claim is accepted the way R0 requires.

    ``stale_approval`` writes the Review against the *wrong* digest, which is
    exactly what happens when science changes after it was approved. The packet
    must withhold that Claim, and a fixture that could not produce the state
    could not test it.
    """

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    (path / "README.md").write_text("# widget paper\n", encoding="utf-8")
    (path / "paper").mkdir(exist_ok=True)
    (path / "paper" / "manuscript.md").write_text(MANUSCRIPT, encoding="utf-8")
    _git(["add", "-A"], path)
    _git(["commit", "-m", "initial"], path)

    init_project(path, project_id=PROJECT_ID, title="Widget paper")
    for payload in [*science_objects(), accepted_claim(), draft_claim()]:
        write_object(path, payload)

    # The Review has to bind digests that only exist once the objects do, so it
    # is written in a second pass from what the capsule actually holds.
    report = validate_project(path)
    by_id = {item.id: item for item in report.objects}
    claim = by_id["CLAIM-0001"]
    wrong = "v1:" + "0" * 64
    write_object(
        path,
        {
            "id": "REV-0001",
            "type": "review",
            "schema_version": 1,
            "status": "concluded",
            "title": "Human approval of CLAIM-0001",
            "subject": "CLAIM-0001",
            "reviewer_kind": "human",
            "verdict": "approve",
            "findings": (
                "Checked the sweep data and the outlier. The conclusion holds "
                "for this bench setup and is not claimed beyond it."
            ),
            "subject_digest": (
                wrong
                if stale_approval
                else subject_digest(claim, project_id=PROJECT_ID)
            ),
            "evidence_digests": {
                item: _digest(by_id[item]) for item in ("EVI-0001", "EVI-0002")
            },
            "experiment_digests": {"EXP-0001": _digest(by_id["EXP-0001"])},
        },
    )
    _git(["add", "-A"], path)
    _git(["commit", "-m", "capsule"], path)
    return path


def _digest(item: object) -> str:
    assert isinstance(item, Reviewable)
    return subject_digest(item, project_id=PROJECT_ID)


def fake_config(
    *,
    coder: str = "fake",
    reviewer: str = "fake",
    budget: Budget | None = None,
) -> AutomationConfig:
    return AutomationConfig(
        roles={
            "planner": RoleSetting(
                provider="fake",
                model="fake-planner",
                read_only=True,
                access=Access.CONTEXT_ONLY,
                tools=[],
            ),
            "coder": RoleSetting(
                provider=coder,
                model="fake-writer",
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


def manifest_payload(
    *,
    claim_ids: list[str] | None = None,
    evidence_ids: list[str] | None = None,
    experiment_ids: list[str] | None = None,
    citation_keys: list[str] | None = None,
    caveats: list[str] | None = None,
    written_paths: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "claim_ids": claim_ids if claim_ids is not None else ["CLAIM-0001"],
        "evidence_ids": evidence_ids
        if evidence_ids is not None
        else ["EVI-0001", "EVI-0002"],
        "experiment_ids": experiment_ids
        if experiment_ids is not None
        else ["EXP-0001"],
        "citation_keys": citation_keys if citation_keys is not None else [],
        "unresolved_caveats": caveats
        if caveats is not None
        else ["the outlier widget was retained rather than explained"],
        "written_paths": written_paths
        if written_paths is not None
        else ["paper/manuscript.md"],
    }


GROUNDED_RESULTS = """\
# Widget deformation under load

## Abstract

(to be written)

## Results

In this bench setup, widget deformation is linear in applied load below 10N
(CLAIM-0001). Across the loading sweep, deformation per newton varied by 1.4
percent (EVI-0001, EXP-0001). One widget deformed 9 percent more than the others
at the highest load and was retained rather than excluded (EVI-0002); excluding
it does not change the conclusion.

## Limitations

(to be written)
"""

OVERREACHING_RESULTS = """\
# Widget deformation under load

## Results

Widget deformation is linear in applied load (CLAIM-0001), and the effect
generalises to every widget geometry we are aware of [@smith2019fabricated].
Deformation per newton varied by only 0.3 percent across the sweep.
"""

OMITS_CONTRARY = """\
# Widget deformation under load

## Results

In this bench setup, widget deformation is linear in applied load below 10N
(CLAIM-0001). Across the loading sweep, deformation per newton varied by 1.4
percent (EVI-0001).
"""
