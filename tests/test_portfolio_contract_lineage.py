"""Which frozen contract a result belongs to, and that nothing later poses as it.

Held here:

- **a replication names the primary contract it replicates**, as the
  contract row's ``parent_contract_id`` -- set when its analysis is
  inherited, readable after a reload with no document parsed, and committed
  to by its digest, so verifying it against no parent or another one fails;
- **the database refuses a wrong parent**: another idea's contract, another
  version's, a replication's, or a parent on a primary; and it refuses to
  bind an experiment to a contract of another idea version, role or kind;
- **a replication frozen before the link keeps an empty parent** -- the
  migration writes no relationship it would have to guess;
- **no later analysis is read as the primary one**: no production code
  creates a contract that is not preregistered (the route that did, and the
  re-analysis that read under it, had no caller and were removed), and a
  non-preregistered contract that reaches the database anyway -- a restore,
  a psql session -- can neither be bound to a measurement nor read under.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any

import pytest

from research_os.portfolio import empirical, scicontract
from research_os.portfolio.models import (
    ContractKind,
    ExperimentRole,
    ExperimentState,
)
from research_os.portfolio.prompts import TEMPLATES
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.db import Database, RuntimeDatabaseError
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import idea_fields
from tests.runtime_graph_helpers import ScriptedRouter
from tests.runtime_helpers import (
    pg_dsn,
    runtime_db,
    runtime_project,
    runtime_xdg,
    throwaway_dsn,
)
from tests.test_portfolio_scientific_contract import (
    _analysis_answer,
    _context,
    _design_answer,
    _idea,
    grid_repo,
)

__all__ = [
    "grid_repo",
    "pg_dsn",
    "runtime_db",
    "runtime_project",
    "runtime_xdg",
    "throwaway_dsn",
]


@pytest.fixture
def portfolio(runtime_db: Database) -> PortfolioStore:
    return PortfolioStore(runtime_db)


def _measured(
    portfolio: PortfolioStore,
    runtime_db: Database,
    tmp: Path,
    project: str,
    repo: Path,
) -> tuple[Any, Any, Any]:
    """An idea whose primary and replication were both designed, run and read."""

    idea_id = _idea(portfolio, project)
    router = ScriptedRouter(
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: _analysis_answer(),
            TEMPLATES["experiment_designer"].identity: _design_answer(
                [1, 2, 4], [1, 3]
            ),
            TEMPLATES["replication_designer"].identity: {
                **_design_answer([1, 3, 5], [2, 4]),
                "variation_kind": "grid",
                "variation_detail": "grid: other sizes and difficulties",
            },
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(portfolio, runtime_db, tmp, project, idea_id, router, repo)
    version = portfolio.require_version(idea_id)
    primary = empirical.advance(context, version)
    assert primary.ok, primary.detail
    replication = empirical.advance(context, version, role=ExperimentRole.REPLICATION)
    assert replication.ok, replication.detail
    return context, primary.experiment, replication.experiment


# ================================================ replication -> primary --
def test_a_replication_contract_names_and_commits_to_the_primary_it_replicates(
    portfolio: PortfolioStore,
    runtime_db: Database,
    pg_dsn: str,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Before this, the primary was named only in the replication's document.

    Now it is a column, validated on insert and immutable after it, and the
    replication's digest contains the primary's -- so the link is something
    the runtime reads and verifies, not text it would have to trust.
    """

    context, primary, replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    primary_contract = portfolio.require_contract(primary.contract_id)
    copy_contract = portfolio.require_contract(replication.contract_id)
    assert copy_contract.role is ExperimentRole.REPLICATION
    assert copy_contract.parent_contract_id == primary_contract.contract_id

    # Across a reload: a fresh connection reads the same relationship.
    with Database(pg_dsn) as fresh:
        reloaded = PortfolioStore(fresh).require_contract(copy_contract.contract_id)
    assert reloaded.parent_contract_id == primary_contract.contract_id

    # Committed to by the digest: the right parent verifies, nothing else does.
    scicontract.verify(context.artifacts, copy_contract, parent=primary_contract)
    with pytest.raises(scicontract.ContractIntegrityError):
        scicontract.verify(context.artifacts, copy_contract, parent=None)
    with pytest.raises(scicontract.ContractIntegrityError):
        scicontract.verify(context.artifacts, copy_contract, parent=copy_contract)
    # A different contract that happens to carry the same digest is still
    # not the parent the row names.
    impostor = primary_contract.model_copy(
        update={"contract_id": "PCON-20260101T000000Z-00000000"}
    )
    with pytest.raises(scicontract.ContractIntegrityError, match="verified against"):
        scicontract.verify(context.artifacts, copy_contract, parent=impostor)
    without_parent = scicontract.contract_digest(
        idea_id=copy_contract.idea_id,
        idea_version=copy_contract.idea_version,
        hypothesis_digest=copy_contract.hypothesis_digest,
        role=str(copy_contract.role),
        kind=copy_contract.kind,
        analysis_digest=copy_contract.analysis_digest,
        design_digest=copy_contract.design_digest,
    )
    assert copy_contract.contract_digest != without_parent

    # The analysis it inherited is the primary's, by digest, and the stored
    # analysis document names the same parent as the row.
    assert copy_contract.analysis_digest == primary_contract.analysis_digest
    document = json.loads(
        context.artifacts.get_text(copy_contract.analysis_artifact_id)
    )
    assert document["parent_contract_id"] == primary_contract.contract_id
    # And the replication was read under its own contract.
    read = portfolio.require_experiment(replication.experiment_id)
    assert read.state is ExperimentState.INTERPRETED
    assert read.contract_id == copy_contract.contract_id


