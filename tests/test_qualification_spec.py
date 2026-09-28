"""The Research OS v1 qualification contract: frozen, complete, and decidable by code.

``src/research_os/portfolio/qualification_v1.yaml`` is the contract and
``research_os.portfolio.qualification`` evaluates it read-only. These tests
hold the freeze (the file's digest is pinned here, so no mandatory criterion
changes without this test changing), the shape (the 25 mandatory gates the
closure brief names, in order, and the advisory generalisation dimensions),
the freeze rule at evaluation time, and that the automated gates really are
decided from records -- by evaluating a real synthetic traversal.
"""

from __future__ import annotations

import json
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import qualification
from research_os.portfolio.config import load_config
from research_os.portfolio.models import IdeaOrigin
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.executors import LocalExecutor
from research_os.runtime.models import BudgetScope
from tests.portfolio_helpers import idea_fields, portfolio
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg
from tests.test_portfolio_empirical import (
    EMPIRICAL_FALSIFIER,
    EMPIRICAL_QUESTION,
    ThreeSourceLiterature,
    _empirical_router,
    _runtime_config,
    checkpoint_tables,
    declare_capabilities,
    project_repo,
)

__all__ = [
    "checkpoint_tables",
    "pg_dsn",
    "portfolio",
    "project_repo",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
]

#: The frozen specification's sha256. Changing a mandatory criterion changes
#: this, visibly; a live qualification records it at its start.
FROZEN_SPEC_SHA256 = "14627a4d86891dd9af6e5be12247dc5215812f7b654ac2cbded336681d8a0f3b"

#: The closure brief's 25 mandatory v1 gates, in its order.
MANDATORY = (
    "zero_state_environment",
    "unseeded_origination",
    "idea_provenance",
    "deduplication",
    "falsification",
    "executed_literature_retrieval",
    "curation_prioritization",
    "frozen_scientific_contract",
    "frozen_experimental_design",
    "capability_resolution",
    "frozen_execution_plan",
    "real_scientific_execution",
    "trusted_execution_receipt",
    "validated_result_artifact",
    "deterministic_primary_outcome",
    "real_replication",
    "trusted_replication_receipt",
    "replication_assessment",
    "complete_review_board",
    "meta_review_readiness",
    "recursive_child",
    "budget_authority",
    "repository_isolation",
    "containment",
    "complete_provenance_chain",
)


def test_the_specification_is_frozen_at_its_pinned_digest() -> None:
    assert qualification.spec_digest() == FROZEN_SPEC_SHA256, (
        "the v1 qualification specification changed. If this is deliberate and "
        "no live qualification has started under the old digest, update "
        "FROZEN_SPEC_SHA256 in the same commit, so the change is visible."
    )


def test_it_names_every_mandatory_gate_and_keeps_generalisation_advisory() -> None:
    spec = qualification.load_spec()
    assert tuple(gate["name"] for gate in spec["mandatory"]) == MANDATORY
    assert [gate["id"] for gate in spec["mandatory"]] == [
        f"Q{n:02d}" for n in range(1, 26)
    ]
    advisory = {gate["name"] for gate in spec["advisory"]}
    assert {"second_provider", "second_empirical_domain", "mathematical_track"} <= (
        advisory
    )
    assert not advisory & set(MANDATORY)
    assert spec["frozen"] is True


def test_every_automated_gate_is_decided_by_a_check_and_every_check_is_used() -> None:
    spec = qualification.load_spec()
    automated = {
        gate["name"]
        for tier in ("mandatory", "advisory")
        for gate in spec[tier]
        if gate["check"] in {"automated", "both"}
    }
    assert automated == set(qualification.CHECKS)


def test_a_specification_that_changed_since_the_start_is_refused(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    with pytest.raises(qualification.QualificationError, match="cannot change"):
        qualification.evaluate(
            runtime_db,
            project_id=runtime_project,
            artifacts=None,
            expect_spec_digest="0" * 64,
        )
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "00-zero-state.json").write_text(json.dumps({"spec_digest": "1" * 64}))
    with pytest.raises(qualification.QualificationError, match="recorded spec"):
        qualification.evaluate(
            runtime_db,
            project_id=runtime_project,
            artifacts=None,
            evidence_dir=evidence,
        )


