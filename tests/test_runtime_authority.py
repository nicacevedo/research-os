"""The runtime cannot manufacture scientific acceptance.

These tests are the reason the architecture is arranged the way it is. The
runtime is meant to reach near-total *operational* autonomy -- scheduling,
retrying, recovering, submitting, polling, collecting, routing, continuing --
and to remain structurally unable to decide that a claim is true.

Two kinds of assertion, and both are needed. The behavioural ones check that the
adapter reports what the kernel says. The structural ones inspect the source of
the whole ``research_os.runtime`` package, because a behavioural test only
covers the path it calls, and the property being protected is "there is no such
path anywhere".
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from research_os.models import ClaimStatus
from research_os.runtime.kernel import ScientificAuthorityError, ScientificKernelAdapter
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    hypothesis_data,
    make_git_repo,
    question_data,
    write_minimal_capsule,
    write_yaml,
)

RUNTIME_DIR = Path(__file__).resolve().parents[1] / "src" / "research_os" / "runtime"


def _runtime_sources() -> list[Path]:
    found = sorted(RUNTIME_DIR.rglob("*.py"))
    assert found, "no runtime sources found; the path in this test is wrong"
    return found


# ---------------------------------------------------------------- structural --
def test_no_runtime_module_writes_a_human_review() -> None:
    """Only `researchctl review` may author a human Review. Agents may not.

    ``AGENTS.md`` states the policy; this asserts the code cannot break it by
    accident. ``write_review`` and ``build_review`` are the kernel's writers,
    and nothing under ``research_os/runtime`` may call either.
    """

    forbidden = {"write_review", "build_review"}
    offenders: list[str] = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "research_os.review":
                offenders.extend(
                    f"{path.name}: imports {alias.name}"
                    for alias in node.names
                    if alias.name in forbidden
                )
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(
                    node.func, "attr", None
                )
                if name in forbidden:
                    offenders.append(f"{path.name}:{node.lineno}: calls {name}()")
    assert offenders == [], (
        "the runtime must never author a human Review: " + "; ".join(offenders)
    )


def test_no_runtime_module_reimplements_the_acceptance_rule() -> None:
    """There must be exactly one definition of what "accepted" means.

    A second implementation would eventually disagree with the kernel's, and the
    one that disagreed quietly would be the one that let an unapproved claim be
    quoted. The runtime is allowed to *call* ``claim_approval``; it is not
    allowed to define anything that looks like it.
    """

    offenders: list[str] = []
    for path in _runtime_sources():
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef) and node.name in {
                "claim_approval",
                "is_qualifying_evidence",
                "claim_is_accepted",
            }:
                offenders.append(f"{path.name}:{node.lineno}: defines {node.name}()")
    assert offenders == [], "; ".join(offenders)


def test_only_the_kernel_adapter_reaches_the_capsule() -> None:
    """One door, so "could the runtime have faked this" is one module to read."""

    allowed = {"kernel.py"}
    capsule_modules = {
        "research_os.capsule",
        "research_os.validate",
        "research_os.review",
    }
    offenders: list[str] = []
    for path in _runtime_sources():
        if path.name in allowed:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module in capsule_modules:
                offenders.append(f"{path.name}:{node.lineno}: imports {node.module}")
    assert offenders == [], (
        "scientific state must be reached only through runtime/kernel.py: "
        + "; ".join(offenders)
    )


def test_the_kernel_adapter_exposes_no_write_method() -> None:
    public = [name for name in dir(ScientificKernelAdapter) if not name.startswith("_")]
    writers = [
        name
        for name in public
        if any(
            token in name
            for token in (
                "write",
                "save",
                "accept",
                "approve",
                "record",
                "promote",
                "set_",
            )
        )
    ]
    assert writers == [], f"the adapter must be read-only, but exposes {writers}"


# --------------------------------------------------------------- behavioural --
@pytest.fixture
def capsule(tmp_path: Path) -> Path:
    """A capsule with an accepted claim that has no qualifying human review."""

    repo = make_git_repo(tmp_path / "project")
    write_minimal_capsule(repo, project_id="alpha-project")
    research = repo / ".research"
    (research / "questions").mkdir()
    (research / "hypotheses").mkdir()
    (research / "claims").mkdir()
    (research / "evidence").mkdir()
    write_yaml(research / "questions" / "Q-0001.yaml", question_data())
    write_yaml(
        research / "hypotheses" / "HYP-0001.yaml",
        hypothesis_data(status="active", addresses=["Q-0001"]),
    )
    write_yaml(research / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        research / "claims" / "CLAIM-0001.yaml",
        claim_data(status="accepted", supporting_evidence=["EVI-0001"]),
    )
    return repo


def test_an_accepted_claim_without_a_human_review_is_not_quotable(
    capsule: Path,
) -> None:
    """The file says accepted. The kernel says no approval stands. The kernel wins."""

    adapter = ScientificKernelAdapter(capsule)
    claim = adapter.object("CLAIM-0001")
    assert claim.status is ClaimStatus.ACCEPTED
    assert adapter.approval("CLAIM-0001").qualifies is False
    assert adapter.quotable_claims() == ()


def test_the_frontier_is_derived_from_files_with_no_model_call(capsule: Path) -> None:
    frontier = ScientificKernelAdapter(capsule).frontier()
    assert frontier.project_id == "alpha-project"
    assert "Q-0001" in frontier.open_questions
    assert "HYP-0001" in frontier.actionable_hypotheses
    assert "HYP-0001" in frontier.hypotheses_without_tests
    assert "CLAIM-0001" in frontier.claims_with_stale_review
    assert frontier.empty is False


def test_an_empty_frontier_is_expressible(tmp_path: Path) -> None:
    """A frontier that is never empty is a runtime that never stops."""

    repo = make_git_repo(tmp_path / "quiet")
    write_minimal_capsule(repo, project_id="quiet-project")
    frontier = ScientificKernelAdapter(repo).frontier()
    assert frontier.empty is True
    assert frontier.summary()["open_questions"] == 0


def test_refusing_scientific_authority_says_what_to_do_instead(capsule: Path) -> None:
    adapter = ScientificKernelAdapter(capsule)
    with pytest.raises(ScientificAuthorityError, match="researchctl review"):
        adapter.refuse_scientific_authority("Accepting CLAIM-0001")
