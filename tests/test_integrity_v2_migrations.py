"""The second integrity migrations (0043-0046) as upgrades over live data.

Two starting points, because two kinds of deployment exist: one still on the
schema the qualification runs used (0036), and one on the first integrity
round's (0042). Each is built with explicit SQL naming only the columns that
schema had, holding the rows that round's code wrote, then migrated to the
head. What each legacy row must mean afterwards -- failing closed wherever
its history is ambiguous:

- **replication evidence** (0046) -- an executed replication row written
  before trusted receipts gets a ``legacy`` assessment, is kept, and cannot
  satisfy the current replication gate (the independent review's
  ``LEGACY_REPLICATION``);
- **replication manifests** (0046) -- a pointer already set on an
  interpreted replication is frozen;
- **reviews and objections** (0044) -- a review whose objection set may be
  incomplete is kept and is never live; its objections keep standing; a
  review with severity NONE and no objections is complete;
- **parks** (0045) -- a park with no structural reason becomes
  ``legacy_unknown`` and no ceiling revives it (``LEGACY_BUDGET_PARK``);
- **budget reservations** (0043) -- a legacy submitted reservation keeps its
  null cap and is still charged in full by the reconciler; nothing new can
  be submitted without a cap.
"""

from __future__ import annotations

from decimal import Decimal

import pytest

from research_os.portfolio import digests as pdigests
from research_os.portfolio import gates, provenance
from research_os.portfolio.config import load_config
from research_os.portfolio.models import (
    AdjudicationType,
    ExperimentRole,
    IdeaStatus,
    ParkReason,
    Severity,
)
from research_os.portfolio.store import PortfolioStore
from research_os.portfolio.tick import _revive_budget_parks
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.migrations import discover, migrate
from research_os.runtime.models import BudgetScope
from tests.runtime_helpers import pg_dsn, throwaway_dsn

__all__ = ["pg_dsn", "throwaway_dsn"]

IDEA = "PIDEA-19700101T000000Z-legacy10"
PARK = "PIDEA-19700101T000000Z-legacy11"
MODEL_PARK = "PIDEA-19700101T000000Z-legacy12"


def _apply_up_to(db: Database, last: str) -> None:
    with db.tx() as conn:
        for migration in discover():
            if migration.version > last:
                break
            conn.execute(migration.sql)
            conn.execute(
                "insert into schema_migrations (version, checksum) values (%s, %s)",
                (migration.version, migration.checksum),
            )


