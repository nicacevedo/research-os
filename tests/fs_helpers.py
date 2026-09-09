"""Filesystem helpers for temporary Git repositories and capsules."""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import yaml

from research_os.capsule import CAPSULE_GITIGNORE, CHARTER_TEMPLATE, STATE_TEMPLATE
from research_os.models import Project


def make_git_repo(path: Path) -> Path:
    """Create an empty Git repository at ``path``."""

    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        ["git", "init"],
        cwd=path,
        check=True,
        capture_output=True,
        text=True,
    )
    return path


def write_yaml(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    dumped = yaml.safe_dump(
        data,
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    path.write_text(dumped, encoding="utf-8")


def write_bytes(path: Path, data: bytes) -> None:
    """Write raw bytes, for fixtures that are deliberately not valid UTF-8.

    ``write_text`` cannot express these: it always encodes UTF-8. Do not pass a
    repository containing such a file to ``snapshot_files``, which reads every
    file as UTF-8 text.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def hypothesis_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "HYP-0001",
        "type": "hypothesis",
        "schema_version": 1,
        "status": "active",
        "title": "X holds above 200 K",
        "statement": "X holds for the studied material above 200 K.",
        "falsification": "Observing not-X above 200 K in a controlled measurement.",
        "addresses": ["Q-0001"],
    }
    data.update(updates)
    return data


def write_reviewable_capsule(
    git_root: Path,
    *,
    claim_status: str = "evidence_linked",
    project_id: str | None = None,
) -> Project:
    """Write a research-shaped capsule whose Claim is ready for review.

    Three linked Evidence objects, deliberately covering every source pointer a
    reviewer must be able to follow: a literature item with citation +
    global_ref + locator, an experiment-derived item pointing at a completed
    Experiment, and a contrary literature item. The Claim therefore binds three
    evidence digests, which is what makes coverage worth asserting.
    """

    project = write_minimal_capsule(git_root, project_id=project_id)
    capsule = git_root / ".research"
    write_yaml(
        capsule / "questions" / "Q-0001.yaml",
        question_data(
            title="Does X hold above 200 K?",
            statement="Whether property X holds in the regime above 200 K.",
        ),
    )
    write_yaml(capsule / "hypotheses" / "HYP-0001.yaml", hypothesis_data())
    write_yaml(
        capsule / "experiments" / "EXP-0001" / "manifest.yaml",
        experiment_data(
            status="completed",
            title="Calibrated probe sweep",
            purpose="Measure X between 180 K and 260 K.",
            hypotheses=["HYP-0001"],
            predictions=[
                {
                    "hypothesis": "HYP-0001",
                    "predicted_outcome": "X is observed above 200 K.",
                    "discriminates": True,
                }
            ],
            primary_metrics=["x_amplitude"],
            decision_rule="Reject the hypothesis if x_amplitude stays below 0.1.",
            provenance={
                "code": "src/sweep.py",
                "config": "configs/sweep.toml",
                "data": "institutional-store://runs/2026-09",
                "git_commit": "deadbeef",
            },
        ),
    )
    write_yaml(
        capsule / "evidence" / "EVI-0001.yaml",
        evidence_data(
            title="Literature report of X above 200 K",
            statement="The source reports X above 200 K.",
            citation="Doe et al., J. Example Phys. 12, 345 (2024)",
            global_ref="doi:10.1000/example.12.345",
            locator="Fig. 3",
        ),
    )
    write_yaml(
        capsule / "evidence" / "EVI-0002.yaml",
        evidence_data(
            id="EVI-0002",
            title="Contrary report near 210 K",
            statement="The source reports weak not-X near 210 K.",
            citation="Roe et al., J. Example Phys. 13, 1 (2025)",
            locator="Table 2",
        ),
    )
    experiment_evidence = evidence_data(
        id="EVI-0003",
        kind="experiment",
        title="Bench measurement at 240 K",
        statement="Measured X at 240 K with the calibrated probe.",
        experiment="EXP-0001",
    )
    del experiment_evidence["citation"]
    write_yaml(capsule / "evidence" / "EVI-0003.yaml", experiment_evidence)
    write_yaml(
        capsule / "claims" / "CLAIM-0001.yaml",
        claim_data(
            status=claim_status,
            title="X holds above 200 K",
            statement="X holds above 200 K for the studied material.",
            supporting_evidence=["EVI-0001", "EVI-0003"],
            contrary_evidence=["EVI-0002"],
            contrary_evidence_addressed=(
                "The contrary report used an uncalibrated probe."
            ),
            hypotheses=["HYP-0001"],
        ),
    )
    return project


def snapshot_files(root: Path) -> dict[str, str]:
    """Return a deterministic map of relative posix paths to file text."""

    snapshot: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        if path.is_symlink():
            snapshot[rel] = f"symlink:{path.readlink().as_posix()}"
        else:
            snapshot[rel] = path.read_text(encoding="utf-8")
    return snapshot


def write_minimal_capsule(
    git_root: Path,
    *,
    project_id: str | None = None,
    title: str | None = None,
) -> Project:
    """Write a valid capsule without going through init-project."""

    capsule = git_root / ".research"
    capsule.mkdir()
    project = Project.model_validate(
        {
            "id": project_id or git_root.name,
            "title": title or git_root.name,
            "capsule_version": 1,
            "status": "active",
        }
    )
    write_text(capsule / ".gitignore", CAPSULE_GITIGNORE)
    write_yaml(
        capsule / "project.yaml",
        {
            "id": project.id,
            "title": project.title,
            "capsule_version": 1,
            "status": "active",
        },
    )
    write_text(capsule / "CHARTER.md", CHARTER_TEMPLATE)
    write_text(capsule / "STATE.md", STATE_TEMPLATE)
    return project


def question_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "Q-0001",
        "type": "question",
        "schema_version": 1,
        "status": "open",
        "title": "A question",
        "statement": "Why does this happen?",
    }
    data.update(updates)
    return data


def claim_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "CLAIM-0001",
        "type": "claim",
        "schema_version": 1,
        "status": "draft",
        "title": "A claim",
        "statement": "X is supported.",
    }
    data.update(updates)
    return data


def evidence_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "EVI-0001",
        "type": "evidence",
        "schema_version": 1,
        "status": "active",
        "title": "Evidence",
        "statement": "The source supports X.",
        "kind": "literature",
        "citation": "Smith 2020",
    }
    data.update(updates)
    return data


def idea_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "IDEA-0001",
        "type": "idea",
        "schema_version": 1,
        "status": "draft",
        "title": "An idea",
        "statement": "Perhaps a mechanism exists.",
    }
    data.update(updates)
    return data


def experiment_data(**updates: Any) -> dict[str, Any]:
    data: dict[str, Any] = {
        "id": "EXP-0001",
        "type": "experiment",
        "schema_version": 1,
        "status": "draft",
        "title": "An experiment",
        "purpose": "Test the hypothesis.",
    }
    data.update(updates)
    return data
