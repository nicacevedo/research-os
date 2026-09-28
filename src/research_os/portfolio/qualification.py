"""The Research OS v1 qualification contract, evaluated by code, read-only.

``qualification_v1.yaml`` beside this module is the contract: 25 mandatory
gates and the advisory dimensions, each decided by a check this module
implements (``automated``), by a named evidence file (``evidence``), or both.
Nothing here writes to the runtime database, calls a model or reads a
model's prose as a finding: every automated check is a query over records the
system wrote itself, and the chain checks re-verify through
:mod:`research_os.portfolio.sciencechain` and
:mod:`research_os.portfolio.provenance` exactly as the readiness gate does.

The freeze rule (see the YAML header): the zero-state snapshot taken at the
start of a live qualification records the spec's sha256, and evaluation
refuses a spec whose digest differs -- mandatory criteria cannot change once
a live qualification has begun.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml

from research_os.errors import ResearchOSError
from research_os.portfolio.models import ReviewerRole, Stage

SPEC_PATH = Path(__file__).with_name("qualification_v1.yaml")
SCHEMA = "research-os-qualification-v1"
SNAPSHOT_SCHEMA = "research-os-qualification-zero-state-v1"
CHECK_KINDS = frozenset({"automated", "evidence", "both"})

#: Origins that are the system's own, with no researcher text behind them.
AUTONOMOUS_ROOT_ORIGINS = (
    "BLIND_EXPLORER",
    "LITERATURE_EXPLORER",
    "FAILURE_MINING_EXPLORER",
)

#: What must be zero in a project that has never run.
ZERO_STATE_COUNTS: Mapping[str, str] = {
    "ideas": "select count(*) as n from ideas where project_id = %(p)s",
    "runs": "select count(*) as n from research_runs where project_id = %(p)s",
    "model_calls": (
        "select count(*) as n from model_calls c join research_runs r "
        "on r.run_id = c.run_id where r.project_id = %(p)s"
    ),
    "work_items": "select count(*) as n from work_items where project_id = %(p)s",
    "contracts": "select count(*) as n from scientific_contracts where project_id = %(p)s",
    "experiments": "select count(*) as n from idea_experiments where project_id = %(p)s",
    "science_objects": "select count(*) as n from science_objects where project_id = %(p)s",
    "outcomes": "select count(*) as n from science_outcomes where project_id = %(p)s",
    "external_jobs": "select count(*) as n from external_jobs where project_id = %(p)s",
    "retrievals": "select count(*) as n from literature_retrievals where project_id = %(p)s",
    "seeds": "select count(*) as n from portfolio_seeds where project_id = %(p)s",
    "spend_usd_x1e6": (
        "select coalesce(round(sum(spent) * 1000000), 0)::bigint as n from budgets "
        "where dimension = 'model_cost_usd' and "
        "((scope = 'project' and scope_id = %(p)s) or scope = 'system')"
    ),
}


class QualificationError(ResearchOSError):
    """The specification is malformed, or changed since the qualification began."""


# ------------------------------------------------------------ the spec --
def spec_digest(path: Path = SPEC_PATH) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_spec(path: Path = SPEC_PATH) -> dict[str, Any]:
    """The specification, checked for shape against what this module can decide."""

    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or data.get("schema") != SCHEMA:
        raise QualificationError(f"{path} is not a {SCHEMA} document")
    if data.get("frozen") is not True:
        raise QualificationError("a qualification specification is frozen or not one")
    seen_ids: set[str] = set()
    seen_names: set[str] = set()
    for tier in ("mandatory", "advisory"):
        for gate in data.get(tier) or ():
            for key in ("id", "name", "title", "requirement", "check"):
                if not gate.get(key):
                    raise QualificationError(f"a {tier} gate has no {key}: {gate}")
            if gate["id"] in seen_ids or gate["name"] in seen_names:
                raise QualificationError(f"gate {gate['id']} {gate['name']} repeats")
            seen_ids.add(gate["id"])
            seen_names.add(gate["name"])
            if gate["check"] not in CHECK_KINDS:
                raise QualificationError(f"{gate['id']}: unknown check {gate['check']}")
            if gate["check"] in {"automated", "both"} and gate["name"] not in CHECKS:
                raise QualificationError(
                    f"{gate['id']} {gate['name']} is automated and no check decides it"
                )
            if gate["check"] in {"evidence", "both"} and not (
                gate.get("evidence") or {}
            ).get("file"):
                raise QualificationError(f"{gate['id']} needs an evidence file")
    if len(data.get("mandatory") or ()) < 25:
        raise QualificationError("v1 has 25 mandatory gates")
    return data


# ------------------------------------------------------------- results --
@dataclass(frozen=True, slots=True)
class GateResult:
    id: str
    name: str
    title: str
    mandatory: bool
    passed: bool
    detail: str

    def record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "title": self.title,
            "tier": "MANDATORY" if self.mandatory else "ADVISORY",
            "status": "PASS" if self.passed else "FAIL",
            "detail": self.detail,
        }


@dataclass(frozen=True, slots=True)
class Report:
    spec_digest: str
    project_id: str
    verdict: str
    gates: tuple[GateResult, ...] = field(default_factory=tuple)

    def record(self) -> dict[str, Any]:
        return {
            "spec": "research-os-v1",
            "spec_digest": self.spec_digest,
            "project_id": self.project_id,
            "verdict": self.verdict,
            "mandatory_passed": sum(1 for g in self.gates if g.mandatory and g.passed),
            "mandatory_total": sum(1 for g in self.gates if g.mandatory),
            "gates": [gate.record() for gate in self.gates],
        }


# ------------------------------------------------------------- context --
@dataclass(frozen=True, slots=True)
class _Ctx:
    db: Any
    project_id: str
    artifacts: Any
    repo_path: Path | None

    def one(self, sql: str, **params: Any) -> Any:
        with self.db.tx() as conn:
            row = conn.execute(sql, {"p": self.project_id, **params}).fetchone()
        return row

    def rows(self, sql: str, **params: Any) -> list[Any]:
        with self.db.tx() as conn:
            return list(conn.execute(sql, {"p": self.project_id, **params}).fetchall())

    def count(self, sql: str, **params: Any) -> int:
        row = self.one(sql, **params)
        return int(row["n"]) if row is not None else 0

    @property
    def store(self) -> Any:
        from research_os.portfolio.store import PortfolioStore

        return PortfolioStore(self.db)


Check = Callable[[_Ctx], tuple[bool, str]]


def _at_least(n: int, what: str) -> tuple[bool, str]:
    return n >= 1, f"{n} {what}"


def _ideas_sql() -> str:
    return "select idea_id from ideas where project_id = %(p)s"


def _stage(ctx: _Ctx, stage: str) -> tuple[bool, str]:
    stage = str(Stage(stage))
    n = ctx.count(
        "select count(*) as n from idea_actions a join ideas i on i.idea_id = a.idea_id "
        "where i.project_id = %(p)s and a.stage = %(stage)s and a.status = 'SUCCEEDED'",
        stage=stage,
    )
    return _at_least(n, f"succeeded {stage} stage(s)")


# ---------------------------------------------------------- the checks --
def _unseeded_origination(ctx: _Ctx) -> tuple[bool, str]:
    roots = ctx.count(
        "select count(*) as n from ideas where project_id = %(p)s and depth = 0 "
        "and origin = any(%(origins)s)",
        origins=list(AUTONOMOUS_ROOT_ORIGINS),
    )
    seeds = ctx.count(
        "select count(*) as n from portfolio_seeds where project_id = %(p)s"
    )
    seeded = ctx.count(
        "select count(*) as n from ideas where project_id = %(p)s "
        "and origin in ('RESEARCHER_SEED','SEEDED_EXPLORER')"
    )
    ok = roots >= 1 and seeds == 0 and seeded == 0
    return (
        ok,
        f"{roots} autonomous root idea(s); {seeds} seed(s); {seeded} seeded idea(s)",
    )


def _idea_provenance(ctx: _Ctx) -> tuple[bool, str]:
    total = ctx.count("select count(*) as n from ideas where project_id = %(p)s")
    missing = ctx.count(
        "select count(*) as n from ideas i where i.project_id = %(p)s and not exists "
        "(select 1 from idea_provenance v where v.idea_id = i.idea_id)"
    )
    return total >= 1 and missing == 0, f"{total} idea(s), {missing} without provenance"


def _retrieval(ctx: _Ctx) -> tuple[bool, str]:
    n = ctx.count(
        "select count(*) as n from literature_retrievals where project_id = %(p)s "
        "and status = 'COMPLETED'"
    )
    return _at_least(n, "completed literature retrieval(s)")


def _curation(ctx: _Ctx) -> tuple[bool, str]:
    bank = ctx.one("select bank_commit from portfolio_state where project_id = %(p)s")
    bought = ctx.count(
        "select count(*) as n from idea_actions a join ideas i on i.idea_id = a.idea_id "
        "where i.project_id = %(p)s and a.utility is not null"
    )
    commit = (bank or {}).get("bank_commit") if bank else None
    return bool(commit) and bought >= 1, (
        f"bank commit {commit or '(none)'}; {bought} stage(s) bought with a utility"
    )


def _objects(kind: str) -> Check:
    def check(ctx: _Ctx) -> tuple[bool, str]:
        n = ctx.count(
            "select count(*) as n from science_objects where project_id = %(p)s "
            "and kind = %(kind)s",
            kind=kind,
        )
        return _at_least(n, f"frozen {kind} object(s)")

    return check


def _frozen_plan(ctx: _Ctx) -> tuple[bool, str]:
    n = ctx.count(
        "select count(*) as n from idea_experiments where project_id = %(p)s "
        "and plan_digest is not null"
    )
    return _at_least(n, "experiment(s) realising a frozen plan")


def _execution(role: str) -> Check:
    def check(ctx: _Ctx) -> tuple[bool, str]:
        n = ctx.count(
            "select count(*) as n from execution_receipts r "
            "join external_jobs j on j.job_id = r.job_id "
            "where j.project_id = %(p)s and r.role = %(role)s "
            "and r.plan_digest is not null and j.status = 'COMPLETED' "
            "and r.exit_code = 0",
            role=role,
        )
        return _at_least(n, f"completed plan-bound {role} execution(s)")

    return check


def _plan_receipts(ctx: _Ctx, role: str | None = None) -> list[Any]:
    from research_os.portfolio.models import ExecutionReceipt
    from research_os.portfolio.store import RECEIPT_COLUMNS

    clause = " and r.role = %(role)s" if role else ""
    rows = ctx.rows(
        "select "
        + ", ".join(f"r.{c.strip()}" for c in RECEIPT_COLUMNS.split(","))
        + " from execution_receipts r join external_jobs j on j.job_id = r.job_id "
        "where j.project_id = %(p)s and r.plan_digest is not null" + clause,
        role=role,
    )
    return [ExecutionReceipt.model_validate(row) for row in rows]


def _trusted_receipts(ctx: _Ctx) -> tuple[bool, str]:
    from research_os.portfolio import provenance

    receipts = _plan_receipts(ctx)
    broken = []
    for receipt in receipts:
        try:
            provenance.verified_receipt(ctx.artifacts, receipt)
        except provenance.ProvenanceError as exc:
            broken.append(f"{receipt.receipt_id}: {exc}")
    ok = bool(receipts) and not broken
    return ok, f"{len(receipts)} plan-bound receipt(s); broken: {broken or 'none'}"


def _validated_result(ctx: _Ctx) -> tuple[bool, str]:
    n = ctx.count(
        "select count(*) as n from science_outcomes where project_id = %(p)s "
        "and result_sha256 is not null"
    )
    return _at_least(n, "outcome(s) on a result that passed its declared schema")


def _chains_by_version(ctx: _Ctx) -> dict[tuple[str, int], tuple[Any, ...]]:
    from research_os.portfolio import sciencechain

    found: dict[tuple[str, int], tuple[Any, ...]] = {}
    for row in ctx.rows(
        "select distinct idea_id, idea_version from idea_experiments "
        "where project_id = %(p)s and plan_digest is not null"
    ):
        key = (str(row["idea_id"]), int(row["idea_version"]))
        found[key] = sciencechain.science_chains(
            ctx.store,
            ctx.artifacts,
            idea_id=key[0],
            idea_version=key[1],
            evidence=ctx.store.list_evidence(idea_id=key[0], idea_version=key[1]),
        )
    return found


def _primary_outcome(ctx: _Ctx) -> tuple[bool, str]:
    from research_os.portfolio.models import READ_OUTCOMES, ExperimentRole

    intact = [
        chain
        for chains in _chains_by_version(ctx).values()
        for chain in chains
        if chain.role is ExperimentRole.PRIMARY
        and chain.chain_intact
        and chain.outcome in READ_OUTCOMES
    ]
    states = sorted({str(chain.outcome) for chain in intact})
    return bool(intact), f"{len(intact)} primary reading(s) on intact chains: {states}"


def _replication_receipt(ctx: _Ctx) -> tuple[bool, str]:
    from research_os.portfolio import provenance

    good = []
    for receipt in _plan_receipts(ctx, "REPLICATION"):
        try:
            provenance.verified_receipt(ctx.artifacts, receipt)
        except provenance.ProvenanceError:
            continue
        if receipt.manifest_artifact_id and receipt.parent_receipt_id:
            good.append(receipt.receipt_id)
    return _at_least(
        len(good), "verified replication receipt(s) with manifest and parent"
    )


def _replication_provenance(ctx: _Ctx) -> dict[tuple[str, int], tuple[Any, ...]]:
    from research_os.portfolio import provenance

    found: dict[tuple[str, int], tuple[Any, ...]] = {}
    for row in ctx.rows(
        "select distinct a.idea_id, a.idea_version from replication_assessments a "
        "join ideas i on i.idea_id = a.idea_id where i.project_id = %(p)s"
    ):
        key = (str(row["idea_id"]), int(row["idea_version"]))
        found[key] = provenance.replication_provenance(
            ctx.store,
            ctx.artifacts,
            idea_id=key[0],
            idea_version=key[1],
            evidence=ctx.store.list_evidence(idea_id=key[0], idea_version=key[1]),
        )
    return found


def _replication_assessment(ctx: _Ctx) -> tuple[bool, str]:
    admissible = [
        record
        for records in _replication_provenance(ctx).values()
        for record in records
        if record.admissible
    ]
    measured = ctx.rows(
        "select a.agrees from replication_assessments a join ideas i "
        "on i.idea_id = a.idea_id where i.project_id = %(p)s and not a.legacy"
    )
    agreement = sorted({str(row["agrees"]) for row in measured})
    return bool(admissible), (
        f"{len(admissible)} admissible assessment(s) (VERIFIED and ATTESTED, chain "
        f"intact); MEASURED agreement recorded: {agreement}"
    )


def _board_ideas(ctx: _Ctx) -> set[str]:
    from research_os.portfolio.gates import _incomplete_review_events
    from research_os.portfolio.models import INDEPENDENT_REVIEW_ROLES
    from research_os.portfolio.runner import CURRENT_REVIEW_PROMPTS

    complete: set[str] = set()
    store = ctx.store
    for row in ctx.rows(_ideas_sql()):
        idea_id = str(row["idea_id"])
        live = store.live_reviews(
            idea_id=idea_id, current_prompt_versions=CURRENT_REVIEW_PROMPTS
        )
        roles = {item.reviewer_role for item in live}
        if not set(INDEPENDENT_REVIEW_ROLES) <= roles:
            continue
        if _incomplete_review_events(live, store.open_objections(idea_id=idea_id)):
            continue
        complete.add(idea_id)
    return complete


def _review_board(ctx: _Ctx) -> tuple[bool, str]:
    ideas = _board_ideas(ctx)
    return bool(ideas), f"{len(ideas)} idea(s) with a complete current board"


def _meta_ideas(ctx: _Ctx) -> set[str]:
    rows = ctx.rows(
        "select distinct a.idea_id from idea_actions a join ideas i "
        "on i.idea_id = a.idea_id where i.project_id = %(p)s "
        "and a.stage = %(stage)s and a.status = 'SUCCEEDED' "
        "and a.disposition is not null and exists (select 1 from idea_reviews r "
        "where r.idea_id = a.idea_id and r.reviewer_role = %(meta)s)",
        stage=str(Stage.META_REVIEW),
        meta=str(ReviewerRole.META),
    )
    return {str(row["idea_id"]) for row in rows}


def _meta_review(ctx: _Ctx) -> tuple[bool, str]:
    ideas = _meta_ideas(ctx)
    return bool(
        ideas
    ), f"{len(ideas)} idea(s) with a gate-permitted meta-review disposition"


def _recursive_child(ctx: _Ctx) -> tuple[bool, str]:
    n = ctx.count(
        "with recursive lineage(idea_id, root) as ("
        " select idea_id, idea_id from ideas where project_id = %(p)s and depth = 0"
        "   and origin = any(%(origins)s)"
        " union"
        " select e.child_idea_id, l.root from idea_edges e"
        "   join lineage l on l.idea_id = e.parent_idea_id)"
        " select count(*) as n from ideas i join lineage l on l.idea_id = i.idea_id"
        " where i.project_id = %(p)s and i.depth >= 1",
        origins=list(AUTONOMOUS_ROOT_ORIGINS),
    )
    return _at_least(n, "follow-up idea(s) of depth >= 1 from autonomous roots")


def _budget_authority(ctx: _Ctx) -> tuple[bool, str]:
    uncapped = ctx.count(
        "select count(*) as n from budget_reservations r join budgets b "
        "on b.budget_id = r.budget_id where b.dimension = 'model_cost_usd' "
        "and r.submitted_at is not null "
        "and (r.provider_cap_usd is null or r.provider_cap_usd > r.amount)"
    )
    overspent = ctx.rows(
        "select b.scope, b.scope_id, b.limit_value, b.spent from budgets b "
        "where b.explicit and b.spent > b.limit_value and "
        "((b.scope = 'project' and b.scope_id = %(p)s) or b.scope = 'system') "
        "and not exists (select 1 from budget_reservations r where r.budget_id = "
        "b.budget_id and r.settlement_basis = 'reported_over_reservation')"
    )
    explicit = ctx.count(
        "select count(*) as n from budgets b where b.explicit and "
        "((b.scope = 'project' and b.scope_id = %(p)s) or b.scope = 'system')"
    )
    ok = uncapped == 0 and not overspent and explicit >= 1
    return ok, (
        f"{explicit} explicit ceiling(s); {uncapped} submitted reservation(s) without "
        f"a cap within their amount; overspent explicit ceilings: "
        f"{[dict(row) for row in overspent] or 'none'}"
    )


def _isolation(ctx: _Ctx) -> tuple[bool, str]:
    left = [
        str(row["experiment_id"])
        for row in ctx.rows(
            "select experiment_id, workspace_path from idea_experiments "
            "where project_id = %(p)s and state in "
            "('INTERPRETED','OPERATIONALLY_FAILED','SUPERSEDED')"
        )
        if row["workspace_path"] and Path(str(row["workspace_path"])).exists()
    ]
    branches = _experiment_branches_left(ctx)
    return not left and not branches, (
        f"workspaces left behind: {left or 'none'}; experiment branches left in "
        f"the repository: {branches or 'none'}"
    )


def _containment(ctx: _Ctx) -> tuple[bool, str]:
    total = ctx.count(
        "select count(*) as n from external_jobs where project_id = %(p)s "
        "and status = 'COMPLETED'"
    )
    uncontained = ctx.count(
        "select count(*) as n from external_jobs where project_id = %(p)s "
        "and status = 'COMPLETED' and contained is not true"
    )
    return total >= 1 and uncontained == 0, (
        f"{total} completed execution(s), {uncontained} not contained"
    )


def _complete_chain(ctx: _Ctx) -> tuple[bool, str]:
    from research_os.portfolio.models import ExperimentRole

    boards, metas = _board_ideas(ctx), _meta_ideas(ctx)
    provenance_by = _replication_provenance(ctx)
    for key, chains in _chains_by_version(ctx).items():
        primary = [
            c for c in chains if c.role is ExperimentRole.PRIMARY and c.admissible
        ]
        replica = [
            c for c in chains if c.role is ExperimentRole.REPLICATION and c.admissible
        ]
        trusted = {r.evidence_id for r in provenance_by.get(key, ()) if r.admissible}
        replica = [c for c in replica if c.evidence_id in trusted]
        current = ctx.store.require_version(key[0])
        if (
            primary
            and replica
            and key[0] in boards
            and key[0] in metas
            and current.version == key[1]
        ):
            return True, (
                f"{key[0]} v{key[1]}: primary {primary[0].outcome}, replication "
                f"{replica[0].outcome} (agrees={replica[0].agrees_with_primary}), "
                f"complete board, meta-review"
            )
    return False, "no idea version carries the whole chain at once"


def _second_provider(ctx: _Ctx) -> tuple[bool, str]:
    rows = ctx.rows(
        "select distinct c.provider from model_calls c join research_runs r "
        "on r.run_id = c.run_id where r.project_id = %(p)s"
    )
    providers = sorted(str(row["provider"]) for row in rows)
    return len(providers) >= 2, f"providers: {providers}"


def _human_ready(ctx: _Ctx) -> tuple[bool, str]:
    n = ctx.count(
        "select count(*) as n from ideas where project_id = %(p)s "
        "and quality_tier = 'HUMAN_READY'"
    )
    return _at_least(n, "idea(s) at HUMAN_READY")


CHECKS: dict[str, Check] = {
    "unseeded_origination": _unseeded_origination,
    "idea_provenance": _idea_provenance,
    "deduplication": lambda ctx: _stage(ctx, "dedup"),
    "falsification": lambda ctx: _stage(ctx, "falsify"),
    "executed_literature_retrieval": _retrieval,
    "curation_prioritization": _curation,
    "frozen_scientific_contract": _objects("CONTRACT"),
    "frozen_experimental_design": _objects("DESIGN"),
    "capability_resolution": _objects("PLAN"),
    "frozen_execution_plan": _frozen_plan,
    "real_scientific_execution": _execution("PRIMARY"),
    "trusted_execution_receipt": _trusted_receipts,
    "validated_result_artifact": _validated_result,
    "deterministic_primary_outcome": _primary_outcome,
    "real_replication": _execution("REPLICATION"),
    "trusted_replication_receipt": _replication_receipt,
    "replication_assessment": _replication_assessment,
    "complete_review_board": _review_board,
    "meta_review_readiness": _meta_review,
    "recursive_child": _recursive_child,
    "budget_authority": _budget_authority,
    "repository_isolation": _isolation,
    "containment": _containment,
    "complete_provenance_chain": _complete_chain,
    "second_provider": _second_provider,
    "human_ready": _human_ready,
}


# ------------------------------------------------------------ evidence --
def _evidence(gate: Mapping[str, Any], evidence_dir: Path | None) -> tuple[bool, str]:
    spec = dict(gate.get("evidence") or {})
    name = str(spec.get("file") or "")
    if evidence_dir is None:
        return False, f"no evidence directory given; {name} is required"
    path = evidence_dir / name
    if not path.is_file():
        return False, f"{name} is missing from {evidence_dir}"
    text = path.read_text(encoding="utf-8", errors="replace")
    missing = [m for m in spec.get("markers") or () if m not in text]
    forbidden = [m for m in spec.get("forbidden") or () if m in text]
    ok = not missing and not forbidden
    return ok, (
        f"{name}: missing markers {missing or 'none'}; forbidden present "
        f"{forbidden or 'none'}"
    )


def _zero_state_evidence(evidence_dir: Path | None, digest: str) -> tuple[bool, str]:
    if evidence_dir is None:
        return False, "no evidence directory given"
    path = evidence_dir / "00-zero-state.json"
    if not path.is_file():
        return False, f"00-zero-state.json is missing from {evidence_dir}"
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    nonzero = {k: v for k, v in dict(snapshot.get("counts") or {}).items() if v}
    ok = (
        snapshot.get("schema") == SNAPSHOT_SCHEMA
        and snapshot.get("spec_digest") == digest
        and not nonzero
        and set(snapshot.get("counts") or {}) == set(ZERO_STATE_COUNTS)
    )
    return ok, (
        f"snapshot at {snapshot.get('taken_at')}: nonzero {nonzero or 'none'}; "
        f"spec digest recorded {str(snapshot.get('spec_digest'))[:16]}"
    )


# ------------------------------------------------------------ snapshots --
def _capsule_fingerprint(repo_path: Path | None) -> dict[str, Any]:
    if repo_path is None:
        return {}
    from research_os.automation.gitutil import git

    capsule = git(["ls-tree", "-r", "HEAD", ".research"], cwd=repo_path).stdout
    status = git(["status", "--porcelain"], cwd=repo_path).stdout
    return {
        "head": git(["rev-parse", "HEAD"], cwd=repo_path).stdout.strip(),
        "capsule_tree_sha256": hashlib.sha256(capsule.encode("utf-8")).hexdigest(),
        "status_clean": status.strip() == "",
    }


def _experiment_branches_left(ctx: _Ctx) -> list[str]:
    """This project's experiment worktree branches still in the repository."""

    if ctx.repo_path is None:
        return []
    from research_os.automation.gitutil import branch_exists
    from research_os.automation.worktree import branch_name
    from research_os.portfolio.empirical import _worktree_run_id

    left = []
    for row in ctx.rows(
        "select experiment_id from idea_experiments where project_id = %(p)s"
    ):
        branch = branch_name(_worktree_run_id(str(row["experiment_id"])), "T-001")
        if branch_exists(ctx.repo_path, branch):
            left.append(branch)
    return left