def _legacy_rows(db: Database, *, schema: str) -> None:
    """What a deployment on ``schema`` holds, written with that schema's columns."""

    with db.tx() as conn:
        conn.execute(
            "insert into projects (project_id, repo_path) values ('old', '/tmp/old')"
        )
        for idea_id, status, reason, revisit in (
            (IDEA, "VALIDATED", None, None),
            (
                PARK,
                "PARKED",
                (
                    "spent 3.00 USD of its idea ceiling of 3.00 USD, and its next "
                    "stage (review_board) may cost 0.60 USD a call"
                ),
                # The tick's exact sentence -- which a model could also write.
                "a person raises bounds.idea_spend_ceiling_usd",
            ),
            (
                MODEL_PARK,
                "PARKED",
                "could not be made precise",
                "room in its lineage to branch; resumes as PROMISING",
            ),
        ):
            conn.execute(
                "insert into ideas (idea_id, project_id, lineage_root, origin, status, "
                "retire_reason, revisit_if) values (%s, 'old', %s, 'BLIND_EXPLORER', "
                "%s, %s, %s)",
                (idea_id, idea_id, status, reason, revisit),
            )
            conn.execute(
                "insert into idea_versions (idea_id, version, title, research_question, "
                "core_idea, content_digest, canonical_digest, origin_role, "
                "adjudication_types) values (%s, 1, 't', 'q', 'c', %s, %s, "
                "'blind_explorer', %s)",
                (
                    idea_id,
                    f"pidea-content-v1:{idea_id}",
                    f"pidea-canonical-v1:{idea_id}",
                    ["empirical"],
                ),
            )
        conn.execute(
            "insert into model_calls (call_id, provider, role) values "
            "('MCALL-old-primary', 'claude', 'experimentalist'), "
            "('MCALL-old-replication', 'claude', 'replicator'), "
            "('MCALL-old-review-1', 'codex', 'methodology_reviewer'), "
            "('MCALL-old-review-2', 'codex', 'skeptic_reviewer'), "
            "('MCALL-old-review-3', 'codex', 'novelty_reviewer')"
        )
        conn.execute(
            "insert into external_jobs (job_id, project_id, executor, spec_digest, "
            "run_dir, status) values "
            "('XJOB-old-primary', 'old', 'local', 'sp1', '/tmp/p', 'COMPLETED'), "
            "('XJOB-old-replication', 'old', 'local', 'sp2', '/tmp/r', 'COMPLETED')"
        )
        conn.execute(
            "insert into artifacts (artifact_id, size_bytes) values "
            "('old-analysis-1', 1), ('old-analysis-2', 1), ('old-manifest', 1)"
        )
        conn.execute(
            "insert into idea_evidence (evidence_id, idea_id, idea_version, kind, "
            "strength, summary, job_id, source_call_id, artifact_id) values "
            "('IEVD-old-primary', %s, 1, 'experiment', 'SUPPORTS', 'primary', "
            "'XJOB-old-primary', 'MCALL-old-primary', 'old-analysis-1'), "
            "('IEVD-old-replication', %s, 1, 'replication', 'SUPPORTS', "
            "'replication', 'XJOB-old-replication', 'MCALL-old-replication', "
            "'old-analysis-2')",
            (IDEA, IDEA),
        )
        conn.execute(
            "insert into idea_experiments (experiment_id, idea_id, idea_version, "
            "project_id, role, state, command, spec_digest, variation_digest, "
            "workspace_path, no_rule_reason, job_id, analysis_artifact_id, "
            "conclusion, evidence_id, origin_call_id) values "
            "('PEXP-old-primary', %s, 1, 'old', 'PRIMARY', 'INTERPRETED', 'measure', "
            "'sp1', 'var1', '/tmp/p', 'old rule', 'XJOB-old-primary', "
            "'old-analysis-1', 'SUPPORTS', 'IEVD-old-primary', 'MCALL-old-primary'), "
            "('PEXP-old-replication', %s, 1, 'old', 'REPLICATION', 'INTERPRETED', "
            "'measure', 'sp2', 'var2', '/tmp/r', 'old rule', 'XJOB-old-replication', "
            "'old-analysis-2', 'SUPPORTS', 'IEVD-old-replication', "
            "'MCALL-old-replication')",
            (IDEA, IDEA),
        )
        if schema >= "0041":
            conn.execute(
                "update idea_experiments set execution_manifest_artifact_id = "
                "'old-manifest' where experiment_id = 'PEXP-old-replication'"
            )
        # Three reviews of one version: a complete PASS, and two whose
        # objection sets the old code could have lost -- one FATAL with its
        # objection folded into another review's MINOR row, one MAJOR with
        # nothing at all (a crash between the two writes).
        for review_id, call, role, verdict, severity in (
            ("IREV-old-pass", "MCALL-old-review-3", "novelty_reviewer", "PASS", "NONE"),
            (
                "IREV-old-minor",
                "MCALL-old-review-1",
                "methodology_reviewer",
                "PASS_WITH_OBJECTIONS",
                "MINOR",
            ),
            (
                "IREV-old-fatal",
                "MCALL-old-review-2",
                "skeptic_reviewer",
                "PASS_WITH_OBJECTIONS",
                "FATAL",
            ),
        ):
            conn.execute(
                "insert into idea_reviews (review_id, idea_id, idea_version, "
                "reviewer_role, verdict, severity, summary, reviewed_content_digest, "
                "reviewed_evidence_digest, packet_digest, prompt_version, call_id, "
                "provider, provider_family, independence_vs_origin, context_class) "
                "values (%s, %s, 1, %s, %s, %s, 's', %s, %s, 'p', %s, %s, 'codex', "
                "'openai', 'different_family', 'FROZEN_PACKET')",
                (
                    review_id,
                    IDEA,
                    role,
                    verdict,
                    severity,
                    f"pidea-content-v1:{IDEA}",
                    pdigests.evidence_set_digest(
                        ["IEVD-old-primary", "IEVD-old-replication"]
                    ),
                    f"{role}@1",
                    call,
                ),
            )
        conn.execute(
            "insert into idea_objections (objection_id, idea_id, raised_in_review, "
            "raised_at_version, objection_key, severity, summary) values "
            "('IOBJ-old', %s, 'IREV-old-minor', 1, 'k-same', 'MINOR', 'same flaw')",
            (IDEA,),
        )
        conn.execute(
            "insert into budgets (budget_id, scope, scope_id, dimension, limit_value, "
            "reserved) values ('BDGT-old', 'project', 'old', 'model_cost_usd', 1, 0.6)"
        )
        if schema >= "0037":
            conn.execute(
                "insert into budget_reservations (reservation_id, budget_id, amount, "
                "created_at, submitted_at) values ('RSV-old', 'BDGT-old', 0.6, "
                "now() - interval '3 hours', now() - interval '3 hours')"
            )
        else:
            conn.execute(
                "insert into budget_reservations (reservation_id, budget_id, amount, "
                "created_at) values ('RSV-old', 'BDGT-old', 0.6, "
                "now() - interval '3 hours')"
            )


