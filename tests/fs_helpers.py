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


def write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


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