def zero_state_snapshot(
    db: Any, *, project_id: str, repo_path: Path | None, spec_path: Path = SPEC_PATH
) -> dict[str, Any]:
    """What must be zero before a live qualification, and the spec it is held to."""

    ctx = _Ctx(db=db, project_id=project_id, artifacts=None, repo_path=repo_path)
    return {
        "schema": SNAPSHOT_SCHEMA,
        "taken_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "project_id": project_id,
        "spec_digest": spec_digest(spec_path),
        "counts": {name: ctx.count(sql) for name, sql in ZERO_STATE_COUNTS.items()},
        "repository": _capsule_fingerprint(repo_path),
    }


def isolation_report(*, zero_state: Mapping[str, Any], repo_path: Path) -> str:
    """The isolation evidence: the checkout now against the zero-state snapshot."""

    now = _capsule_fingerprint(repo_path)
    then = dict(zero_state.get("repository") or {})
    unchanged = now.get("capsule_tree_sha256") == then.get("capsule_tree_sha256")
    lines = [
        f"repository: {repo_path}",
        f"head at start: {then.get('head')}",
        f"head now: {now.get('head')}",
        f"git status: {'clean' if now.get('status_clean') else 'DIRTY'}",
        f"capsule unchanged: {'true' if unchanged else 'false'}",
    ]
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------ evaluate --
def evaluate(
    db: Any,
    *,
    project_id: str,
    artifacts: Any,
    evidence_dir: Path | None = None,
    repo_path: Path | None = None,
    spec_path: Path = SPEC_PATH,
    expect_spec_digest: str | None = None,
) -> Report:
    """Every gate of the frozen specification, decided by code. Read-only."""

    digest = spec_digest(spec_path)
    if expect_spec_digest is not None and expect_spec_digest != digest:
        raise QualificationError(
            f"the specification is {digest} and the qualification began under "
            f"{expect_spec_digest}; mandatory criteria cannot change once a live "
            f"qualification has begun"
        )
    if evidence_dir is not None and (evidence_dir / "00-zero-state.json").is_file():
        recorded = json.loads((evidence_dir / "00-zero-state.json").read_text())
        if recorded.get("spec_digest") not in {None, digest}:
            raise QualificationError(
                f"the zero-state snapshot recorded spec {recorded.get('spec_digest')} "
                f"and this specification is {digest}"
            )
    spec = load_spec(spec_path)
    ctx = _Ctx(db=db, project_id=project_id, artifacts=artifacts, repo_path=repo_path)
    results: list[GateResult] = []
    for tier in ("mandatory", "advisory"):
        for gate in spec.get(tier) or ():
            parts: list[tuple[bool, str]] = []
            if gate["name"] == "zero_state_environment":
                parts.append(_zero_state_evidence(evidence_dir, digest))
            else:
                if gate["check"] in {"automated", "both"}:
                    try:
                        parts.append(CHECKS[gate["name"]](ctx))
                    except ResearchOSError as exc:
                        parts.append((False, f"the check could not complete: {exc}"))
                if gate["check"] in {"evidence", "both"}:
                    parts.append(_evidence(gate, evidence_dir))
            results.append(
                GateResult(
                    id=gate["id"],
                    name=gate["name"],
                    title=gate["title"],
                    mandatory=tier == "mandatory",
                    passed=all(ok for ok, _ in parts),
                    detail="; ".join(text for _, text in parts),
                )
            )
    verdicts = spec["verdicts"]
    qualified = all(item.passed for item in results if item.mandatory)
    return Report(
        spec_digest=digest,
        project_id=project_id,
        verdict=verdicts["pass"] if qualified else verdicts["fail"],
        gates=tuple(results),
    )


__all__ = [
    "CHECKS",
    "SPEC_PATH",
    "QualificationError",
    "Report",
    "evaluate",
    "isolation_report",
    "load_spec",
    "spec_digest",
    "zero_state_snapshot",
]