@pytest.mark.parametrize("schema", ["0036", "0042"])
def test_legacy_state_fails_closed_after_upgrading_to_the_head(
    throwaway_dsn: str, schema: str
) -> None:
    with Database(throwaway_dsn) as db:
        _apply_up_to(db, schema)
        _legacy_rows(db, schema=schema)
        applied = migrate(db)
        assert applied == tuple(m.version for m in discover() if m.version > schema)
        assert applied[-4:] == ("0043", "0044", "0045", "0046")
        store = PortfolioStore(db)

        # ---- replication evidence (0046): kept, legacy, never admissible.
        (assessment,) = store.replication_assessments(idea_id=IDEA, idea_version=1)
        assert assessment.legacy
        assert assessment.evidence_id == "IEVD-old-replication"
        assert not (
            assessment.configuration_independent or assessment.perturbation_attested
        )
        evidence = store.list_evidence(idea_id=IDEA, idea_version=1)
        chains = provenance.replication_provenance(
            store, None, idea_id=IDEA, idea_version=1, evidence=evidence
        )
        assert [chain.admissible for chain in chains] == [False]
        rule = gates.REPLICATION_RULES[AdjudicationType.EMPIRICAL]
        assert not gates._replication_met(
            rule, evidence, {"MCALL-old-primary"}, (), chains
        )
        assert store.receipt_for_job("XJOB-old-replication") is None

        # ---- manifests (0046): an interpreted replication's pointer is frozen.
        with pytest.raises(RuntimeDatabaseError, match="frozen"):
            store.set_execution_manifest("PEXP-old-replication", artifact_id="x")

        # ---- reviews (0044): only the provably complete one is live.
        with db.tx() as conn:
            counts = {
                row["review_id"]: row["objection_count"]
                for row in conn.execute(
                    "select review_id, objection_count from idea_reviews"
                ).fetchall()
            }
        assert counts == {
            "IREV-old-pass": 0,
            "IREV-old-minor": None,
            "IREV-old-fatal": None,
        }
        live = store.live_reviews(
            idea_id=IDEA, current_prompt_versions=None, max_age_seconds=None
        )
        # All three bind the current content and evidence; only the review
        # whose completeness is certain counts.
        assert [item.review_id for item in live] == ["IREV-old-pass"]
        standing = store.open_objections(idea_id=IDEA)
        assert [(item.objection_id, item.severity) for item in standing] == [
            ("IOBJ-old", Severity.MINOR)
        ]
        with pytest.raises(RuntimeDatabaseError), db.tx() as conn:
            conn.execute(
                "insert into idea_objections (objection_id, idea_id, "
                "raised_in_review, raised_at_version, objection_key, severity, "
                "summary, ordinal) values ('IOBJ-late', %s, 'IREV-old-fatal', 1, "
                "'k', 'FATAL', 'recovered afterwards', 0)",
                (IDEA,),
            )

        # ---- parks (0045): no ceiling revives an unstructured legacy park.
        for idea_id in (PARK, MODEL_PARK):
            parked = store.require_idea(idea_id)
            assert parked.status is IdeaStatus.PARKED
            assert parked.park_reason is ParkReason.LEGACY_UNKNOWN
            assert parked.revisit_if  # history kept
        config = load_config()
        raised = config.with_overrides(
            {
                "idea_spend_ceiling_usd": 1000.0,
                "lineage_spend_ceiling_usd": 1000.0,
            }
        )
        assert _revive_budget_parks(store, "old", raised) == 0
        from research_os.portfolio import frontier

        assert frontier.revive_for_lineage_room(store, "old", raised) == 0
        assert store.require_idea(PARK).status is IdeaStatus.PARKED
        assert store.require_idea(MODEL_PARK).status is IdeaStatus.PARKED

        # ---- reservations (0043): charged in full, nothing new uncapped.
        ledger = BudgetLedger(db)
        assert ledger.reconcile_stale(older_than_seconds=3600) == 1
        record = ledger.get(
            scope=BudgetScope.PROJECT,
            scope_id="old",
            dimension=Dimension.MODEL_COST_USD,
        )
        assert record is not None
        assert record.spent == Decimal("0.6") and record.reserved == 0
        grant = ledger.reserve(
            scope=BudgetScope.PROJECT,
            scope_id="old",
            dimension=Dimension.MODEL_COST_USD,
            amount=Decimal("0.3"),
            pending=True,
        )
        with pytest.raises(RuntimeDatabaseError), db.tx() as conn:
            conn.execute(
                "update budget_reservations set submitted_at = now() "
                "where reservation_id = %s",
                (grant.reservation_id,),
            )
        assert migrate(db) == ()


def test_upgraded_0036_replication_evidence_cannot_satisfy_the_current_gate(
    throwaway_dsn: str,
) -> None:
    """LEGACY_REPLICATION, the reviewer's case, through the gate as it runs."""

    with Database(throwaway_dsn) as db:
        _apply_up_to(db, "0036")
        _legacy_rows(db, schema="0036")
        migrate(db)
        store = PortfolioStore(db)
        evidence = store.list_evidence(idea_id=IDEA, idea_version=1)
        chains = provenance.replication_provenance(
            store, None, idea_id=IDEA, idea_version=1, evidence=evidence
        )
        rule = gates.REPLICATION_RULES[AdjudicationType.EMPIRICAL]
        # The frozen gate's own call, which passed on 8e92e8c ...
        assert not gates._replication_met(rule, evidence, {"MCALL-old-primary"})
        # ... and the gate as the runner now calls it, with the chains.
        assert not gates._replication_met(
            rule, evidence, {"MCALL-old-primary"}, (), chains
        )
        replication = store.get_experiment(
            idea_id=IDEA, idea_version=1, role=ExperimentRole.REPLICATION
        )
        assert replication is not None
        assert replication.execution_manifest_artifact_id is None
