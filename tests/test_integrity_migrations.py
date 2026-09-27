"""The integrity migrations (0037-0042) as an upgrade over live 0036 data.

A migration added for this round is exercised by the session fixture only on
a database that has seen nothing. What an existing deployment hits is
different: rows already exist, written by code that did not know what the new
columns mean, and each migration has to give them the meaning that fails
closed. This applies the 0036 schema, writes the rows a 0036 deployment
holds -- a HELD reservation, an ACTIVE action, a review, a literature row, a
parked idea -- with explicit SQL naming only 0036 columns, migrates the rest,
and checks what each legacy row now means:

- 0037: a reservation that existed before submission was recorded is
  *submitted*, so the reconciler charges it rather than releasing it;
- 0038: an action gets its run back from its LangGraph thread, so its owner's
  lock can be probed;
- 0039: a literature row names no retrieval, so it can be no retrieval path;
- 0040: the old one-review-per-binding identity is gone and a second call on
  the same binding is its own row;
- 0041 and 0042: new, nullable, and consistent with every existing row.
"""

from __future__ import annotations

from decimal import Decimal

from research_os.portfolio import gates
from research_os.portfolio.models import IdeaStatus, ReviewerRole
from research_os.portfolio.store import OWNER_DEAD, PortfolioStore
from research_os.runtime.budgets import BudgetLedger, Dimension
from research_os.runtime.db import Database
from research_os.runtime.migrations import discover, migrate
from research_os.runtime.models import BudgetScope
from tests.portfolio_helpers import record_review
from tests.runtime_helpers import pg_dsn, throwaway_dsn

__all__ = ["pg_dsn", "throwaway_dsn"]

IDEA = "PIDEA-19700101T000000Z-legacy00"
PARKED = "PIDEA-19700101T000000Z-legacy01"
RUN = "RRUN-19700101T000000Z-legacy00"


def _legacy(db: Database) -> None:
    previous = [m for m in discover() if m.version <= "0036"]
    with db.tx() as conn:
        for migration in previous:
            conn.execute(migration.sql)
            conn.execute(
                "insert into schema_migrations (version, checksum) values (%s, %s)",
                (migration.version, migration.checksum),
            )
    with db.tx() as conn:
        conn.execute(
            "insert into projects (project_id, repo_path) values ('legacy', '/tmp/legacy')"
        )
        conn.execute(
            "insert into research_runs (run_id, project_id, objective, status, thread_id, "
            "run_kind) values (%s, 'legacy', 'idea-track', 'RUNNING', %s, 'idea_track')",
            (RUN, f"idea-track:{RUN}"),
        )
        for idea_id, status, extra in (
            (IDEA, "INVESTIGATING", ("", "")),
            (PARKED, "PARKED", ("spent its ceiling", "a person raises it")),
        ):
            conn.execute(
                "insert into ideas (idea_id, project_id, lineage_root, origin, status, "
                "operational_state, retire_reason, revisit_if) values "
                "(%s, 'legacy', %s, 'BLIND_EXPLORER', %s, %s, %s, %s)",
                (
                    idea_id,
                    idea_id,
                    status,
                    "ACTIVE" if idea_id == IDEA else "IDLE",
                    extra[0] or None,
                    extra[1] or None,
                ),
            )
            conn.execute(
                "insert into idea_versions (idea_id, version, title, research_question, "
                "core_idea, content_digest, canonical_digest, origin_role) values "
                "(%s, 1, 't', 'q', 'c', %s, %s, 'blind_explorer')",
                (
                    idea_id,
                    f"pidea-content-v1:{idea_id}",
                    f"pidea-canonical-v1:{idea_id}",
                ),
            )
        conn.execute(
            "insert into idea_actions (action_id, idea_id, idea_version, stage, "
            "basis_digest, status, thread_id) values "
            "('IACT-19700101T000000Z-legacy00', %s, 1, 'literature_audit', 'b', "
            "'ACTIVE', %s)",
            (IDEA, f"idea-track:{RUN}"),
        )
        conn.execute(
            "insert into idea_evidence (evidence_id, idea_id, idea_version, kind, "
            "strength, summary, literature_key, source_call_id) values "
            "('IEVD-19700101T000000Z-legacy00', %s, 1, 'literature', 'SUPPORTS', "
            "'a legacy audit row', 'openalex:W1', null)",
            (IDEA,),
        )
        conn.execute(
            "insert into budgets (budget_id, scope, scope_id, dimension, limit_value, "
            "reserved) values ('BDGT-legacy', 'project', 'legacy', 'model_cost_usd', "
            "10, 4)"
        )
        conn.execute(
            "insert into budget_reservations (reservation_id, budget_id, amount, "
            "created_at) values ('RSV-legacy', 'BDGT-legacy', 4, "
            "now() - interval '3 hours')"
        )


def test_the_integrity_migrations_upgrade_live_0036_data(throwaway_dsn: str) -> None:
    with Database(throwaway_dsn) as db:
        _legacy(db)
        applied = migrate(db)
        assert applied == tuple(m.version for m in discover() if m.version > "0036")
        assert applied[:6] == ("0037", "0038", "0039", "0040", "0041", "0042")

        # 0037 -- the legacy HELD reservation is submitted, so unknown spend is charged.
        with db.tx() as conn:
            row = conn.execute(
                "select submitted_at, created_at from budget_reservations "
                "where reservation_id = 'RSV-legacy'"
            ).fetchone()
        assert row["submitted_at"] == row["created_at"]
        ledger = BudgetLedger(db)
        assert ledger.reconcile_stale(older_than_seconds=3600) == 1
        budget = ledger.get(
            scope=BudgetScope.PROJECT,
            scope_id="legacy",
            dimension=Dimension.MODEL_COST_USD,
        )
        assert budget is not None
        assert budget.spent == Decimal(4) and budget.reserved == Decimal(0)

        # 0038 -- the action's run comes back from its thread; its owner is gone.
        store = PortfolioStore(db)
        action = store.active_action(IDEA)
        assert action is not None and action.run_id == RUN
        assert store.owner_is_live(action.action_id) == OWNER_DEAD

        # 0039 -- the legacy literature row names no search, so it is no path.
        (evidence,) = store.list_evidence(idea_id=IDEA)
        assert evidence.retrieval_id is None
        assert store.list_retrievals(idea_id=IDEA) == ()
        assert not gates._distinct_retrieval_path((evidence,), (), 1)

        # 0040 -- two calls on one binding are two rows; the first is attempt 1.
        first = record_review(store, idea_id=IDEA, version=1, role=ReviewerRole.SKEPTIC)
        second = record_review(
            store, idea_id=IDEA, version=1, role=ReviewerRole.SKEPTIC
        )
        assert first.review_id != second.review_id
        assert (first.attempt, second.attempt) == (1, 2)

        # 0041 / 0042 -- nullable, and consistent with every existing row.
        parked = store.require_idea(PARKED)
        assert parked.status is IdeaStatus.PARKED
        assert (parked.park_reason, parked.park_stage, parked.resume_status) == (
            None,
            None,
            None,
        )
        with db.tx() as conn:
            column = conn.execute(
                "select count(*) as n from information_schema.columns where "
                "table_name = 'idea_experiments' and "
                "column_name = 'execution_manifest_artifact_id'"
            ).fetchone()
        assert int(column["n"]) == 1
        assert migrate(db) == ()