def test_a_malformed_specification_is_refused(tmp_path: Path) -> None:
    import yaml

    spec = yaml.safe_load(qualification.SPEC_PATH.read_text())
    spec["mandatory"][1]["name"] = "a_gate_no_check_decides"
    path = tmp_path / "spec.yaml"
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(qualification.QualificationError, match="no check decides"):
        qualification.load_spec(path)
    spec = yaml.safe_load(qualification.SPEC_PATH.read_text())
    spec["frozen"] = False
    path.write_text(yaml.safe_dump(spec))
    with pytest.raises(qualification.QualificationError, match="frozen"):
        qualification.load_spec(path)


def test_an_empty_project_is_zero_state_and_qualifies_nothing(
    runtime_db: Database, runtime_project: str, tmp_path: Path
) -> None:
    snapshot = qualification.zero_state_snapshot(
        runtime_db, project_id=runtime_project, repo_path=None
    )
    assert snapshot["spec_digest"] == FROZEN_SPEC_SHA256
    assert set(snapshot["counts"]) == set(qualification.ZERO_STATE_COUNTS)
    assert not any(snapshot["counts"].values())
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "00-zero-state.json").write_text(json.dumps(snapshot))
    report = qualification.evaluate(
        runtime_db, project_id=runtime_project, artifacts=None, evidence_dir=evidence
    )
    assert report.verdict == "NOT_QUALIFIED"
    status = {gate.name: gate.passed for gate in report.gates}
    assert status["zero_state_environment"] is True
    assert sum(status[name] for name in MANDATORY) == 1, status  # the zero state
    assert status["repository_isolation"] is False, "its evidence file is missing"


def test_a_real_traversal_is_decided_gate_by_gate_from_its_records(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    checkpoint_tables: str,
    runtime_project: str,
    project_repo: Path,
    tmp_path: Path,
) -> None:
    """The same end-to-end route as the empirical control, then the contract.

    A zero-state snapshot first; an explicit budget a person set; the real
    stage machine through measurement, replication, board and meta-review;
    then the evaluator. What this synthetic run does not do -- buy stages
    through the allocator and publish a bank, open a follow-up child, supply a
    containment audit -- fails, by name, and nothing else does.
    """

    from research_os.portfolio.track import advance_idea
    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.store import RuntimeStore

    declare_capabilities(project_repo)
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    zero = qualification.zero_state_snapshot(
        runtime_db, project_id=runtime_project, repo_path=project_repo
    )
    assert not any(zero["counts"].values())
    (evidence / "00-zero-state.json").write_text(json.dumps(zero))
    BudgetLedger(runtime_db).set_limit(
        scope=BudgetScope.PROJECT,
        scope_id=runtime_project,
        dimension=Dimension.MODEL_COST_USD,
        limit_value=Decimal(20),
        explicit=True,
    )
    idea, _version = portfolio.create_idea(
        project_id=runtime_project,
        origin=IdeaOrigin.BLIND_EXPLORER,
        fields=idea_fields(
            research_question=EMPIRICAL_QUESTION, falsifier=EMPIRICAL_FALSIFIER
        ),
        origin_role="blind_explorer",
    )
    router = _empirical_router(runtime_db)
    config = _runtime_config(pg_dsn, tmp_path)
    for _ in range(30):
        result = advance_idea(
            runtime_config=config,
            portfolio_config=load_config(),
            db=runtime_db,
            project_id=runtime_project,
            idea_id=idea.idea_id,
            models=router,
            literature=ThreeSourceLiterature(),
            repo_path=project_repo,
            executors={"local": LocalExecutor()},
        )
        if result.stage is None or not result.ok:
            break
    (evidence / "50-isolation.txt").write_text(
        qualification.isolation_report(zero_state=zero, repo_path=project_repo)
    )
    report = qualification.evaluate(
        runtime_db,
        project_id=runtime_project,
        artifacts=FilesystemArtifactStore(
            config.artifacts_root, store=RuntimeStore(runtime_db)
        ),
        evidence_dir=evidence,
        repo_path=project_repo,
        expect_spec_digest=FROZEN_SPEC_SHA256,
    )
    status: dict[str, Any] = {gate.name: gate for gate in report.gates}
    failed = sorted(name for name in MANDATORY if not status[name].passed)
    detail = {name: status[name].detail for name in failed}
    assert failed == ["containment", "curation_prioritization", "recursive_child"], (
        detail
    )
    assert report.verdict == "NOT_QUALIFIED"
    assert "03-containment-audit.txt is missing" in status["containment"].detail
    assert status["complete_provenance_chain"].passed, status[
        "complete_provenance_chain"
    ].detail
    assert status["human_ready"].passed