def test_a_stored_analysis_that_names_another_parent_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The document and the row must agree about the parent, not only the digest.

    The analysis digest covers the analysis itself; the parent the document
    records is beside it. A document re-filed under another parent -- with
    the row re-pointed at it, every trigger off -- is refused rather than
    read as the provenance of this replication.
    """

    context, primary, replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    copy_contract = portfolio.require_contract(replication.contract_id)
    document = json.loads(
        context.artifacts.get_text(copy_contract.analysis_artifact_id)
    )
    document["parent_contract_id"] = None
    forged = context.artifacts.put_text(
        json.dumps(document), media_type="application/json", role="forged", producer="t"
    )
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update scientific_contracts set analysis_artifact_id = %s "
            "where contract_id = %s",
            (forged.artifact_id, copy_contract.contract_id),
        )
    with pytest.raises(scicontract.ContractIntegrityError, match="different parent"):
        scicontract.verify(
            context.artifacts,
            portfolio.require_contract(copy_contract.contract_id),
            parent=portfolio.require_contract(primary.contract_id),
        )


def test_the_database_refuses_a_parent_that_is_not_the_primary_of_the_same_hypothesis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The foreign key said the parent existed; nothing said it was the right one."""

    _, primary, replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    own = portfolio.require_contract(primary.contract_id)
    copy_contract = portfolio.require_contract(replication.contract_id)
    other_idea = _idea(portfolio, runtime_project)
    other_version = portfolio.require_version(other_idea)

    def attempt(
        *,
        idea_id: str,
        version: Any,
        role: str,
        parent: str,
        match: str,
        kind: str = "PREREGISTERED",
        analysis_digest: str | None = None,
    ) -> None:
        # Raw and SUPERSEDED, so the one-live-contract index cannot answer
        # first: what refuses it must be the lineage check.
        with (
            pytest.raises(RuntimeDatabaseError, match=match),
            runtime_db.tx() as conn,
        ):
            conn.execute(
                "insert into scientific_contracts (contract_id, project_id, idea_id, "
                "idea_version, role, kind, state, hypothesis_digest, analysable, "
                "analysis_digest, analysis_artifact_id, analysis_prompt, "
                "parent_contract_id) values ("
                "'PCON-20260101T000000Z-0000abcd', %s, %s, %s, %s, %s, 'SUPERSEDED', "
                "%s, true, %s, %s, 'test', %s)",
                (
                    runtime_project,
                    idea_id,
                    version.version,
                    role,
                    kind,
                    version.content_digest,
                    analysis_digest or own.analysis_digest,
                    own.analysis_artifact_id,
                    parent,
                ),
            )

    own_version = portfolio.require_version(own.idea_id, own.idea_version)
    attempt(  # another idea's primary
        idea_id=other_idea,
        version=other_version,
        role="REPLICATION",
        parent=own.contract_id,
        match="same hypothesis",
    )
    attempt(  # the same idea, but a replication as the parent
        idea_id=own.idea_id,
        version=own_version,
        role="REPLICATION",
        parent=copy_contract.contract_id,
        match="only a replication inherits",
    )
    attempt(  # a primary that claims to inherit
        idea_id=own.idea_id,
        version=own_version,
        role="PRIMARY",
        parent=own.contract_id,
        match="only a replication inherits",
    )
    attempt(  # a replication that does not inherit its primary's analysis
        idea_id=own.idea_id,
        version=own_version,
        role="REPLICATION",
        parent=own.contract_id,
        analysis_digest="panalysis-v1:" + "1" * 64,
        match="freezes a different analysis",
    )
    attempt(  # an exploratory departure from a contract of another role
        idea_id=own.idea_id,
        version=own_version,
        role="REPLICATION",
        parent=own.contract_id,
        kind="EXPLORATORY",
        match="another role",
    )
    unfrozen = portfolio.create_contract(
        project_id=runtime_project,
        idea_id=other_idea,
        idea_version=other_version.version,
        role=ExperimentRole.PRIMARY,
        hypothesis_digest=other_version.content_digest,
        analysable=True,
        analysis_digest=own.analysis_digest,
        analysis_artifact_id=own.analysis_artifact_id,
    )
    attempt(  # a primary whose design was never frozen
        idea_id=other_idea,
        version=other_version,
        role="REPLICATION",
        parent=unfrozen.contract_id,
        match="only a frozen preregistered",
    )
    revised = portfolio.append_version(
        idea_id=own.idea_id,
        fields=idea_fields(title="a revised statement"),
        origin_role="scientific_discovery",
    )
    attempt(  # the same idea's primary, of an earlier version
        idea_id=own.idea_id,
        version=revised,
        role="REPLICATION",
        parent=own.contract_id,
        match="same hypothesis",
    )
    assert [
        item.contract_id for item in portfolio.list_contracts(idea_id=other_idea)
    ] == [unfrozen.contract_id]


def test_an_experiment_is_bound_only_to_the_contract_of_its_own_version_and_role(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """Another role, another idea, another version: each refused by the database."""

    _, primary, _replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    other_idea = _idea(portfolio, runtime_project)
    revised = portfolio.append_version(
        idea_id=primary.idea_id,
        fields=idea_fields(title="a revised statement"),
        origin_role="scientific_discovery",
    )
    for index, (idea_id, version, role) in enumerate(
        (
            (primary.idea_id, primary.idea_version, "REPLICATION"),
            (other_idea, 1, "PRIMARY"),
            (primary.idea_id, revised.version, "PRIMARY"),
        )
    ):
        with (
            pytest.raises(RuntimeDatabaseError, match="cannot be read under"),
            runtime_db.tx() as conn,
        ):
            conn.execute(
                "insert into idea_experiments (experiment_id, idea_id, idea_version, "
                "project_id, role, command, spec_digest, variation_digest, "
                "workspace_path, contract_id) values (%s, %s, %s, %s, %s, "
                "'grid', %s, %s, '/tmp/w', %s)",
                (
                    f"PEXP-20260101T000000Z-0000dea{index}",
                    idea_id,
                    version,
                    runtime_project,
                    role,
                    "a" * 64,
                    "b" * 64,
                    primary.contract_id,
                ),
            )


def test_verify_holds_the_parent_to_the_row_and_to_its_analysis(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The stored contract document must name the row's parent, and a
    replication must carry its parent's analysis -- each checked by `verify`
    itself, not only by the insert trigger."""

    context, primary, replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    own = portfolio.require_contract(primary.contract_id)
    copy_contract = portfolio.require_contract(replication.contract_id)
    drifted = copy_contract.model_copy(
        update={"analysis_digest": "panalysis-v1:" + "1" * 64}
    )
    with pytest.raises(scicontract.ContractIntegrityError, match="different analysis"):
        scicontract.verify(context.artifacts, drifted, parent=own)

    document = json.loads(
        context.artifacts.get_text(copy_contract.contract_artifact_id)
    )
    document["parent_contract_id"] = "PCON-20260101T000000Z-0000f00d"
    forged = context.artifacts.put_text(
        json.dumps(document), media_type="application/json", role="forged", producer="t"
    )
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update scientific_contracts set contract_artifact_id = %s "
            "where contract_id = %s",
            (forged.artifact_id, copy_contract.contract_id),
        )
    with pytest.raises(scicontract.ContractIntegrityError, match="stored contract"):
        scicontract.verify(
            context.artifacts,
            portfolio.require_contract(copy_contract.contract_id),
            parent=own,
        )


def test_a_replication_is_never_read_against_a_primary_that_was_replaced(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The parent is checked at insert; this is what holds it afterwards.

    A replication designed while the primary was still unread names that
    primary. If the primary is then retired and another frozen and read, the
    replication must not be read against it: the application refuses to, and
    the next advance retires the unread replication and inherits again from
    the primary that is there now.
    """

    idea_id = _idea(portfolio, runtime_project)
    router = ScriptedRouter(
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: _analysis_answer(),
            TEMPLATES["experiment_designer"].identity: _design_answer(
                [1, 2, 4], [1, 3]
            ),
            TEMPLATES["replication_designer"].identity: {
                **_design_answer([1, 3, 5], [2, 4]),
                "variation_kind": "grid",
                "variation_detail": "grid: other sizes and difficulties",
            },
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea_id, router, grid_repo
    )
    version = portfolio.require_version(idea_id)
    first = empirical.design(context, version)
    assert first.ok, first.detail
    early = empirical.design(
        context, version, role=ExperimentRole.REPLICATION, previous=first.experiment
    )
    assert early.ok, early.detail
    early_contract = portfolio.require_contract(early.experiment.contract_id)
    assert early_contract.parent_contract_id == first.experiment.contract_id

    empirical._retire_contract(
        context,
        portfolio.require_contract(first.experiment.contract_id),
        detail="retired before it was measured",
    )
    measured = empirical.advance(context, version)
    assert measured.ok, measured.detail
    assert measured.experiment.contract_id != first.experiment.contract_id

    refused = empirical.submit(
        context, portfolio.require_experiment(early.experiment.experiment_id)
    )
    assert not refused.ok
    assert refused.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "not the contract of this version's primary" in refused.detail

    again = empirical.advance(context, version, role=ExperimentRole.REPLICATION)
    assert again.ok, again.detail
    now = portfolio.require_contract(again.experiment.contract_id)
    assert now.parent_contract_id == measured.experiment.contract_id
    assert portfolio.require_contract(early_contract.contract_id).state.value == (
        "SUPERSEDED"
    )
    assert portfolio.require_experiment(early.experiment.experiment_id).state is (
        ExperimentState.SUPERSEDED
    )


def test_a_replication_contract_of_a_replaced_primary_is_not_reused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """The two places a live replication contract is reused, held to its parent.

    `ensure_analysis` hands back the live contract; `_inherit_analysis`
    hands back whichever won a race to insert. Each now checks that it
    replicates the primary asked about: the first retires an unread one and
    inherits again, the second refuses rather than return another primary's.
    """

    idea_id = _idea(portfolio, runtime_project)
    router = ScriptedRouter(
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: _analysis_answer(),
            TEMPLATES["experiment_designer"].identity: _design_answer(
                [1, 2, 4], [1, 3]
            ),
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea_id, router, grid_repo
    )
    version = portfolio.require_version(idea_id)
    first = empirical.design(context, version)
    assert first.ok, first.detail
    held = empirical.ensure_analysis(
        context, version, role=ExperimentRole.REPLICATION, previous=first.experiment
    )
    stale = held.contract
    assert stale.parent_contract_id == first.experiment.contract_id
    empirical._retire_contract(
        context,
        portfolio.require_contract(first.experiment.contract_id),
        detail="retired before it was measured",
    )
    measured = empirical.advance(context, version)
    assert measured.ok, measured.detail
    current = portfolio.require_experiment(measured.experiment.experiment_id)

    raced = empirical._inherit_analysis(context, version, previous=current)
    assert isinstance(raced, empirical.ExperimentStep) and not raced.ok
    assert raced.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert stale.contract_id in raced.detail

    again = empirical.ensure_analysis(
        context, version, role=ExperimentRole.REPLICATION, previous=current
    )
    assert again.contract.contract_id != stale.contract_id
    assert again.contract.parent_contract_id == current.contract_id
    assert portfolio.require_contract(stale.contract_id).state.value == "SUPERSEDED"


def test_a_replication_frozen_before_the_upgrade_still_verifies_and_is_read(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """An empty parent is kept, not repaired: the contract advances as frozen."""

    idea_id = _idea(portfolio, runtime_project)
    router = ScriptedRouter(
        answers_by_prompt={
            TEMPLATES["analysis_designer"].identity: _analysis_answer(),
            TEMPLATES["experiment_designer"].identity: _design_answer(
                [1, 2, 4], [1, 3]
            ),
            TEMPLATES["replication_designer"].identity: {
                **_design_answer([1, 3, 5], [2, 4]),
                "variation_kind": "grid",
                "variation_detail": "grid: other sizes and difficulties",
            },
        },
        store=RuntimeStore(runtime_db),
    )
    context = _context(
        portfolio, runtime_db, tmp_path, runtime_project, idea_id, router, grid_repo
    )
    version = portfolio.require_version(idea_id)
    primary = empirical.advance(context, version)
    assert primary.ok, primary.detail
    own = portfolio.require_contract(primary.experiment.contract_id)
    # What the build before this one froze: the primary's analysis, no parent.
    old_style = empirical._freeze_analysis(
        context,
        version,
        role=ExperimentRole.REPLICATION,
        spec=scicontract.verify(context.artifacts, own, version=version).analysis,
        provenance={"inherited_from": own.contract_id},
        analysis_prompt=f"inherited:{own.contract_id}",
        analysis_call_id=None,
    )
    assert old_style.parent_contract_id is None
    read = empirical.advance(context, version, role=ExperimentRole.REPLICATION)
    assert read.ok, read.detail
    assert read.experiment.contract_id == old_style.contract_id
    kept = portfolio.require_contract(old_style.contract_id)
    assert kept.parent_contract_id is None and kept.state.value == "FROZEN"
    scicontract.verify(context.artifacts, kept, parent=None)
    assert portfolio.require_experiment(read.experiment.experiment_id).state is (
        ExperimentState.INTERPRETED
    )


def test_a_replication_frozen_before_the_link_keeps_an_empty_parent(
    throwaway_dsn: str,
) -> None:
    """The migration names no parent it would have to read out of text.

    Such a contract's digest was computed without a parent; naming one now
    would make it fail verification, and the only record of its primary is
    document prose. Empty means not recorded.
    """

    from research_os.runtime.migrations import discover, migrate

    files = discover()
    with Database(throwaway_dsn) as db:
        with db.tx() as conn:
            for migration in (m for m in files if m.version <= "0034"):
                conn.execute(migration.sql)
                conn.execute(
                    "insert into schema_migrations (version, checksum) values (%s, %s)",
                    (migration.version, migration.checksum),
                )
            idea_id = "PIDEA-20260101T000000Z-aaaaaaaa"
            conn.execute(
                "insert into projects (project_id, repo_path) values ('old', '/tmp/o')"
            )
            conn.execute(
                "insert into ideas (idea_id, project_id, depth, lineage_root, origin, "
                "current_version, status, operational_state, quality_tier) values "
                "(%s, 'old', 0, %s, 'BLIND_EXPLORER', 1, 'VALIDATED', 'IDLE', "
                "'VALIDATED')",
                (idea_id, idea_id),
            )
            conn.execute(
                "insert into idea_versions (idea_id, version, title, "
                "research_question, core_idea, content_digest, canonical_digest, "
                "origin_role) values (%s, 1, 't', 'q', 'c', %s, %s, 'blind_explorer')",
                (
                    idea_id,
                    "pidea-content-v1:" + "c" * 64,
                    "pidea-canonical-v1:" + "d" * 64,
                ),
            )
            for role, contract_id, prompt in (
                ("PRIMARY", "PCON-20260101T000000Z-0000000p", "analysis_designer@1"),
                (
                    "REPLICATION",
                    "PCON-20260101T000000Z-0000000r",
                    "inherited:PCON-20260101T000000Z-0000000p",
                ),
            ):
                conn.execute(
                    "insert into artifacts (artifact_id, size_bytes, media_type, role) "
                    "values (%s, 2, 'application/json', 'idea_analysis')",
                    (f"A{contract_id}",),
                )
                conn.execute(
                    "insert into scientific_contracts (contract_id, project_id, idea_id, "
                    "idea_version, role, state, hypothesis_digest, analysable, "
                    "analysis_digest, analysis_artifact_id, analysis_prompt) values "
                    "(%s, 'old', %s, 1, %s, 'ANALYSIS_FROZEN', %s, true, %s, %s, %s)",
                    (
                        contract_id,
                        idea_id,
                        role,
                        "pidea-content-v1:" + "c" * 64,
                        "panalysis-v1:" + "e" * 64,
                        f"A{contract_id}",
                        prompt,
                    ),
                )
            # A row the new insert rules would refuse -- a primary naming a
            # parent -- written before them. The migration leaves it alone.
            conn.execute(
                "insert into scientific_contracts (contract_id, project_id, idea_id, "
                "idea_version, role, state, hypothesis_digest, analysable, "
                "analysis_digest, analysis_artifact_id, analysis_prompt, "
                "parent_contract_id) values (%s, 'old', %s, 1, 'PRIMARY', "
                "'SUPERSEDED', %s, true, %s, %s, 'test', %s)",
                (
                    "PCON-20260101T000000Z-0000000x",
                    idea_id,
                    "pidea-content-v1:" + "c" * 64,
                    "panalysis-v1:" + "e" * 64,
                    "APCON-20260101T000000Z-0000000p",
                    "PCON-20260101T000000Z-0000000p",
                ),
            )
        assert migrate(db) == ("0035",)
        store = PortfolioStore(db)
        legacy = store.require_contract("PCON-20260101T000000Z-0000000r")
        odd = store.require_contract("PCON-20260101T000000Z-0000000x")
    assert legacy.parent_contract_id is None
    assert legacy.analysis_prompt.startswith("inherited:"), "the text is kept as text"
    assert odd.parent_contract_id == "PCON-20260101T000000Z-0000000p"


# ======================================= nothing later poses as the primary --
def test_no_production_code_creates_a_contract_that_is_not_preregistered() -> None:
    """The only creator of EXPLORATORY contracts had no production caller.

    `amend_contract` and `reanalyse` were reachable from one test. They wrote
    a labelled exploratory contract and an artifact nothing read, recorded no
    actor, and kept a mutation-shaped entry point beside immutable
    preregistration. Removed; this keeps them removed. The kind stays in the
    schema, because migration 0031 is applied and its meaning is fixed.
    """

    root = Path(__file__).resolve().parents[1] / "src" / "research_os"
    offenders: list[str] = []
    for path in sorted(root.rglob("*.py")):
        if path.name == "models.py" and path.parent.name == "portfolio":
            continue  # where the enum is declared
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # Any attribute of that name, however the enum was reached or
            # aliased, and the literal.
            if (isinstance(node, ast.Attribute) and node.attr == "EXPLORATORY") or (
                isinstance(node, ast.Constant) and node.value == "EXPLORATORY"
            ):
                offenders.append(f"{path.relative_to(root)}:{node.lineno}")
    assert offenders == []
    assert not hasattr(empirical, "amend_contract")
    assert not hasattr(empirical, "reanalyse")


def test_a_later_analysis_that_reaches_the_database_is_never_read_as_the_primary(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    grid_repo: Path,
    tmp_path: Path,
) -> None:
    """A restore or a psql session can still insert an exploratory contract.

    It does not become the idea's contract, the database will not bind a
    measurement to it, and a measurement bound to it with every trigger off
    is not read: nothing runs, no evidence is written.
    """

    context, primary, _replication = _measured(
        portfolio, runtime_db, tmp_path, runtime_project, grid_repo
    )
    own = portfolio.require_contract(primary.contract_id)
    before = portfolio.list_evidence(idea_id=own.idea_id)
    later = portfolio.create_contract(
        project_id=runtime_project,
        idea_id=own.idea_id,
        idea_version=own.idea_version,
        role=ExperimentRole.PRIMARY,
        hypothesis_digest=own.hypothesis_digest,
        analysable=True,
        analysis_digest="panalysis-v1:" + "9" * 64,
        analysis_artifact_id=own.analysis_artifact_id,
        kind=ContractKind.EXPLORATORY,
        parent_contract_id=own.contract_id,
        design={
            "design_digest": own.design_digest,
            "design_artifact_id": own.design_artifact_id,
            "design_prompt": own.design_prompt,
            "design_call_id": own.design_call_id,
            "contract_digest": "pcontract-v1:" + "9" * 64,
            "contract_artifact_id": own.contract_artifact_id,
        },
    )
    live = portfolio.live_contract(
        idea_id=own.idea_id, idea_version=own.idea_version, role=ExperimentRole.PRIMARY
    )
    assert live is not None and live.contract_id == own.contract_id

    forged = "PEXP-20260101T000000Z-0000beef"
    insert = (
        "insert into idea_experiments (experiment_id, idea_id, idea_version, "
        "project_id, role, command, spec_digest, variation_digest, workspace_path, "
        "decision_rule, preregistration_artifact_id, contract_id) "
        "select %s, idea_id, idea_version, project_id, role, command, "
        "spec_digest || '', variation_digest, %s, decision_rule, "
        "preregistration_artifact_id, %s from idea_experiments where experiment_id = %s"
    )
    params = (forged, "/tmp/forged", later.contract_id, primary.experiment_id)
    with (
        pytest.raises(RuntimeDatabaseError, match="cannot be read under"),
        runtime_db.tx() as conn,
    ):
        conn.execute(insert, params)
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update idea_experiments set state = 'SUPERSEDED' where experiment_id = %s",
            (primary.experiment_id,),
        )
        conn.execute(insert, params)
    step = empirical.submit(context, portfolio.require_experiment(forged))
    assert not step.ok
    assert step.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "preregistered contract of its own role" in step.detail

    # And a replication-role execution bound to the primary's preregistered
    # contract -- the right kind, the wrong role -- is refused the same way.
    crossed = "PEXP-20260101T000000Z-0000cafe"
    with runtime_db.tx() as conn:
        conn.execute("set local session_replication_role = replica")
        conn.execute(
            "update idea_experiments set state = 'SUPERSEDED' "
            "where idea_id = %s and role = 'REPLICATION'",
            (primary.idea_id,),
        )
        conn.execute(
            "insert into idea_experiments (experiment_id, idea_id, idea_version, "
            "project_id, role, command, spec_digest, variation_digest, "
            "workspace_path, decision_rule, preregistration_artifact_id, "
            "contract_id, state) select %s, idea_id, idea_version, project_id, "
            "'REPLICATION', command, spec_digest, variation_digest, '/tmp/crossed', "
            "decision_rule, preregistration_artifact_id, contract_id, 'PROPOSED' "
            "from idea_experiments where experiment_id = %s",
            (crossed, primary.experiment_id),
        )
    step = empirical.submit(context, portfolio.require_experiment(crossed))
    assert not step.ok
    assert "preregistered contract of its own role" in step.detail
    assert portfolio.list_evidence(idea_id=own.idea_id) == before
    assert portfolio.require_contract(own.contract_id) == own
