"""Portfolio reads and writes against one database.

Same contract as :class:`research_os.runtime.store.RuntimeStore`: short
transactions, no conversation held open, every model in
:mod:`research_os.portfolio.models` handed back validated. This module owns the
SQL; nothing above it writes a row.

Three things here are load-bearing and are worth finding quickly:

:meth:`PortfolioStore.add_edge` inserts the parent's and the child's depth by
*selecting them from ``ideas``* in the same statement, never from the caller.
The composite foreign keys then make the copies provably equal to the real
depths, and the acyclicity check on the edge is sound because of it.

:meth:`PortfolioStore.live_reviews` is where "live" is defined, and it is
defined in SQL rather than in Python so that no caller can compute it a second,
different way. A review is live when it binds the current content digest *and*
the current evidence set *and* the current prompt version, and is not older
than the configured maximum.

:meth:`PortfolioStore.resolve_objection` refuses to let the producing side mark
its own objection answered. The resolving review must exist, must be of the
current version, and must be by a different role than the one that raised it.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any

from research_os.errors import ResearchOSError
from research_os.portfolio import digests as pdigests
from research_os.portfolio.contracts import MAX_SUMMARY_CHARS
from research_os.portfolio.ids import (
    new_contract_id,
    new_idea_action_id,
    new_idea_evidence_id,
    new_idea_experiment_id,
    new_idea_id,
    new_idea_review_id,
    new_objection_id,
    new_portfolio_digest_id,
    new_provenance_id,
    new_request_id,
    new_seed_id,
)
from research_os.portfolio.models import (
    BLOCKED_STATES,
    CLOSED_IDEA_STATUSES,
    FRONTIER_CLAIM_KINDS,
    OPEN_EXPERIMENT_STATES,
    PROVENANCE_FOR_ORIGIN,
    TERMINAL_IDEA_STATUSES,
    TIER_ORDER,
    ActionStatus,
    AdjudicationType,
    ContractKind,
    ContractState,
    Disposition,
    EdgeKind,
    EmpiricalConclusion,
    EvidenceKind,
    EvidenceStrength,
    ExperimentRole,
    ExperimentState,
    FrontierRequest,
    IdeaAction,
    IdeaEdge,
    IdeaEvidence,
    IdeaExperiment,
    IdeaObjection,
    IdeaOrigin,
    IdeaProvenance,
    IdeaReview,
    IdeaStatus,
    IdeaVersion,
    LiteratureClaim,
    ObjectionTarget,
    OperationalState,
    PortfolioDigestRecord,
    PortfolioIdea,
    PortfolioSeed,
    PortfolioState,
    PortfolioStatus,
    ProvenanceBasis,
    QualityDimensions,
    QualityTier,
    RequestBasis,
    RequestKind,
    RequestState,
    ReviewerRole,
    ReviewVerdict,
    ScientificContract,
    Severity,
    Stage,
    Synthesis,
)
from research_os.runtime.db import Database, RuntimeDatabaseError, jsonb
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import Independence

LOG = logging.getLogger("research_os.portfolio.store")

IDEA_COLUMNS = (
    "idea_id, project_id, depth, lineage_root, origin, current_version, status, "
    "operational_state, quality_tier, curated_digest, curated_at, retire_reason, "
    "revisit_if, created_at, updated_at"
)
VERSION_COLUMNS = (
    "idea_id, version, title, research_question, core_idea, mechanism, "
    "why_it_matters, falsifier, adjudication_types, closest_prior_work, "
    "claimed_difference, assumptions, alternative_explanations, open_uncertainties, "
    "next_best_action, dimensions, addressed_objections, content_digest, "
    "canonical_digest, origin_call_id, origin_role, origin_stage, created_at"
)
EDGE_COLUMNS = (
    "parent_idea_id, child_idea_id, kind, parent_depth, child_depth, detail, created_at"
)
EVIDENCE_COLUMNS = (
    "evidence_id, idea_id, idea_version, kind, strength, summary, artifact_id, "
    "finding_id, job_id, literature_key, source_call_id, claim_id, created_at"
)
REVIEW_COLUMNS = (
    "review_id, idea_id, idea_version, reviewer_role, verdict, severity, summary, "
    "recommendation, detail_artifact_id, reviewed_content_digest, "
    "reviewed_evidence_digest, packet_digest, prompt_version, call_id, provider, "
    "model, provider_family, independence_vs_origin, context_class, "
    "independence_note, created_at"
)
OBJECTION_COLUMNS = (
    "objection_id, idea_id, raised_in_review, raised_at_version, objection_key, "
    "severity, target, summary, addressed_at_version, response, resolved_by_review, "
    "resolved_at, created_at"
)
EXPERIMENT_COLUMNS = (
    "experiment_id, idea_id, idea_version, project_id, role, state, command, "
    "spec_digest, variation_digest, workspace_path, preregistration_artifact_id, "
    "decision_rule, no_rule_reason, job_id, analysis_artifact_id, conclusion, "
    "evidence_id, failure_class, detail, attempts, origin_call_id, "
    "prompt_version, contract_id, created_at, updated_at"
)
CONTRACT_COLUMNS = (
    "contract_id, project_id, idea_id, idea_version, role, kind, state, "
    "hypothesis_digest, analysable, analysis_digest, analysis_artifact_id, "
    "analysis_prompt, analysis_call_id, design_digest, design_artifact_id, "
    "design_prompt, design_call_id, contract_digest, contract_artifact_id, "
    "parent_contract_id, capability_request, command_set_digest, detail, "
    "created_at, updated_at, frozen_at"
)
ACTION_COLUMNS = (
    "action_id, idea_id, idea_version, stage, basis_digest, status, work_id, "
    "thread_id, utility, disposition, detail, failure_class, cost_usd, model_calls, "
    "created_at, updated_at, completed_at"
)
STATE_COLUMNS = (
    "project_id, status, charter_digest, detail, paused_at, paused_by, "
    "last_tick_at, last_digest_at, bounds, bank_commit, bank_digest, "
    "bank_written_at, failures_forgiven_at, command_set_digest, created_at, "
    "updated_at"
)
SEED_COLUMNS = "seed_id, project_id, text, note, consumed_at, consumed_by, created_at"
REQUEST_COLUMNS = (
    "request_id, project_id, kind, basis, source_idea_id, source_version, "
    "source_ref, question, detail, state, attempts, resolution, resolved_by, "
    "created_at, updated_at"
)
SYNTHESIS_COLUMNS = (
    "synthesis_id, project_id, state, basis_digest, document_artifact_id, "
    "referee_artifact_id, writer_call_id, referee_call_id, statements, findings, "
    "referee_verdict, detail, created_at, updated_at"
)
CLAIM_COLUMNS = (
    "claim_id, project_id, kind, statement, work_keys, excerpt, verification, "
    "query, request_id, idea_id, source_call_id, artifact_id, digest, created_at"
)
PROVENANCE_COLUMNS = (
    "provenance_id, idea_id, basis, source_ref, request_id, call_id, detail, created_at"
)

#: How much of a stage's own account of itself is kept.
#:
#: The same number the contract that produces the string enforces, because a
#: store that keeps less than the producer may emit throws away the part
#: nobody chose to lose. The dogfood measured the cost of disagreeing: four
#: call sites had each picked their own literal, the smallest won at 500, the
#: column is ``text`` and holds anything, and all three of the overnight run's
#: refusals -- the only place the system says which capability a researcher
#: would have to add -- reached the record cut mid-word.
MAX_DETAIL_CHARS = MAX_SUMMARY_CHARS

_CLIP_MARKER = " [clipped]"


def clipped_detail(detail: str | None) -> str | None:
    """Bound a stage's explanation, and say so when the bound bites.

    Unbounded is not the answer either: ``detail`` is written on every action
    and a runaway string is a runaway row. What matters is that a reader can
    tell a reason that ended from a reason that was cut, which is the only
    reason the marker exists.
    """

    if detail is None:
        return None
    text = detail.strip()
    if not text:
        # What the two call sites this replaced both did, via `if detail`.
        # NULL is how this column says "nothing recorded"; an empty string
        # would be a second way to say it.
        return None
    if len(text) <= MAX_DETAIL_CHARS:
        return text
    return text[: MAX_DETAIL_CHARS - len(_CLIP_MARKER)].rstrip() + _CLIP_MARKER


#: The same version list, prefixed, for the queries that join ``ideas``. A bare
#: ``idea_id`` beside the ideas table's own is ambiguous, and PostgreSQL says
#: so rather than guessing -- which is the good outcome, but only once.
_QUALIFIED_VERSION_COLUMNS = ", ".join(
    f"v.{name.strip()}" for name in VERSION_COLUMNS.split(",")
)
DIGEST_COLUMNS = (
    "digest_id, project_id, period_start, period_end, payload, artifact_id, created_at"
)


class _Unset:
    """ "Not supplied", distinct from ``None``, which means "do not check".

    A sentinel rather than ``None`` because both are meaningful for the
    liveness filters: the default applies this build's prompt versions and
    configured maximum age, and an explicit ``None`` turns one check off for a
    test that is about a different one.
    """


UNSET = _Unset()


class PortfolioStateError(ResearchOSError):
    """Raised when a portfolio record is missing or a transition is refused."""


class ActiveTrackExistsError(PortfolioStateError):
    """Raised when an idea already has a stage in flight.

    A lost race rather than a defect: two portfolio ticks can both decide the
    same idea is next. The caller's response is to skip it, which is the truth.
    """


class DuplicateExperimentError(PortfolioStateError):
    """Raised when an idea version already has an experiment in this role.

    A lost race rather than a defect, and the reason the unique index exists:
    "an empirical idea creates exactly one experiment spec" and "replay
    creates no duplicate" are the same sentence seen from two sides, and the
    place to hold them is the schema rather than a caller's memory.
    """


class DuplicateContractError(PortfolioStateError):
    """Raised when an idea version already has a live contract in this role.

    The same race `DuplicateExperimentError` names, one level up: two passes
    both freezing an analysis for one version would be two preregistrations
    of one question, and the second is exactly the "try again until the rule
    looks better" this table exists to make unrepresentable.
    """


class DuplicateBasisError(PortfolioStateError):
    """Raised when this exact scientific basis has already been acted on.

    Also a lost race, and the whole reason ``idea_actions`` carries a basis
    digest: a replayed event must not buy the same reasoning twice.
    """


def _fields_of(version_fields: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise the caller's version fields into storable columns."""

    return {
        "title": str(version_fields.get("title", "")),
        "research_question": str(version_fields.get("research_question", "")),
        "core_idea": str(version_fields.get("core_idea", "")),
        "mechanism": str(version_fields.get("mechanism", "") or ""),
        "why_it_matters": str(version_fields.get("why_it_matters", "") or ""),
        "falsifier": str(version_fields.get("falsifier", "") or ""),
        "adjudication_types": [
            str(item) for item in (version_fields.get("adjudication_types") or ())
        ],
        "closest_prior_work": str(version_fields.get("closest_prior_work", "") or ""),
        "claimed_difference": str(version_fields.get("claimed_difference", "") or ""),
        "assumptions": [
            str(item) for item in (version_fields.get("assumptions") or ())
        ],
        "alternative_explanations": [
            str(item) for item in (version_fields.get("alternative_explanations") or ())
        ],
        "open_uncertainties": [
            str(item) for item in (version_fields.get("open_uncertainties") or ())
        ],
        "next_best_action": str(version_fields.get("next_best_action", "") or ""),
    }


#: Failure classes that mean "the system worked and said no".
#:
#: The taxonomy already calls each of these terminal. What the portfolio adds
#: is that a *stage* which produced one will produce it again: "no declared
#: command can test this idea" does not become true on the third attempt, and
#: each attempt is a paid frontier call. So one of these blocks the idea and
#: `researchctl portfolio resume` is what reconsiders it -- which is the same
#: sentence the blocked state has always meant.
REFUSAL_CLASSES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.CAPABILITY_DENIED,
        FailureClass.POLICY_REFUSED,
        FailureClass.MISSING_SCIENTIFIC_AUTHORITY,
    }
)


class PortfolioStore:
    """Portfolio reads and writes against one database."""

    __slots__ = ("_db",)

    def __init__(self, db: Database) -> None:
        self._db = db

    @property
    def db(self) -> Database:
        return self._db

    # -------------------------------------------------------------- ideas --
    def create_idea(
        self,
        *,
        project_id: str,
        origin: IdeaOrigin,
        fields: Mapping[str, Any],
        parent_idea_id: str | None = None,
        edge_kind: EdgeKind = EdgeKind.DERIVED_FROM,
        edge_detail: str = "",
        origin_call_id: str | None = None,
        origin_role: str = "",
        origin_stage: str | None = None,
        dimensions: QualityDimensions | None = None,
        provenance: tuple[ProvenanceBasis, str | None, str | None] | None = None,
    ) -> tuple[PortfolioIdea, IdeaVersion]:
        """Mint one candidate direction and its first, immutable version.

        ``provenance`` is ``(basis, source_ref, request_id)``: why this idea
        exists. When omitted it is what the origin implies, with the parent
        as its source. Written in the same transaction as the idea, so no
        idea can exist without a reason recorded for it.

        ``parent_idea_id`` sets lineage: the child's depth is the parent's plus
        one and its lineage root is the parent's, so the depth rule on
        ``idea_edges`` -- which is what makes a cycle unrepresentable -- holds
        by construction rather than by the caller getting it right.
        """

        normalised = _fields_of(fields)
        digest_input = {**normalised, "project": project_id}
        content = pdigests.content_digest(digest_input)
        canonical = pdigests.canonical_digest(digest_input)
        idea_id = new_idea_id()
        # An idea belongs to a portfolio, so the portfolio exists once an idea
        # does. Without this, `researchctl portfolio status` on a project whose
        # ideas arrived by some other route reported that there was no
        # portfolio -- while listing its ideas perfectly well.
        self.upsert_state(project_id=project_id)

        with self._db.tx() as conn:
            if parent_idea_id is None:
                depth, lineage_root = 0, idea_id
            else:
                parent = conn.execute(
                    "select depth, lineage_root from ideas where idea_id = %s",
                    (parent_idea_id,),
                ).fetchone()
                if parent is None:
                    raise PortfolioStateError(
                        f"{parent_idea_id} is not an idea in this portfolio"
                    )
                depth = int(parent["depth"]) + 1
                lineage_root = str(parent["lineage_root"])

            idea_row = conn.execute(
                f"""
                insert into ideas
                    (idea_id, project_id, depth, lineage_root, origin,
                     current_version, status, operational_state, quality_tier)
                values (%(idea_id)s, %(project_id)s, %(depth)s, %(lineage_root)s,
                        %(origin)s, 1, 'CANDIDATE', 'IDLE', 'NONE')
                returning {IDEA_COLUMNS}
                """,
                {
                    "idea_id": idea_id,
                    "project_id": project_id,
                    "depth": depth,
                    "lineage_root": lineage_root,
                    "origin": str(origin),
                },
            ).fetchone()
            version_row = self._insert_version(
                conn,
                idea_id=idea_id,
                version=1,
                normalised=normalised,
                content=content,
                canonical=canonical,
                dimensions=dimensions or QualityDimensions(),
                addressed=(),
                origin_call_id=origin_call_id,
                origin_role=origin_role,
                origin_stage=origin_stage,
            )
            if parent_idea_id is not None:
                self._insert_edge(
                    conn,
                    parent_idea_id=parent_idea_id,
                    child_idea_id=idea_id,
                    kind=edge_kind,
                    detail=edge_detail,
                )
            basis, source_ref, request_id = provenance or (
                PROVENANCE_FOR_ORIGIN[str(origin)],
                parent_idea_id,
                None,
            )
            self._insert_provenance(
                conn,
                idea_id=idea_id,
                basis=basis,
                source_ref=source_ref,
                request_id=request_id,
                call_id=origin_call_id,
                detail=f"created by {origin_role or origin}"
                + (f" at {origin_stage}" if origin_stage else ""),
            )
        return (
            PortfolioIdea.model_validate(idea_row),
            IdeaVersion.model_validate(version_row),
        )

    def append_version(
        self,
        *,
        idea_id: str,
        fields: Mapping[str, Any],
        origin_call_id: str | None = None,
        origin_role: str = "",
        origin_stage: str | None = None,
        dimensions: QualityDimensions | None = None,
        addressed_objections: Sequence[str] = (),
    ) -> IdeaVersion:
        """Append an immutable revision and make it current.

        Every review of every prior version becomes stale the moment the
        content digest moves, and this is the only place that can happen.
        ``addressed_objections`` records which standing objections this revision
        *claims* to answer; claiming is not resolving --
        :meth:`resolve_objection` requires a re-review by another role.
        """

        with self._db.tx() as conn:
            idea = conn.execute(
                "select project_id, current_version, status from ideas "
                "where idea_id = %s for update",
                (idea_id,),
            ).fetchone()
            if idea is None:
                raise PortfolioStateError(f"{idea_id} is not an idea in this portfolio")
            if IdeaStatus(str(idea["status"])) in CLOSED_IDEA_STATUSES:
                raise PortfolioStateError(
                    f"{idea_id} is {idea['status']} and takes no further versions. "
                    f"A closed idea is revived as a new idea with a REVIVES edge, "
                    f"so the record of what was rejected stays what it was."
                )
            normalised = _fields_of(fields)
            digest_input = {**normalised, "project": str(idea["project_id"])}
            version = int(idea["current_version"]) + 1
            row = self._insert_version(
                conn,
                idea_id=idea_id,
                version=version,
                normalised=normalised,
                content=pdigests.content_digest(digest_input),
                canonical=pdigests.canonical_digest(digest_input),
                dimensions=dimensions or QualityDimensions(),
                addressed=tuple(addressed_objections),
                origin_call_id=origin_call_id,
                origin_role=origin_role,
                origin_stage=origin_stage,
            )
            conn.execute(
                "update ideas set current_version = %s, updated_at = now() "
                "where idea_id = %s",
                (version, idea_id),
            )
            # An experiment measures one *version's* prediction. A revision
            # does not inherit it, and an in-flight measurement of superseded
            # text must stop being something the portfolio waits for. In the
            # same transaction as the version, because a revision that
            # committed while the retirement did not would leave the new
            # version waiting on the old one's job.
            conn.execute(
                """
                update idea_experiments
                   set state = 'SUPERSEDED',
                       detail = 'the idea version this measured was revised',
                       updated_at = now()
                 where idea_id = %(idea_id)s
                   and idea_version < %(version)s
                   and state in ('PROPOSED','EXECUTABLE','RUNNING','COMPLETED')
                """,
                {"idea_id": idea_id, "version": version},
            )
            # And the contracts those versions froze, for the same reason:
            # a contract tests one hypothesis, and the hypothesis just
            # changed. A contract under which something was *read* stays
            # FROZEN -- what was measured was measured, and the record of
            # which rule it was read under must stay the record.
            conn.execute(
                """
                update scientific_contracts c
                   set state = 'SUPERSEDED',
                       detail = 'the idea version this tested was revised',
                       updated_at = now()
                 where c.idea_id = %(idea_id)s
                   and c.idea_version < %(version)s
                   and c.state <> 'SUPERSEDED'
                   and not exists (
                       select 1 from idea_experiments e
                        where e.contract_id = c.contract_id
                          and (e.state = 'INTERPRETED' or e.evidence_id is not null
                               or e.analysis_artifact_id is not null))
                """,
                {"idea_id": idea_id, "version": version},
            )
        return IdeaVersion.model_validate(row)

    @staticmethod
    def _insert_version(
        conn: Any,
        *,
        idea_id: str,
        version: int,
        normalised: Mapping[str, Any],
        content: str,
        canonical: str,
        dimensions: QualityDimensions,
        addressed: Sequence[str],
        origin_call_id: str | None,
        origin_role: str,
        origin_stage: str | None,
    ) -> Any:
        return conn.execute(
            f"""
            insert into idea_versions
                (idea_id, version, title, research_question, core_idea, mechanism,
                 why_it_matters, falsifier, adjudication_types, closest_prior_work,
                 claimed_difference, assumptions, alternative_explanations,
                 open_uncertainties, next_best_action, dimensions,
                 addressed_objections, content_digest, canonical_digest,
                 origin_call_id, origin_role, origin_stage)
            values (%(idea_id)s, %(version)s, %(title)s, %(research_question)s,
                    %(core_idea)s, %(mechanism)s, %(why_it_matters)s, %(falsifier)s,
                    %(adjudication_types)s, %(closest_prior_work)s,
                    %(claimed_difference)s, %(assumptions)s,
                    %(alternative_explanations)s, %(open_uncertainties)s,
                    %(next_best_action)s, %(dimensions)s, %(addressed)s,
                    %(content)s, %(canonical)s, %(origin_call_id)s, %(origin_role)s,
                    %(origin_stage)s)
            returning {VERSION_COLUMNS}
            """,
            {
                **{
                    key: value
                    for key, value in normalised.items()
                    if key
                    not in {
                        "assumptions",
                        "alternative_explanations",
                        "open_uncertainties",
                    }
                },
                "idea_id": idea_id,
                "version": version,
                "assumptions": jsonb(list(normalised["assumptions"])),
                "alternative_explanations": jsonb(
                    list(normalised["alternative_explanations"])
                ),
                "open_uncertainties": jsonb(list(normalised["open_uncertainties"])),
                "dimensions": jsonb(dimensions.model_dump()),
                "addressed": jsonb(list(addressed)),
                "content": content,
                "canonical": canonical,
                "origin_call_id": origin_call_id,
                "origin_role": origin_role,
                "origin_stage": origin_stage,
            },
        ).fetchone()

    def get_idea(self, idea_id: str) -> PortfolioIdea | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {IDEA_COLUMNS} from ideas where idea_id = %s", (idea_id,)
            ).fetchone()
        return PortfolioIdea.model_validate(row) if row else None

    def require_idea(self, idea_id: str) -> PortfolioIdea:
        idea = self.get_idea(idea_id)
        if idea is None:
            raise PortfolioStateError(f"{idea_id} is not an idea in this portfolio")
        return idea

    def get_version(
        self, idea_id: str, version: int | None = None
    ) -> IdeaVersion | None:
        """One version, or the current one when ``version`` is omitted."""

        with self._db.tx() as conn:
            if version is None:
                row = conn.execute(
                    f"""
                    select {_QUALIFIED_VERSION_COLUMNS} from idea_versions v
                    join ideas i on i.idea_id = v.idea_id
                                and i.current_version = v.version
                    where v.idea_id = %s
                    """,
                    (idea_id,),
                ).fetchone()
            else:
                row = conn.execute(
                    f"select {VERSION_COLUMNS} from idea_versions "
                    "where idea_id = %s and version = %s",
                    (idea_id, version),
                ).fetchone()
        return IdeaVersion.model_validate(row) if row else None

    def require_version(self, idea_id: str, version: int | None = None) -> IdeaVersion:
        found = self.get_version(idea_id, version)
        if found is None:
            raise PortfolioStateError(
                f"{idea_id} has no version {version or 'current'}"
            )
        return found

    def list_versions(self, idea_id: str) -> tuple[IdeaVersion, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {VERSION_COLUMNS} from idea_versions where idea_id = %s "
                "order by version",
                (idea_id,),
            ).fetchall()
        return tuple(IdeaVersion.model_validate(row) for row in rows)

    def list_ideas(
        self,
        *,
        project_id: str,
        statuses: Sequence[IdeaStatus] | None = None,
        operational: Sequence[OperationalState] | None = None,
        limit: int = 500,
    ) -> tuple[PortfolioIdea, ...]:
        clauses = ["project_id = %(project_id)s"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if statuses:
            clauses.append("status = any(%(statuses)s)")
            params["statuses"] = [str(item) for item in statuses]
        if operational:
            clauses.append("operational_state = any(%(operational)s)")
            params["operational"] = [str(item) for item in operational]
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {IDEA_COLUMNS} from ideas where {' and '.join(clauses)} "
                "order by created_at desc, idea_id limit %(limit)s",
                params,
            ).fetchall()
        return tuple(PortfolioIdea.model_validate(row) for row in rows)

    def set_status(
        self,
        *,
        idea_id: str,
        status: IdeaStatus,
        retire_reason: str | None = None,
        revisit_if: str | None = None,
        expected_status: IdeaStatus | None = None,
        require_idle: bool = False,
        clear_retirement: bool = False,
    ) -> PortfolioIdea | None:
        """Move an idea's scientific status, and raise its tier high-water mark.

        ``quality_tier`` is a ``greatest``: it records how far this idea ever
        got, so a rejection after ``PROMISING`` stays distinguishable from a
        rejection as a candidate. The database will not accept a retirement
        without a reason, which is deliberate -- see
        ``sql/0019_portfolio_ideas.sql``.

        ``expected_status`` and ``require_idle`` make it a compare-and-set,
        for a caller deciding from a snapshot: the tick settles an idea it
        read a moment ago, and a worker may have finished a stage on it in
        between. When the row no longer matches, nothing changes and this
        returns ``None`` -- the decision was about a state that is gone.
        """

        tier = {
            IdeaStatus.PROMISING: QualityTier.PROMISING,
            IdeaStatus.VALIDATED: QualityTier.VALIDATED,
            IdeaStatus.HUMAN_READY: QualityTier.HUMAN_READY,
        }.get(status)
        with self._db.tx() as conn:
            current = conn.execute(
                "select status, quality_tier, operational_state from ideas "
                "where idea_id = %s for update",
                (idea_id,),
            ).fetchone()
            if current is None:
                raise PortfolioStateError(f"{idea_id} is not an idea in this portfolio")
            if expected_status is not None and (
                IdeaStatus(str(current["status"])) is not expected_status
                or (require_idle and str(current["operational_state"]) != "IDLE")
            ):
                return None
            if IdeaStatus(str(current["status"])) in CLOSED_IDEA_STATUSES:
                raise PortfolioStateError(
                    f"{idea_id} is {current['status']}; a closed idea does not move. "
                    f"Revive it as a new idea with a REVIVES edge instead."
                )
            existing_tier = QualityTier(str(current["quality_tier"]))
            if tier is not None and TIER_ORDER[tier] > TIER_ORDER[existing_tier]:
                existing_tier = tier
            row = conn.execute(
                f"""
                update ideas
                   set status = %(status)s,
                       quality_tier = %(tier)s,
                       retire_reason = case when %(clear)s then null
                                            else coalesce(%(reason)s, retire_reason) end,
                       revisit_if = case when %(clear)s then null
                                         else coalesce(%(revisit)s, revisit_if) end,
                       updated_at = now()
                 where idea_id = %(idea_id)s
                returning {IDEA_COLUMNS}
                """,
                {
                    "idea_id": idea_id,
                    "status": str(status),
                    "tier": str(existing_tier),
                    "reason": retire_reason,
                    "revisit": revisit_if,
                    "clear": clear_retirement,
                },
            ).fetchone()
        return PortfolioIdea.model_validate(row)

    def set_operational_state(
        self,
        *,
        idea_id: str,
        state: OperationalState,
        expected: OperationalState | None = None,
    ) -> PortfolioIdea | None:
        """Set what an idea is doing. With ``expected``, only from that state.

        The compare-and-set form is for a decision made from a read: blocking
        an idea on a literature answer must not overwrite ``ACTIVE`` on an
        idea a worker picked up in between, which would free its capacity
        slot while the stage is still running. Returns ``None`` when the
        expectation no longer holds.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"update ideas set operational_state = %s, updated_at = now() "
                f"where idea_id = %s and (%s::text is null or operational_state = %s) "
                f"returning {IDEA_COLUMNS}",
                (
                    str(state),
                    idea_id,
                    str(expected) if expected else None,
                    str(expected) if expected else None,
                ),
            ).fetchone()
        if row is None:
            if expected is not None and self.get_idea(idea_id) is not None:
                return None
            raise PortfolioStateError(f"{idea_id} is not an idea in this portfolio")
        return PortfolioIdea.model_validate(row)

    def unblock_ideas(self, *, project_id: str) -> int:
        """Return every blocked idea of one project to IDLE. A person's act.

        ``tick._clear_blocks`` lifts ``BLOCKED_PROVIDER`` by itself, against
        provider health it can observe, and deliberately guesses at nothing
        else: ``BLOCKED_BUDGET`` lifts when a ceiling is raised and
        ``BLOCKED_EXTERNAL`` when a missing capability appears, and neither
        is a fact the tick can read.

        A researcher typing ``portfolio resume`` *is* that fact. It is the one
        signal in the system that means "whatever was blocking these, look
        again", and without it a portfolio whose blocker was fixed -- a host
        that can now run experiments, a provider family that was installed --
        stays stopped with no command that starts it. That was true of the
        three ideas this layer's empirical route unblocked: the capability
        appeared and nothing said so.

        Terminal statuses are left alone. A REJECTED idea is not blocked, it
        is finished.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                update ideas
                   set operational_state = 'IDLE', updated_at = now()
                 where project_id = %(project_id)s
                   and operational_state = any(%(blocked)s)
                   and status <> all(%(terminal)s)
                returning idea_id
                """,
                {
                    "project_id": project_id,
                    "blocked": [str(item) for item in sorted(BLOCKED_STATES)],
                    "terminal": [str(item) for item in sorted(TERMINAL_IDEA_STATUSES)],
                },
            ).fetchall()
        return len(rows)

    # -------------------------------------------------------------- edges --
    def add_edge(
        self,
        *,
        parent_idea_id: str,
        child_idea_id: str,
        kind: EdgeKind,
        detail: str = "",
    ) -> IdeaEdge:
        with self._db.tx() as conn:
            row = self._insert_edge(
                conn,
                parent_idea_id=parent_idea_id,
                child_idea_id=child_idea_id,
                kind=kind,
                detail=detail,
            )
        return IdeaEdge.model_validate(row)

    @staticmethod
    def _insert_edge(
        conn: Any,
        *,
        parent_idea_id: str,
        child_idea_id: str,
        kind: EdgeKind,
        detail: str,
    ) -> Any:
        """Insert an edge with both depths read from ``ideas`` in one statement.

        The depths are never taken from the caller. That, plus the composite
        foreign keys on ``(idea_id, depth)``, is what makes the acyclicity
        check on this table sound: a lineage edge must strictly increase depth,
        the depths on the edge provably equal the ideas' own, and PostgreSQL
        refuses to update an idea's depth while an edge references it.
        """

        row = conn.execute(
            f"""
            insert into idea_edges
                (parent_idea_id, child_idea_id, kind, parent_depth, child_depth,
                 detail)
            select p.idea_id, c.idea_id, %(kind)s, p.depth, c.depth, %(detail)s
              from ideas p, ideas c
             where p.idea_id = %(parent)s and c.idea_id = %(child)s
            on conflict (parent_idea_id, child_idea_id, kind) do nothing
            returning {EDGE_COLUMNS}
            """,
            {
                "parent": parent_idea_id,
                "child": child_idea_id,
                "kind": str(kind),
                "detail": detail,
            },
        ).fetchone()
        if row is None:
            row = conn.execute(
                f"select {EDGE_COLUMNS} from idea_edges where parent_idea_id = %s "
                "and child_idea_id = %s and kind = %s",
                (parent_idea_id, child_idea_id, str(kind)),
            ).fetchone()
        if row is None:
            raise PortfolioStateError(
                f"could not link {parent_idea_id} -> {child_idea_id} ({kind}); "
                f"one of them is not an idea in this portfolio"
            )
        return row

    def edges_of(self, idea_id: str) -> tuple[IdeaEdge, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {EDGE_COLUMNS} from idea_edges "
                "where parent_idea_id = %(id)s or child_idea_id = %(id)s "
                "order by created_at, kind",
                {"id": idea_id},
            ).fetchall()
        return tuple(IdeaEdge.model_validate(row) for row in rows)

    def ancestors(self, idea_id: str, *, limit: int = 200) -> tuple[str, ...]:
        """Lineage ancestors, nearest first.

        The recursive term carries a ``cycle`` clause. The depth rule makes a
        cycle unrepresentable, and a recursive CTE that meets one does not
        terminate -- so the clause costs nothing and removes the one way a
        corrupt row could hang the control plane rather than fail a query.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                with recursive up (idea_id, generation) as (
                    select e.parent_idea_id, 1
                      from idea_edges e
                     where e.child_idea_id = %(id)s and e.is_lineage
                    union all
                    select e.parent_idea_id, up.generation + 1
                      from idea_edges e
                      join up on e.child_idea_id = up.idea_id
                     where e.is_lineage and up.generation < %(limit)s
                ) cycle idea_id set looped using path
                select distinct on (idea_id) idea_id, generation
                  from up
                 order by idea_id, generation
                """,
                {"id": idea_id, "limit": limit},
            ).fetchall()
        return tuple(
            str(row["idea_id"])
            for row in sorted(
                rows, key=lambda item: (item["generation"], item["idea_id"])
            )
        )

    def descendants(self, idea_id: str, *, limit: int = 200) -> tuple[str, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                """
                with recursive down (idea_id, generation) as (
                    select e.child_idea_id, 1
                      from idea_edges e
                     where e.parent_idea_id = %(id)s and e.is_lineage
                    union all
                    select e.child_idea_id, down.generation + 1
                      from idea_edges e
                      join down on e.parent_idea_id = down.idea_id
                     where e.is_lineage and down.generation < %(limit)s
                ) cycle idea_id set looped using path
                select distinct on (idea_id) idea_id, generation
                  from down
                 order by idea_id, generation
                """,
                {"id": idea_id, "limit": limit},
            ).fetchall()
        return tuple(
            str(row["idea_id"])
            for row in sorted(
                rows, key=lambda item: (item["generation"], item["idea_id"])
            )
        )

    def duplicate_survivor(self, idea_id: str, *, limit: int = 32) -> str:
        """Follow ``DUPLICATE_OF`` to the idea that actually survived.

        At most one outgoing ``DUPLICATE_OF`` per idea is a unique index, so the
        chain is a path rather than a tree. Mutual duplication is still
        expressible, hence the visited set: a pair pointing at each other
        resolves to the first one reached rather than looping.
        """

        seen: set[str] = {idea_id}
        current = idea_id
        with self._db.tx() as conn:
            for _ in range(limit):
                row = conn.execute(
                    "select parent_idea_id from idea_edges "
                    "where child_idea_id = %s and kind = 'DUPLICATE_OF'",
                    (current,),
                ).fetchone()
                if row is None:
                    return current
                nxt = str(row["parent_idea_id"])
                if nxt in seen:
                    return current
                seen.add(nxt)
                current = nxt
        return current

    # ---------------------------------------------------------- duplicates --
    def find_by_content_digest(
        self, *, project_id: str, content_digest: str
    ) -> tuple[str, int] | None:
        with self._db.tx() as conn:
            row = conn.execute(
                """
                select v.idea_id, v.version from idea_versions v
                  join ideas i on i.idea_id = v.idea_id
                 where i.project_id = %s and v.content_digest = %s
                 order by v.created_at limit 1
                """,
                (project_id, content_digest),
            ).fetchone()
        return (str(row["idea_id"]), int(row["version"])) if row else None

    def find_by_canonical_digest(
        self, *, project_id: str, canonical_digest: str, exclude: str | None = None
    ) -> str | None:
        with self._db.tx() as conn:
            row = conn.execute(
                """
                select v.idea_id from idea_versions v
                  join ideas i on i.idea_id = v.idea_id
                 where i.project_id = %(project_id)s
                   and v.canonical_digest = %(canonical)s
                   and (%(exclude)s::text is null or v.idea_id <> %(exclude)s)
                 order by v.created_at limit 1
                """,
                {
                    "project_id": project_id,
                    "canonical": canonical_digest,
                    "exclude": exclude,
                },
            ).fetchone()
        return str(row["idea_id"]) if row else None

    def similarity_corpus(
        self, *, project_id: str, exclude: str | None = None, limit: int = 400
    ) -> tuple[tuple[str, str, str], ...]:
        """``(idea_id, research_question, core_idea)`` for the local dedup screen.

        Current versions only, and every idea regardless of status -- a
        candidate that duplicates something already rejected is still a
        duplicate, and saying so is cheaper than investigating it again.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select v.idea_id, v.research_question, v.core_idea
                  from idea_versions v
                  join ideas i on i.idea_id = v.idea_id
                               and i.current_version = v.version
                 where i.project_id = %(project_id)s
                   and (%(exclude)s::text is null or v.idea_id <> %(exclude)s)
                 order by i.updated_at desc
                 limit %(limit)s
                """,
                {"project_id": project_id, "exclude": exclude, "limit": limit},
            ).fetchall()
        return tuple(
            (str(row["idea_id"]), str(row["research_question"]), str(row["core_idea"]))
            for row in rows
        )

    # ------------------------------------------------------------ evidence --
    def add_evidence(
        self,
        *,
        idea_id: str,
        idea_version: int,
        kind: EvidenceKind,
        strength: EvidenceStrength,
        summary: str,
        artifact_id: str | None = None,
        finding_id: str | None = None,
        job_id: str | None = None,
        literature_key: str | None = None,
        source_call_id: str | None = None,
        claim_id: str | None = None,
    ) -> IdeaEvidence:
        """Link one piece of evidence to one idea version.

        Four of this table's constraints are scientific policy and all four are
        checked by PostgreSQL, not here: a numerical witness cannot be recorded
        as ``SUPPORTS``; literature evidence must name a retrieved source;
        experiment evidence must name an execution; and nothing may be stored
        with no reference under it at all. A caller that gets one wrong gets an
        integrity error rather than a row.
        """

        with self._db.tx() as conn:
            row = self._insert_evidence(
                conn,
                idea_id=idea_id,
                idea_version=idea_version,
                kind=kind,
                strength=strength,
                summary=summary,
                artifact_id=artifact_id,
                finding_id=finding_id,
                job_id=job_id,
                literature_key=literature_key,
                source_call_id=source_call_id,
                claim_id=claim_id,
            )
        return IdeaEvidence.model_validate(row)

    @staticmethod
    def _insert_evidence(
        conn: Any,
        *,
        idea_id: str,
        idea_version: int,
        kind: EvidenceKind,
        strength: EvidenceStrength,
        summary: str,
        artifact_id: str | None = None,
        finding_id: str | None = None,
        job_id: str | None = None,
        literature_key: str | None = None,
        source_call_id: str | None = None,
        claim_id: str | None = None,
    ) -> Any:
        evidence_id = new_idea_evidence_id()
        row = conn.execute(
            f"""
            insert into idea_evidence
                (evidence_id, idea_id, idea_version, kind, strength, summary,
                 artifact_id, finding_id, job_id, literature_key, source_call_id,
                 claim_id)
            values (%(evidence_id)s, %(idea_id)s, %(version)s, %(kind)s,
                    %(strength)s, %(summary)s, %(artifact_id)s, %(finding_id)s,
                    %(job_id)s, %(literature_key)s, %(call_id)s, %(claim_id)s)
            on conflict (idea_id, idea_version, claim_id)
                where claim_id is not null do nothing
            returning {EVIDENCE_COLUMNS}
            """,
            {
                "evidence_id": evidence_id,
                "idea_id": idea_id,
                "version": idea_version,
                "kind": str(kind),
                "strength": str(strength),
                "summary": summary,
                "artifact_id": artifact_id,
                "finding_id": finding_id,
                "job_id": job_id,
                "literature_key": literature_key,
                "call_id": source_call_id,
                "claim_id": claim_id,
            },
        ).fetchone()
        if row is None:
            # The same claim on the same version: a replay. The row that is
            # already there is the answer.
            row = conn.execute(
                f"select {EVIDENCE_COLUMNS} from idea_evidence "
                "where idea_id = %s and idea_version = %s and claim_id = %s",
                (idea_id, idea_version, claim_id),
            ).fetchone()
        return row

    def record_reading(
        self,
        experiment_id: str,
        *,
        evidence: Mapping[str, Any],
        analysis_artifact_id: str,
        conclusion: EmpiricalConclusion,
        detail: str,
    ) -> tuple[IdeaExperiment, str]:
        """The evidence row and ``INTERPRETED``, in one transaction.

        They were two, and the gap between them was a hole: a crash after the
        evidence row and before the state left an experiment that had been
        *read* looking unread, and a prompt bump in that window retired its
        contract and froze a second preregistration of the same question on
        the same version -- which was then measured and read too. One
        transaction, with the experiment locked, so either both happened or
        neither did; and an experiment that already names evidence keeps it.
        """

        with self._db.tx() as conn:
            current = conn.execute(
                f"select {EXPERIMENT_COLUMNS} from idea_experiments "
                "where experiment_id = %s for update",
                (experiment_id,),
            ).fetchone()
            if current is None:
                raise PortfolioStateError(f"no such experiment: {experiment_id}")
            evidence_id = current["evidence_id"]
            if evidence_id is None:
                evidence_id = self._insert_evidence(conn, **dict(evidence))[
                    "evidence_id"
                ]
            row = conn.execute(
                f"""
                update idea_experiments
                   set state = 'INTERPRETED',
                       analysis_artifact_id = coalesce(analysis_artifact_id, %(analysis)s),
                       conclusion = coalesce(conclusion, %(conclusion)s),
                       evidence_id = %(evidence_id)s,
                       failure_class = null,
                       detail = %(detail)s,
                       updated_at = now()
                 where experiment_id = %(experiment_id)s
                returning {EXPERIMENT_COLUMNS}
                """,
                {
                    "experiment_id": experiment_id,
                    "analysis": analysis_artifact_id,
                    "conclusion": str(conclusion),
                    "evidence_id": evidence_id,
                    "detail": clipped_detail(detail),
                },
            ).fetchone()
        return IdeaExperiment.model_validate(row), str(evidence_id)

    def list_evidence(
        self, *, idea_id: str, idea_version: int | None = None
    ) -> tuple[IdeaEvidence, ...]:
        with self._db.tx() as conn:
            if idea_version is None:
                rows = conn.execute(
                    f"select {EVIDENCE_COLUMNS} from idea_evidence "
                    "where idea_id = %s order by created_at, evidence_id",
                    (idea_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"select {EVIDENCE_COLUMNS} from idea_evidence "
                    "where idea_id = %s and idea_version = %s "
                    "order by created_at, evidence_id",
                    (idea_id, idea_version),
                ).fetchall()
        return tuple(IdeaEvidence.model_validate(row) for row in rows)

    def evidence_digest(self, *, idea_id: str, idea_version: int) -> str:
        """The digest of the evidence set a review of this version would read."""

        return pdigests.evidence_set_digest(
            item.evidence_id
            for item in self.list_evidence(idea_id=idea_id, idea_version=idea_version)
        )

    # --------------------------------------------------------- experiments --
    def create_experiment(
        self,
        *,
        idea_id: str,
        idea_version: int,
        project_id: str,
        role: ExperimentRole,
        command: str,
        spec_digest: str,
        variation_digest: str,
        workspace_path: str,
        decision_rule: Mapping[str, Any] | None,
        no_rule_reason: str | None,
        preregistration_artifact_id: str | None = None,
        origin_call_id: str | None = None,
        experiment_id: str | None = None,
        prompt_version: str = "",
        contract_id: str | None = None,
        supersedes: tuple[str, str] | None = None,
    ) -> IdeaExperiment:
        """Record the one experiment this idea version asks for in this role.

        ``supersedes`` -- ``(experiment_id, detail)`` -- retires an
        operationally failed execution in the same transaction, which is how
        an implementation repair replaces one.

        ``experiment_id`` is supplied by the caller when the id is already
        load-bearing, and for the empirical bridge it is: the disposable
        workspace path is derived from it and the workspace path is inside
        the specification digest, so minting a second id here would produce a
        row whose recorded digest describes a directory nothing will ever run
        in. That is not hypothetical -- it was the first thing the tests
        found.

        Raises :class:`DuplicateExperimentError` when one already exists. That
        is not a defect and is the reason the unique index is in the schema: a
        replayed work item, a reclaimed lease and a duplicated portfolio tick
        must all be unable to design a second experiment for one version, and
        a caller that could catch "already there" and carry on is a caller
        that submits twice.
        """

        experiment_id = experiment_id or new_idea_experiment_id()
        try:
            with self._db.tx() as conn:
                if supersedes is not None:
                    # The repair's two writes as one: retiring the failed
                    # execution and recording its successor. Apart, a crash
                    # between them left a frozen contract whose only
                    # execution was SUPERSEDED -- which `design` then refused
                    # to redesign, permanently.
                    retired = conn.execute(
                        """
                        update idea_experiments
                           set state = 'SUPERSEDED', detail = %s, updated_at = now()
                         where experiment_id = %s and state = 'OPERATIONALLY_FAILED'
                        returning experiment_id
                        """,
                        (clipped_detail(supersedes[1]), supersedes[0]),
                    ).fetchone()
                    if retired is None:
                        raise PortfolioStateError(
                            f"{supersedes[0]} is not an operationally failed "
                            f"execution; there is nothing to repair"
                        )
                row = conn.execute(
                    f"""
                    insert into idea_experiments
                        (experiment_id, idea_id, idea_version, project_id, role,
                         state, command, spec_digest, variation_digest,
                         workspace_path, decision_rule, no_rule_reason,
                         preregistration_artifact_id, origin_call_id,
                         prompt_version, contract_id)
                    values (%(experiment_id)s, %(idea_id)s, %(version)s,
                            %(project_id)s, %(role)s, 'PROPOSED', %(command)s,
                            %(spec_digest)s, %(variation_digest)s, %(workspace)s,
                            %(rule)s, %(reason)s, %(prereg)s, %(call_id)s,
                            %(prompt_version)s, %(contract_id)s)
                    returning {EXPERIMENT_COLUMNS}
                    """,
                    {
                        "experiment_id": experiment_id,
                        "idea_id": idea_id,
                        "version": idea_version,
                        "project_id": project_id,
                        "role": str(role),
                        "command": command,
                        "spec_digest": spec_digest,
                        "variation_digest": variation_digest,
                        "workspace": workspace_path,
                        "rule": jsonb(dict(decision_rule)) if decision_rule else None,
                        "reason": no_rule_reason,
                        "prereg": preregistration_artifact_id,
                        "call_id": origin_call_id,
                        "prompt_version": prompt_version,
                        "contract_id": contract_id,
                    },
                ).fetchone()
        except RuntimeDatabaseError:
            # `Database.tx` has already turned the driver's constraint
            # violation into this. Which constraint it was is answered by
            # looking, rather than by matching on a message: the unique index
            # is the only way this insert can conflict, and a row being there
            # is the fact the caller needs either way.
            existing = self.get_experiment(
                idea_id=idea_id, idea_version=idea_version, role=role
            )
            if existing is None:
                raise
            raise DuplicateExperimentError(
                f"{idea_id} v{idea_version} already has a {role} experiment "
                f"({existing.experiment_id}); designing a second one is how a "
                f"replay becomes a duplicate measurement"
            ) from None
        return IdeaExperiment.model_validate(row)

    def get_experiment(
        self,
        *,
        idea_id: str,
        idea_version: int,
        role: ExperimentRole = ExperimentRole.PRIMARY,
    ) -> IdeaExperiment | None:
        """The *live* experiment for this idea version and role, if there is one.

        Superseded rows are excluded, and the partial unique index is what
        makes "the live one" singular. They are excluded rather than ordered
        past because a superseded experiment is history -- a design that was
        made, and a record of why it stopped being the one being asked for --
        and every caller that asks this question wants the one that is still
        owed something. :meth:`require_experiment` reaches a retired one by
        id.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"select {EXPERIMENT_COLUMNS} from idea_experiments "
                "where idea_id = %s and idea_version = %s and role = %s "
                "and state <> 'SUPERSEDED'",
                (idea_id, idea_version, str(role)),
            ).fetchone()
        return IdeaExperiment.model_validate(row) if row else None

    def require_experiment(self, experiment_id: str) -> IdeaExperiment:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {EXPERIMENT_COLUMNS} from idea_experiments "
                "where experiment_id = %s",
                (experiment_id,),
            ).fetchone()
        if row is None:
            raise PortfolioStateError(f"no such experiment: {experiment_id}")
        return IdeaExperiment.model_validate(row)

    def list_experiments(
        self, *, idea_id: str, idea_version: int | None = None
    ) -> tuple[IdeaExperiment, ...]:
        with self._db.tx() as conn:
            if idea_version is None:
                rows = conn.execute(
                    f"select {EXPERIMENT_COLUMNS} from idea_experiments "
                    "where idea_id = %s order by created_at, experiment_id",
                    (idea_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    f"select {EXPERIMENT_COLUMNS} from idea_experiments "
                    "where idea_id = %s and idea_version = %s "
                    "order by created_at, experiment_id",
                    (idea_id, idea_version),
                ).fetchall()
        return tuple(IdeaExperiment.model_validate(row) for row in rows)

    def update_experiment(
        self,
        experiment_id: str,
        *,
        state: ExperimentState,
        job_id: str | None = None,
        analysis_artifact_id: str | None = None,
        conclusion: EmpiricalConclusion | None = None,
        evidence_id: str | None = None,
        failure_class: str | None = None,
        detail: str | None = None,
        count_attempt: bool = False,
    ) -> IdeaExperiment:
        """Move an experiment forward, keeping everything already established.

        Every nullable reference is ``coalesce``d, so a later step cannot
        erase an earlier one's record by not repeating it. An experiment that
        ran, failed to be analysed, and was analysed on the retry keeps the
        job the first attempt submitted -- which is what makes retrying safe
        rather than a second submission.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update idea_experiments
                   set state = %(state)s,
                       job_id = coalesce(%(job_id)s, job_id),
                       analysis_artifact_id =
                           coalesce(%(analysis)s, analysis_artifact_id),
                       conclusion = coalesce(%(conclusion)s, conclusion),
                       evidence_id = coalesce(%(evidence_id)s, evidence_id),
                       failure_class = %(failure_class)s,
                       detail = %(detail)s,
                       attempts = attempts + case when %(count)s then 1 else 0 end,
                       updated_at = now()
                 where experiment_id = %(experiment_id)s
                returning {EXPERIMENT_COLUMNS}
                """,
                {
                    "experiment_id": experiment_id,
                    "state": str(state),
                    "job_id": job_id,
                    "analysis": analysis_artifact_id,
                    "conclusion": str(conclusion) if conclusion else None,
                    "evidence_id": evidence_id,
                    "failure_class": failure_class,
                    "detail": clipped_detail(detail),
                    "count": count_attempt,
                },
            ).fetchone()
        if row is None:
            raise PortfolioStateError(f"no such experiment: {experiment_id}")
        return IdeaExperiment.model_validate(row)

    def supersede_experiments_below(self, *, idea_id: str, version: int) -> int:
        """Retire every open experiment of a version older than ``version``.

        Called when a revision appends a new version. An experiment measures
        one *version's* prediction, so a revision does not inherit it -- and
        an in-flight measurement of superseded text must stop being something
        the portfolio waits for. Terminal rows are left exactly as they are:
        what was measured was measured, and the record of it is the point.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                update idea_experiments
                   set state = 'SUPERSEDED',
                       detail = 'the idea version this measured was revised',
                       updated_at = now()
                 where idea_id = %(idea_id)s
                   and idea_version < %(version)s
                   and state = any(%(open)s)
                returning experiment_id
                """,
                {
                    "idea_id": idea_id,
                    "version": version,
                    "open": [str(item) for item in sorted(OPEN_EXPERIMENT_STATES)],
                },
            ).fetchall()
        return len(rows)

    # ----------------------------------------------------------- provenance --
    @staticmethod
    def _insert_provenance(
        conn: Any,
        *,
        idea_id: str,
        basis: ProvenanceBasis,
        source_ref: str | None,
        request_id: str | None,
        call_id: str | None,
        detail: str,
    ) -> Any:
        return conn.execute(
            f"""
            insert into idea_provenance
                (provenance_id, project_id, idea_id, basis, source_ref, request_id,
                 call_id, detail)
            select %s, i.project_id, i.idea_id, %s, %s, %s, %s, %s
              from ideas i where i.idea_id = %s
            returning {PROVENANCE_COLUMNS}
            """,
            (
                new_provenance_id(),
                str(basis),
                source_ref,
                request_id,
                call_id,
                detail[:2_000],
                idea_id,
            ),
        ).fetchone()

    def record_provenance(
        self,
        *,
        idea_id: str,
        basis: ProvenanceBasis,
        source_ref: str | None = None,
        request_id: str | None = None,
        call_id: str | None = None,
        detail: str = "",
    ) -> IdeaProvenance:
        """Record one more reason an idea exists. Append-only, by trigger."""

        with self._db.tx() as conn:
            row = self._insert_provenance(
                conn,
                idea_id=idea_id,
                basis=basis,
                source_ref=source_ref,
                request_id=request_id,
                call_id=call_id,
                detail=detail,
            )
        return IdeaProvenance.model_validate(row)

    def provenance_of(self, idea_id: str) -> tuple[IdeaProvenance, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {PROVENANCE_COLUMNS} from idea_provenance where idea_id = %s "
                "order by created_at, provenance_id",
                (idea_id,),
            ).fetchall()
        return tuple(IdeaProvenance.model_validate(row) for row in rows)

    # ------------------------------------------------------------ syntheses --
    def create_synthesis(
        self,
        *,
        project_id: str,
        basis_digest: str,
        document_artifact_id: str,
        writer_call_id: str | None,
        statements: int,
    ) -> Synthesis:
        from research_os.portfolio.ids import new_synthesis_id

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into syntheses
                    (synthesis_id, project_id, basis_digest, document_artifact_id,
                     writer_call_id, statements)
                values (%s, %s, %s, %s, %s, %s)
                on conflict (project_id, basis_digest) do nothing
                returning {SYNTHESIS_COLUMNS}
                """,
                (
                    new_synthesis_id(),
                    project_id,
                    basis_digest,
                    document_artifact_id,
                    writer_call_id,
                    statements,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"select {SYNTHESIS_COLUMNS} from syntheses "
                    "where project_id = %s and basis_digest = %s",
                    (project_id, basis_digest),
                ).fetchone()
            # A newer basis supersedes every older synthesis of the project.
            conn.execute(
                "update syntheses set state = 'SUPERSEDED', updated_at = now() "
                "where project_id = %s and basis_digest <> %s and state <> 'SUPERSEDED'",
                (project_id, basis_digest),
            )
        return Synthesis.model_validate(row)

    def synthesis_for_basis(
        self, *, project_id: str, basis_digest: str
    ) -> Synthesis | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {SYNTHESIS_COLUMNS} from syntheses "
                "where project_id = %s and basis_digest = %s",
                (project_id, basis_digest),
            ).fetchone()
        return Synthesis.model_validate(row) if row else None

    def referee_synthesis(
        self,
        synthesis_id: str,
        *,
        referee_artifact_id: str,
        referee_call_id: str | None,
        verdict: str,
        findings: int,
    ) -> Synthesis:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update syntheses
                   set state = 'REFEREED', referee_artifact_id = %s,
                       referee_call_id = %s, referee_verdict = %s, findings = %s,
                       updated_at = now()
                 where synthesis_id = %s and state = 'DRAFTED'
                returning {SYNTHESIS_COLUMNS}
                """,
                (referee_artifact_id, referee_call_id, verdict, findings, synthesis_id),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"select {SYNTHESIS_COLUMNS} from syntheses where synthesis_id = %s",
                    (synthesis_id,),
                ).fetchone()
        return Synthesis.model_validate(row)

    def list_syntheses(
        self, *, project_id: str, limit: int = 20
    ) -> tuple[Synthesis, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {SYNTHESIS_COLUMNS} from syntheses where project_id = %s "
                "order by created_at desc, synthesis_id limit %s",
                (project_id, limit),
            ).fetchall()
        return tuple(Synthesis.model_validate(row) for row in rows)

    # ----------------------------------------------------- literature claims --
    def record_literature_claim(
        self,
        *,
        project_id: str,
        kind: str,
        statement: str,
        work_keys: Sequence[str],
        excerpt: str,
        verification: str,
        query: str,
        digest: str,
        request_id: str | None = None,
        idea_id: str | None = None,
        source_call_id: str | None = None,
        artifact_id: str | None = None,
    ) -> LiteratureClaim:
        """Store one verified claim. Idempotent on its content digest.

        The caller has already verified it -- every key supplied, every
        quotation found -- and the database refuses one with no key at all.
        The same statement about the same works read twice is one claim.
        """

        from research_os.portfolio.ids import new_claim_id

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into literature_claims
                    (claim_id, project_id, kind, statement, work_keys, excerpt,
                     verification, query, request_id, idea_id, source_call_id,
                     artifact_id, digest)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (project_id, digest) do nothing
                returning {CLAIM_COLUMNS}
                """,
                (
                    new_claim_id(),
                    project_id,
                    kind,
                    statement[:4_000],
                    list(work_keys),
                    excerpt,
                    verification,
                    query[:2_000],
                    request_id,
                    idea_id,
                    source_call_id,
                    artifact_id,
                    digest,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"select {CLAIM_COLUMNS} from literature_claims "
                    "where project_id = %s and digest = %s",
                    (project_id, digest),
                ).fetchone()
        return LiteratureClaim.model_validate(row)

    def list_literature_claims(
        self,
        *,
        project_id: str,
        idea_id: str | None = None,
        frontier_only: bool = False,
        limit: int = 200,
    ) -> tuple[LiteratureClaim, ...]:
        clauses = ["project_id = %(project_id)s"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if idea_id is not None:
            clauses.append("idea_id = %(idea_id)s")
            params["idea_id"] = idea_id
        if frontier_only:
            clauses.append("kind = any(%(kinds)s)")
            params["kinds"] = sorted(str(item) for item in FRONTIER_CLAIM_KINDS)
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {CLAIM_COLUMNS} from literature_claims "
                f"where {' and '.join(clauses)} "
                "order by created_at desc, claim_id limit %(limit)s",
                params,
            ).fetchall()
        return tuple(LiteratureClaim.model_validate(row) for row in rows)

    def get_literature_claim(self, claim_id: str) -> LiteratureClaim | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {CLAIM_COLUMNS} from literature_claims where claim_id = %s",
                (claim_id,),
            ).fetchone()
        return LiteratureClaim.model_validate(row) if row else None

    # ----------------------------------------------------- frontier requests --
    def open_request(
        self,
        *,
        project_id: str,
        kind: RequestKind,
        basis: RequestBasis,
        source_ref: str,
        question: str,
        source_idea_id: str | None = None,
        source_version: int | None = None,
        detail: str = "",
    ) -> FrontierRequest:
        """Record one question owed to the frontier. Idempotent per raising event.

        The unique index on ``(project, kind, basis, source_ref)`` makes a
        replayed stage find the row instead of writing a second one, so the
        number of follow-ups is bounded by the number of *events*, not by the
        number of retries.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into frontier_requests
                    (request_id, project_id, kind, basis, source_idea_id,
                     source_version, source_ref, question, detail)
                values (%s, %s, %s, %s, %s, %s, %s, %s, %s)
                on conflict (project_id, kind, basis, source_ref) do nothing
                returning {REQUEST_COLUMNS}
                """,
                (
                    new_request_id(),
                    project_id,
                    str(kind),
                    str(basis),
                    source_idea_id,
                    source_version,
                    source_ref,
                    question,
                    detail or None,
                ),
            ).fetchone()
            if row is None:
                row = conn.execute(
                    f"select {REQUEST_COLUMNS} from frontier_requests "
                    "where project_id = %s and kind = %s and basis = %s "
                    "and source_ref = %s",
                    (project_id, str(kind), str(basis), source_ref),
                ).fetchone()
        return FrontierRequest.model_validate(row)

    def get_request(self, request_id: str) -> FrontierRequest | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {REQUEST_COLUMNS} from frontier_requests where request_id = %s",
                (request_id,),
            ).fetchone()
        return FrontierRequest.model_validate(row) if row else None

    def require_request(self, request_id: str) -> FrontierRequest:
        found = self.get_request(request_id)
        if found is None:
            raise PortfolioStateError(f"no such frontier request: {request_id}")
        return found

    def list_requests(
        self,
        *,
        project_id: str,
        states: Sequence[RequestState] | None = None,
        kinds: Sequence[RequestKind] | None = None,
        source_idea_id: str | None = None,
        limit: int = 500,
    ) -> tuple[FrontierRequest, ...]:
        clauses = ["project_id = %(project_id)s"]
        params: dict[str, Any] = {"project_id": project_id, "limit": limit}
        if states:
            clauses.append("state = any(%(states)s)")
            params["states"] = [str(item) for item in states]
        if kinds:
            clauses.append("kind = any(%(kinds)s)")
            params["kinds"] = [str(item) for item in kinds]
        if source_idea_id is not None:
            clauses.append("source_idea_id = %(source)s")
            params["source"] = source_idea_id
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {REQUEST_COLUMNS} from frontier_requests "
                f"where {' and '.join(clauses)} "
                "order by created_at, request_id limit %(limit)s",
                params,
            ).fetchall()
        return tuple(FrontierRequest.model_validate(row) for row in rows)

    def close_request(
        self,
        request_id: str,
        *,
        state: RequestState,
        resolution: str,
        resolved_by: str | None = None,
    ) -> FrontierRequest:
        if state is RequestState.OPEN:
            raise PortfolioStateError("closing a request needs a closed state")
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update frontier_requests
                   set state = %s, resolution = %s, resolved_by = %s,
                       updated_at = now()
                 where request_id = %s and state = 'OPEN'
                returning {REQUEST_COLUMNS}
                """,
                (str(state), resolution[:4_000] or "closed", resolved_by, request_id),
            ).fetchone()
        if row is None:
            return self.require_request(request_id)
        return FrontierRequest.model_validate(row)

    def count_request_attempt(self, request_id: str) -> int:
        with self._db.tx() as conn:
            row = conn.execute(
                "update frontier_requests set attempts = attempts + 1, "
                "updated_at = now() where request_id = %s returning attempts",
                (request_id,),
            ).fetchone()
        return int(row["attempts"]) if row else 0

    def request_generations(
        self, *, project_id: str, kind: str
    ) -> dict[str, tuple[int, int]]:
        """``(finished, failed)`` work items of one kind, per request.

        The generation a request's next work item is keyed on, read from the
        queue rather than from a column every handler must remember to bump.
        ``work_items.dedup_key`` is permanently unique, so a key built from a
        count that did not move -- a provider outage, a spent budget, a
        crash, a deferral -- is a retry ``enqueue`` refuses silently, and
        because the allocator serves the oldest request first, one spent key
        wedged every later request of that kind. ``finished`` counts every
        terminal item and names the generation; ``failed`` is what the
        ceiling reads.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select w.payload->>'request_id' as request_id,
                       count(*) filter (
                           where w.status in ('SUCCEEDED','FAILED','CANCELLED')) as finished,
                       count(*) filter (where w.status = 'FAILED') as failed
                  from work_items w
                 where w.project_id = %(project_id)s and w.kind = %(kind)s
                   and w.payload->>'request_id' is not null
                 group by 1
                """,
                {"project_id": project_id, "kind": kind},
            ).fetchall()
        return {
            str(row["request_id"]): (int(row["finished"]), int(row["failed"]))
            for row in rows
        }

    def synthesis_generations(self, *, project_id: str) -> dict[str, int]:
        """Finished synthesis work items per basis: a generation and a ceiling.

        A synthesis that succeeds makes its basis no longer due, so every
        finished item on a basis that is still due was a failure or a refusal
        -- which is what the ceiling counts.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select w.payload->>'basis' as basis, count(*) as n
                  from work_items w
                 where w.project_id = %(project_id)s
                   and w.kind = 'portfolio_synthesize'
                   and w.status in ('SUCCEEDED','FAILED','CANCELLED')
                   and w.payload->>'basis' is not null
                 group by 1
                """,
                {"project_id": project_id},
            ).fetchall()
        return {str(row["basis"]): int(row["n"]) for row in rows}

    def provenance_for_request(self, request_id: str) -> tuple[IdeaProvenance, ...]:
        """Every provenance row that names this request: what it already made."""

        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {PROVENANCE_COLUMNS} from idea_provenance "
                "where request_id = %s order by created_at, provenance_id",
                (request_id,),
            ).fetchall()
        return tuple(IdeaProvenance.model_validate(row) for row in rows)

    def work_in_flight(self, *, project_id: str, kind: str) -> int:
        """Queued or running work of one kind for this project."""

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(*) as n from work_items w
                 where w.project_id = %(project_id)s and w.kind = %(kind)s
                   and w.status in ('PENDING', 'LEASED', 'WAITING')
                """,
                {"project_id": project_id, "kind": kind},
            ).fetchone()
        return int(row["n"]) if row else 0

    # ----------------------------------------------------------- contracts --
    def create_contract(
        self,
        *,
        project_id: str,
        idea_id: str,
        idea_version: int,
        role: ExperimentRole,
        hypothesis_digest: str,
        analysable: bool,
        analysis_digest: str,
        analysis_artifact_id: str,
        analysis_prompt: str = "",
        analysis_call_id: str | None = None,
        kind: ContractKind = ContractKind.PREREGISTERED,
        parent_contract_id: str | None = None,
        contract_id: str | None = None,
        design: Mapping[str, Any] | None = None,
    ) -> ScientificContract:
        """Freeze an analysis: the first half of a scientific contract.

        ``design`` is for the one case that freezes both halves in one act --
        an exploratory contract, which re-reads its parent's measurement under
        a new rule and so inherits the parent's design. It is a mapping of the
        design columns and is refused for a preregistered contract, whose
        design must be authored *after* its analysis is frozen.
        """

        if design is not None and kind is ContractKind.PREREGISTERED:
            raise PortfolioStateError(
                "a preregistered contract freezes its analysis before its design "
                "exists; the two halves cannot be written in one act"
            )
        contract_id = contract_id or new_contract_id()
        state = (
            ContractState.FROZEN
            if design is not None
            else ContractState.ANALYSIS_FROZEN
        )
        columns = dict(design or {})
        try:
            with self._db.tx() as conn:
                row = conn.execute(
                    f"""
                    insert into scientific_contracts
                        (contract_id, project_id, idea_id, idea_version, role, kind,
                         state, hypothesis_digest, analysable, analysis_digest,
                         analysis_artifact_id, analysis_prompt, analysis_call_id,
                         parent_contract_id, design_digest, design_artifact_id,
                         design_prompt, design_call_id, contract_digest,
                         contract_artifact_id, frozen_at)
                    values (%(contract_id)s, %(project_id)s, %(idea_id)s,
                            %(version)s, %(role)s, %(kind)s, %(state)s,
                            %(hypothesis)s, %(analysable)s, %(analysis)s,
                            %(analysis_artifact)s, %(analysis_prompt)s,
                            %(analysis_call)s, %(parent)s, %(design_digest)s,
                            %(design_artifact)s, %(design_prompt)s,
                            %(design_call)s, %(contract_digest)s,
                            %(contract_artifact)s,
                            case when %(frozen)s then now() else null end)
                    returning {CONTRACT_COLUMNS}
                    """,
                    {
                        "contract_id": contract_id,
                        "project_id": project_id,
                        "idea_id": idea_id,
                        "version": idea_version,
                        "role": str(role),
                        "kind": str(kind),
                        "state": str(state),
                        "hypothesis": hypothesis_digest,
                        "analysable": analysable,
                        "analysis": analysis_digest,
                        "analysis_artifact": analysis_artifact_id,
                        "analysis_prompt": analysis_prompt,
                        "analysis_call": analysis_call_id,
                        "parent": parent_contract_id,
                        "design_digest": columns.get("design_digest"),
                        "design_artifact": columns.get("design_artifact_id"),
                        "design_prompt": columns.get("design_prompt"),
                        "design_call": columns.get("design_call_id"),
                        "contract_digest": columns.get("contract_digest"),
                        "contract_artifact": columns.get("contract_artifact_id"),
                        "frozen": design is not None,
                    },
                ).fetchone()
        except RuntimeDatabaseError:
            if kind is ContractKind.PREREGISTERED:
                existing = self.live_contract(
                    idea_id=idea_id, idea_version=idea_version, role=role
                )
                if existing is not None:
                    raise DuplicateContractError(
                        f"{idea_id} v{idea_version} already has a live {role} "
                        f"contract ({existing.contract_id})"
                    ) from None
            raise
        return ScientificContract.model_validate(row)

    def freeze_contract(
        self,
        contract_id: str,
        *,
        design_digest: str,
        design_artifact_id: str,
        contract_digest: str,
        contract_artifact_id: str,
        design_prompt: str = "",
        design_call_id: str | None = None,
    ) -> ScientificContract:
        """Freeze the design half, and the contract with it.

        Only from ``ANALYSIS_FROZEN`` or ``BLOCKED_CAPABILITY``. The database
        trigger refuses any later change to what this writes; the ``where``
        clause refuses writing it twice.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update scientific_contracts
                   set state = 'FROZEN',
                       design_digest = %(design_digest)s,
                       design_artifact_id = %(design_artifact)s,
                       design_prompt = %(design_prompt)s,
                       design_call_id = %(design_call)s,
                       contract_digest = %(contract_digest)s,
                       contract_artifact_id = %(contract_artifact)s,
                       frozen_at = now(),
                       updated_at = now()
                 where contract_id = %(contract_id)s
                   and state in ('ANALYSIS_FROZEN','BLOCKED_CAPABILITY')
                   and design_digest is null
                returning {CONTRACT_COLUMNS}
                """,
                {
                    "contract_id": contract_id,
                    "design_digest": design_digest,
                    "design_artifact": design_artifact_id,
                    "design_prompt": design_prompt,
                    "design_call": design_call_id,
                    "contract_digest": contract_digest,
                    "contract_artifact": contract_artifact_id,
                },
            ).fetchone()
        if row is None:
            current = self.require_contract(contract_id)
            raise PortfolioStateError(
                f"{contract_id} is {current.state}; its design is already frozen "
                f"or it no longer takes one"
            )
        return ScientificContract.model_validate(row)

    def block_contract_on_capability(
        self,
        contract_id: str,
        *,
        capability_request: Mapping[str, Any],
        command_set_digest: str,
        detail: str,
    ) -> ScientificContract:
        """Record that no declared command can produce what the analysis reads."""

        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update scientific_contracts
                   set state = 'BLOCKED_CAPABILITY',
                       capability_request = %(request)s,
                       command_set_digest = %(commands)s,
                       detail = %(detail)s,
                       updated_at = now()
                 where contract_id = %(contract_id)s
                   and state in ('ANALYSIS_FROZEN','BLOCKED_CAPABILITY')
                returning {CONTRACT_COLUMNS}
                """,
                {
                    "contract_id": contract_id,
                    "request": jsonb(dict(capability_request)),
                    "commands": command_set_digest,
                    "detail": clipped_detail(detail),
                },
            ).fetchone()
        if row is None:
            current = self.require_contract(contract_id)
            raise PortfolioStateError(
                f"{contract_id} is {current.state} and cannot be capability-blocked"
            )
        return ScientificContract.model_validate(row)

    def supersede_contract(
        self, contract_id: str, *, detail: str
    ) -> ScientificContract:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update scientific_contracts
                   set state = 'SUPERSEDED', detail = %(detail)s, updated_at = now()
                 where contract_id = %(contract_id)s
                returning {CONTRACT_COLUMNS}
                """,
                {"contract_id": contract_id, "detail": clipped_detail(detail)},
            ).fetchone()
        if row is None:
            raise PortfolioStateError(f"no such contract: {contract_id}")
        return ScientificContract.model_validate(row)

    def get_contract(self, contract_id: str) -> ScientificContract | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {CONTRACT_COLUMNS} from scientific_contracts "
                "where contract_id = %s",
                (contract_id,),
            ).fetchone()
        return ScientificContract.model_validate(row) if row else None

    def require_contract(self, contract_id: str) -> ScientificContract:
        found = self.get_contract(contract_id)
        if found is None:
            raise PortfolioStateError(f"no such contract: {contract_id}")
        return found

    def live_contract(
        self, *, idea_id: str, idea_version: int, role: ExperimentRole
    ) -> ScientificContract | None:
        """The one preregistered contract still being asked for, if any."""

        with self._db.tx() as conn:
            row = conn.execute(
                f"select {CONTRACT_COLUMNS} from scientific_contracts "
                "where idea_id = %s and idea_version = %s and role = %s "
                "and kind = 'PREREGISTERED' and state <> 'SUPERSEDED'",
                (idea_id, idea_version, str(role)),
            ).fetchone()
        return ScientificContract.model_validate(row) if row else None

    def list_contracts(
        self,
        *,
        project_id: str | None = None,
        idea_id: str | None = None,
        states: Sequence[ContractState] | None = None,
        limit: int = 500,
    ) -> tuple[ScientificContract, ...]:
        clauses: list[str] = []
        params: dict[str, Any] = {"limit": limit}
        if project_id is not None:
            clauses.append("project_id = %(project_id)s")
            params["project_id"] = project_id
        if idea_id is not None:
            clauses.append("idea_id = %(idea_id)s")
            params["idea_id"] = idea_id
        if states:
            clauses.append("state = any(%(states)s)")
            params["states"] = [str(item) for item in states]
        where = f"where {' and '.join(clauses)}" if clauses else ""
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {CONTRACT_COLUMNS} from scientific_contracts {where} "
                "order by created_at, contract_id limit %(limit)s",
                params,
            ).fetchall()
        return tuple(ScientificContract.model_validate(row) for row in rows)

    def set_command_set_digest(self, *, project_id: str, digest: str) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "update portfolio_state set command_set_digest = %s, "
                "updated_at = now() where project_id = %s",
                (digest, project_id),
            )

    # ------------------------------------------------------------- reviews --
    def record_review(
        self,
        *,
        idea_id: str,
        idea_version: int,
        reviewer_role: ReviewerRole,
        verdict: ReviewVerdict,
        severity: Severity,
        summary: str,
        reviewed_content_digest: str,
        reviewed_evidence_digest: str,
        packet_digest: str,
        prompt_version: str,
        provider: str,
        provider_family: str,
        independence_vs_origin: Independence,
        context_class: str,
        model: str | None = None,
        call_id: str | None = None,
        recommendation: Disposition | None = None,
        detail_artifact_id: str | None = None,
        independence_note: str = "",
    ) -> tuple[IdeaReview, bool]:
        """Record one review. Returns ``(review, created)``.

        Idempotent on ``(idea, version, role, content digest, evidence
        digest)``: the same reviewer asked the same question about the same
        science twice is one review, which is what makes a replayed stage free
        rather than expensive.
        """

        review_id = new_idea_review_id()
        params = {
            "review_id": review_id,
            "idea_id": idea_id,
            "version": idea_version,
            "role": str(reviewer_role),
            "verdict": str(verdict),
            "severity": str(severity),
            "summary": summary,
            "recommendation": str(recommendation) if recommendation else None,
            "detail_artifact_id": detail_artifact_id,
            "content": reviewed_content_digest,
            "evidence": reviewed_evidence_digest,
            "packet": packet_digest,
            "prompt_version": prompt_version,
            "call_id": call_id,
            "provider": provider,
            "model": model,
            "family": provider_family,
            "independence": str(independence_vs_origin),
            "context": context_class,
            "note": independence_note,
        }
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into idea_reviews
                    (review_id, idea_id, idea_version, reviewer_role, verdict,
                     severity, summary, recommendation, detail_artifact_id,
                     reviewed_content_digest, reviewed_evidence_digest,
                     packet_digest, prompt_version, call_id, provider, model,
                     provider_family, independence_vs_origin, context_class,
                     independence_note)
                values (%(review_id)s, %(idea_id)s, %(version)s, %(role)s,
                        %(verdict)s, %(severity)s, %(summary)s, %(recommendation)s,
                        %(detail_artifact_id)s, %(content)s, %(evidence)s,
                        %(packet)s, %(prompt_version)s, %(call_id)s, %(provider)s,
                        %(model)s, %(family)s, %(independence)s, %(context)s,
                        %(note)s)
                on conflict (idea_id, idea_version, reviewer_role,
                             reviewed_content_digest, reviewed_evidence_digest)
                    do nothing
                returning {REVIEW_COLUMNS}
                """,
                params,
            ).fetchone()
            if row is not None:
                return IdeaReview.model_validate(row), True
            existing = conn.execute(
                f"""
                select {REVIEW_COLUMNS} from idea_reviews
                 where idea_id = %s and idea_version = %s and reviewer_role = %s
                   and reviewed_content_digest = %s and reviewed_evidence_digest = %s
                """,
                (
                    idea_id,
                    idea_version,
                    str(reviewer_role),
                    reviewed_content_digest,
                    reviewed_evidence_digest,
                ),
            ).fetchone()
        if existing is None:  # pragma: no cover - the conflict target guarantees it
            raise PortfolioStateError(f"could not record a review of {idea_id}")
        return IdeaReview.model_validate(existing), False

    def list_reviews(
        self, *, idea_id: str, idea_version: int | None = None
    ) -> tuple[IdeaReview, ...]:
        clauses = ["idea_id = %(idea_id)s"]
        params: dict[str, Any] = {"idea_id": idea_id}
        if idea_version is not None:
            clauses.append("idea_version = %(version)s")
            params["version"] = idea_version
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {REVIEW_COLUMNS} from idea_reviews "
                f"where {' and '.join(clauses)} order by created_at, review_id",
                params,
            ).fetchall()
        return tuple(IdeaReview.model_validate(row) for row in rows)

    def live_reviews(
        self,
        *,
        idea_id: str,
        current_prompt_versions: Mapping[str, str] | None | _Unset = UNSET,
        max_age_seconds: int | None | _Unset = UNSET,
    ) -> tuple[IdeaReview, ...]:
        """The reviews a gate may count. Defined once, in SQL.

        A review is live when **all** of these hold, and every one of them was
        added because leaving it out was a way to promote something on a review
        that had stopped being about it:

        - it binds the idea's *current* version and that version's content
          digest -- a material revision stales it;
        - it binds that version's *current evidence set* -- swapping the
          evidence under a standing approval stales it, which is the rule
          ``docs/CAPSULE.md`` already applies to a Claim's Review;
        - its prompt version is the one this build would use now -- a review
          produced by a superseded prompt answered a question no longer being
          asked;
        - it is not older than ``max_age_seconds``. A parked idea unparked six
          months later has reviews nobody revisited and literature that has
          moved.

        **The last two default to this build's values, and that is deliberate.**
        They were optional once, with ``None`` meaning "skip this check", and
        six of the nine callers took the default -- so the architecture's claim
        that liveness "is defined once" was false, and the most consequential
        divergence was a livelock: ``track._basis_for`` counted a stale review
        in the basis while ``select_stage`` counted it missing, so the track
        demanded a review board it then refused as a duplicate basis, forever.
        An independent test audit found it. Passing ``None`` explicitly still
        disables a check, for a test that is about one of the others.
        """

        from research_os.portfolio.config import load_config
        from research_os.portfolio.prompts import CURRENT_REVIEW_PROMPTS

        if isinstance(current_prompt_versions, _Unset):
            current_prompt_versions = CURRENT_REVIEW_PROMPTS
        if isinstance(max_age_seconds, _Unset):
            max_age_seconds = load_config().thresholds.review_max_age_seconds

        evidence = None
        with self._db.tx() as conn:
            head = conn.execute(
                """
                select v.version, v.content_digest from idea_versions v
                  join ideas i on i.idea_id = v.idea_id
                               and i.current_version = v.version
                 where v.idea_id = %s
                """,
                (idea_id,),
            ).fetchone()
            if head is None:
                return ()
            version = int(head["version"])
            evidence_rows = conn.execute(
                "select evidence_id from idea_evidence "
                "where idea_id = %s and idea_version = %s",
                (idea_id, version),
            ).fetchall()
            evidence = pdigests.evidence_set_digest(
                str(row["evidence_id"]) for row in evidence_rows
            )
            rows = conn.execute(
                f"""
                select {REVIEW_COLUMNS} from idea_reviews
                 where idea_id = %(idea_id)s
                   and idea_version = %(version)s
                   and reviewed_content_digest = %(content)s
                   and reviewed_evidence_digest = %(evidence)s
                   and (%(max_age)s::double precision is null
                        or created_at > now() - make_interval(secs => %(max_age)s))
                 order by created_at, review_id
                """,
                {
                    "idea_id": idea_id,
                    "version": version,
                    "content": str(head["content_digest"]),
                    "evidence": evidence,
                    "max_age": float(max_age_seconds) if max_age_seconds else None,
                },
            ).fetchall()
        found = tuple(IdeaReview.model_validate(row) for row in rows)
        if not current_prompt_versions:
            return found
        # The prompt check is applied in Python because the current prompt
        # version is a property of this *build*, not of the database, and a
        # table of them would be a second place for it to be wrong.
        return tuple(
            review
            for review in found
            if current_prompt_versions.get(str(review.reviewer_role))
            in (None, review.prompt_version)
        )

    # ---------------------------------------------------------- objections --
    def raise_objection(
        self,
        *,
        idea_id: str,
        review_id: str,
        raised_at_version: int,
        severity: Severity,
        summary: str,
        target: ObjectionTarget = ObjectionTarget.CLAIM,
    ) -> tuple[IdeaObjection, bool]:
        """Record one objection against an idea. Returns ``(objection, created)``.

        Keyed by the normalised text, so the same objection raised again -- by
        another reviewer, or at a later version after a revision claimed to
        answer it -- is recognisably the same objection and not a new one.
        """

        if severity is Severity.NONE:
            raise PortfolioStateError(
                "an objection with no severity is not an objection"
            )
        key = pdigests.objection_key(summary)
        objection_id = new_objection_id()
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into idea_objections
                    (objection_id, idea_id, raised_in_review, raised_at_version,
                     objection_key, severity, target, summary)
                values (%(objection_id)s, %(idea_id)s, %(review_id)s, %(version)s,
                        %(key)s, %(severity)s, %(target)s, %(summary)s)
                on conflict (idea_id, objection_key, raised_at_version) do nothing
                returning {OBJECTION_COLUMNS}
                """,
                {
                    "objection_id": objection_id,
                    "idea_id": idea_id,
                    "review_id": review_id,
                    "version": raised_at_version,
                    "key": key,
                    "severity": str(severity),
                    "target": str(target),
                    "summary": summary,
                },
            ).fetchone()
            if row is not None:
                return IdeaObjection.model_validate(row), True
            existing = conn.execute(
                f"select {OBJECTION_COLUMNS} from idea_objections "
                "where idea_id = %s and objection_key = %s and raised_at_version = %s",
                (idea_id, key, raised_at_version),
            ).fetchone()
        if existing is None:  # pragma: no cover
            raise PortfolioStateError(
                f"could not record an objection against {idea_id}"
            )
        return IdeaObjection.model_validate(existing), False

    def open_objections(
        self, *, idea_id: str, minimum: Severity | None = None
    ) -> tuple[IdeaObjection, ...]:
        """Standing objections, of any version, that nothing has answered.

        *Of any version*, and that is the point. An objection raised at version
        2 is still standing at version 5 unless version 5 answered it and a
        re-review confirmed the answer. Without that, rewording the mechanism
        would clear a FATAL objection, and "revise until a stochastic reviewer
        forgets" would be a working strategy.
        """

        order = {
            Severity.MINOR: 1,
            Severity.MAJOR: 2,
            Severity.CRITICAL: 3,
            Severity.FATAL: 4,
        }
        wanted = (
            [name for name, rank in order.items() if rank >= order[minimum]]
            if minimum
            else list(order)
        )
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {OBJECTION_COLUMNS} from idea_objections "
                "where idea_id = %s and resolved_at is null and severity = any(%s) "
                "order by raised_at_version, created_at",
                (idea_id, [str(item) for item in wanted]),
            ).fetchall()
        return tuple(IdeaObjection.model_validate(row) for row in rows)

    def resolve_objection(
        self,
        *,
        objection_id: str,
        addressed_at_version: int,
        response: str,
        resolved_by_review: str,
    ) -> IdeaObjection:
        """Mark one objection answered, and refuse if the answer is self-granted.

        Four conditions, and each closes a way the producing side could clear
        its own objection:

        - the resolving review must exist and be of ``addressed_at_version``,
          so an approval of an older version cannot retire a newer objection;
        - it must be by a *different role* than the one that raised it;
        - ``addressed_at_version`` must be later than the version objected to,
          which the schema also enforces -- an objection cannot be answered in
          the text it objects to;
        - the version must actually claim to have addressed it, via
          ``addressed_objections``. A revision that never mentioned the
          objection did not answer it by accident.
        """

        with self._db.tx() as conn:
            objection = conn.execute(
                "select idea_id, objection_key, raised_at_version, raised_in_review "
                "from idea_objections where objection_id = %s for update",
                (objection_id,),
            ).fetchone()
            if objection is None:
                raise PortfolioStateError(f"{objection_id} is not an objection")
            raiser = conn.execute(
                "select reviewer_role from idea_reviews where review_id = %s",
                (str(objection["raised_in_review"]),),
            ).fetchone()
            resolver = conn.execute(
                "select reviewer_role, idea_id, idea_version from idea_reviews "
                "where review_id = %s",
                (resolved_by_review,),
            ).fetchone()
            if resolver is None:
                raise PortfolioStateError(
                    f"{resolved_by_review} is not a review, and an objection is only "
                    f"resolved by a review that looked at the answer"
                )
            if str(resolver["idea_id"]) != str(objection["idea_id"]):
                raise PortfolioStateError("the resolving review is of a different idea")
            if int(resolver["idea_version"]) != addressed_at_version:
                raise PortfolioStateError(
                    f"the resolving review is of version {resolver['idea_version']}, "
                    f"not the version {addressed_at_version} that claims to answer this"
                )
            if raiser is not None and str(resolver["reviewer_role"]) == str(
                raiser["reviewer_role"]
            ):
                raise PortfolioStateError(
                    f"a {resolver['reviewer_role']} may not resolve an objection a "
                    f"{raiser['reviewer_role']} raised. An objection is answered to "
                    f"somebody else's satisfaction or it is not answered."
                )
            claimed = conn.execute(
                "select addressed_objections from idea_versions "
                "where idea_id = %s and version = %s",
                (str(objection["idea_id"]), addressed_at_version),
            ).fetchone()
            keys = list(claimed["addressed_objections"]) if claimed else []
            if str(objection["objection_key"]) not in keys:
                raise PortfolioStateError(
                    f"version {addressed_at_version} of {objection['idea_id']} does "
                    f"not claim to address this objection; a revision answers an "
                    f"objection by naming it"
                )
            row = conn.execute(
                f"""
                update idea_objections
                   set addressed_at_version = %(version)s,
                       response = %(response)s,
                       resolved_by_review = %(review)s,
                       resolved_at = now()
                 where objection_id = %(objection_id)s
                returning {OBJECTION_COLUMNS}
                """,
                {
                    "objection_id": objection_id,
                    "version": addressed_at_version,
                    "response": response,
                    "review": resolved_by_review,
                },
            ).fetchone()
        return IdeaObjection.model_validate(row)

    def carry_objections_forward(
        self, *, from_idea_id: str, to_idea_id: str, review_id: str
    ) -> int:
        """Copy a retired idea's standing objections onto its revival or merge.

        A revived idea inherits no reviews -- the binding is to a version and
        this is a different idea -- and without this it would inherit no
        *obligation* either, so reviving would launder a fatal objection into
        a clean slate. Returns how many were carried.
        """

        carried = 0
        for objection in self.open_objections(idea_id=from_idea_id):
            _, created = self.raise_objection(
                idea_id=to_idea_id,
                review_id=review_id,
                raised_at_version=1,
                severity=objection.severity,
                summary=objection.summary,
            )
            carried += int(created)
        return carried

    # ------------------------------------------------------------- actions --
    def open_action(
        self,
        *,
        idea_id: str,
        idea_version: int,
        stage: Stage,
        basis_digest: str,
        utility: Decimal | float | None = None,
        work_id: str | None = None,
        thread_id: str | None = None,
    ) -> IdeaAction:
        """Claim the one active track slot for this idea.

        Raises :class:`ActiveTrackExistsError` when something is already in
        flight and :class:`DuplicateBasisError` when this exact scientific
        basis already has a running or successful action. Both are lost races
        rather than defects: two portfolio ticks can reach the same conclusion.
        """

        action_id = new_idea_action_id()
        with self._db.tx() as conn:
            busy = conn.execute(
                "select action_id from idea_actions "
                "where idea_id = %s and status = 'ACTIVE'",
                (idea_id,),
            ).fetchone()
            if busy is not None:
                raise ActiveTrackExistsError(
                    f"{idea_id} already has {busy['action_id']} in flight"
                )
            done = conn.execute(
                """
                select action_id from idea_actions
                 where idea_id = %s and idea_version = %s and stage = %s
                   and basis_digest = %s and status in ('ACTIVE','SUCCEEDED')
                """,
                (idea_id, idea_version, str(stage), basis_digest),
            ).fetchone()
            if done is not None:
                raise DuplicateBasisError(
                    f"{stage} has already run against this basis of {idea_id} "
                    f"as {done['action_id']}"
                )
            row = conn.execute(
                f"""
                insert into idea_actions
                    (action_id, idea_id, idea_version, stage, basis_digest, status,
                     work_id, thread_id, utility)
                values (%(action_id)s, %(idea_id)s, %(version)s, %(stage)s,
                        %(basis)s, 'ACTIVE', %(work_id)s, %(thread_id)s, %(utility)s)
                returning {ACTION_COLUMNS}
                """,
                {
                    "action_id": action_id,
                    "idea_id": idea_id,
                    "version": idea_version,
                    "stage": str(stage),
                    "basis": basis_digest,
                    "work_id": work_id,
                    "thread_id": thread_id,
                    "utility": Decimal(str(utility)) if utility is not None else None,
                },
            ).fetchone()
            conn.execute(
                "update ideas set operational_state = 'ACTIVE', updated_at = now() "
                "where idea_id = %s",
                (idea_id,),
            )
        return IdeaAction.model_validate(row)

    def complete_action(
        self,
        *,
        action_id: str,
        status: ActionStatus,
        disposition: Disposition | None = None,
        detail: str | None = None,
        failure_class: str | None = None,
        cost_usd: Decimal | float = 0,
        model_calls: int = 0,
        operational_state: OperationalState = OperationalState.IDLE,
    ) -> IdeaAction:
        if status is ActionStatus.ACTIVE:
            raise PortfolioStateError("completing an action means it is not active")
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update idea_actions
                   set status = %(status)s,
                       disposition = %(disposition)s,
                       detail = %(detail)s,
                       failure_class = %(failure_class)s,
                       cost_usd = %(cost)s,
                       model_calls = %(calls)s,
                       updated_at = now(),
                       completed_at = now()
                 where action_id = %(action_id)s and status = 'ACTIVE'
                returning {ACTION_COLUMNS}
                """,
                {
                    "action_id": action_id,
                    "status": str(status),
                    "disposition": str(disposition) if disposition else None,
                    "detail": clipped_detail(detail),
                    "failure_class": failure_class,
                    "cost": Decimal(str(cost_usd)),
                    "calls": int(model_calls),
                },
            ).fetchone()
            if row is None:
                raise PortfolioStateError(
                    f"{action_id} is not an active action; it was completed already"
                )
            conn.execute(
                "update ideas set operational_state = %s, updated_at = now() "
                "where idea_id = %s",
                (str(operational_state), str(row["idea_id"])),
            )
        return IdeaAction.model_validate(row)

    def active_action(self, idea_id: str) -> IdeaAction | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {ACTION_COLUMNS} from idea_actions "
                "where idea_id = %s and status = 'ACTIVE'",
                (idea_id,),
            ).fetchone()
        return IdeaAction.model_validate(row) if row else None

    def list_actions(self, *, idea_id: str, limit: int = 200) -> tuple[IdeaAction, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {ACTION_COLUMNS} from idea_actions where idea_id = %s "
                "order by created_at, action_id limit %s",
                (idea_id, limit),
            ).fetchall()
        return tuple(IdeaAction.model_validate(row) for row in rows)

    def completed_stages(
        self, *, idea_id: str, idea_version: int, basis_digest: str
    ) -> frozenset[Stage]:
        """Which stages have already succeeded against this exact basis."""

        with self._db.tx() as conn:
            rows = conn.execute(
                "select distinct stage from idea_actions "
                "where idea_id = %s and idea_version = %s and basis_digest = %s "
                "and status = 'SUCCEEDED'",
                (idea_id, idea_version, basis_digest),
            ).fetchall()
        return frozenset(Stage(str(row["stage"])) for row in rows)

    def succeeded_stages_for_version(
        self, *, idea_id: str, idea_version: int
    ) -> frozenset[Stage]:
        """Which stages have succeeded against *any* basis of this version.

        Distinct from :meth:`completed_stages` and both are needed. The basis
        changes whenever evidence or a review is added, so "has the falsifier
        run on this version" and "has the falsifier run on exactly this basis"
        are different questions: the first decides whether to run the stage at
        all, the second makes a replay free.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                "select distinct stage from idea_actions "
                "where idea_id = %s and idea_version = %s and status = 'SUCCEEDED'",
                (idea_id, idea_version),
            ).fetchall()
        return frozenset(Stage(str(row["stage"])) for row in rows)

    def set_adjudication_types(
        self, *, idea_id: str, version: int, types: Sequence[str]
    ) -> None:
        """Write the adjudication types the falsifier implies, in place.

        One of two columns on an otherwise append-only table that is updated,
        and the reason is the same as ``dimensions``': it is *derived*, not
        authored. The falsifier is in ``content_digest`` and the types are a
        pure function of it, so writing them changes no review's binding --
        while appending a version to write them would stale every review and
        re-run every cheap stage, to record a value nobody wrote.

        Only ``run_adjudicate`` calls this, and it computes the value with
        ``runtime.adjudication.classify``. Nothing a model returns reaches
        here.
        """

        with self._db.tx() as conn:
            conn.execute(
                "update idea_versions set adjudication_types = %s "
                "where idea_id = %s and version = %s",
                ([str(item) for item in types], idea_id, version),
            )

    def revision_count(self, idea_id: str) -> int:
        """How many times this idea has been *rewritten*, not versioned.

        Counts versions produced by the ``discover`` stage. The distinction
        matters because the revision bound exists to stop "revise until a
        stochastic reviewer stops objecting", and a version appended by
        ``adjudicate`` -- which writes the adjudication type read from the
        falsifier and changes no prose -- is not a rewrite. Counting every
        version would spend the bound on bookkeeping.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from idea_versions "
                "where idea_id = %s and origin_stage = 'discover'",
                (idea_id,),
            ).fetchone()
        return int(row["n"])

    def review_count(self, idea_id: str) -> int:
        """Every review this idea has attracted, across every version."""

        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from idea_reviews where idea_id = %s",
                (idea_id,),
            ).fetchone()
        return int(row["n"])

    def lineage_family(self, idea_id: str) -> tuple[str, ...]:
        """Every idea sharing this one's lineage root, including itself."""

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select i.idea_id from ideas i
                 where i.lineage_root = (
                     select lineage_root from ideas where idea_id = %s
                 )
                 order by i.created_at, i.idea_id
                """,
                (idea_id,),
            ).fetchall()
        return tuple(str(row["idea_id"]) for row in rows)

    def depth_without_evidence(self, idea_id: str) -> int:
        """How many lineage levels have passed with no new evidence.

        Measured as this idea's depth minus the depth of the deepest ancestor
        (or itself) that has any evidence row. Bounds "deepening on reasoning
        alone", which is the way a portfolio can spend indefinitely while
        looking busy.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                with recursive up (idea_id, depth) as (
                    select i.idea_id, i.depth from ideas i where i.idea_id = %(id)s
                    union all
                    select p.idea_id, p.depth
                      from idea_edges e
                      join up on e.child_idea_id = up.idea_id
                      join ideas p on p.idea_id = e.parent_idea_id
                     where e.is_lineage
                ) cycle idea_id set looped using path
                select
                    (select depth from ideas where idea_id = %(id)s) as here,
                    coalesce(max(up.depth) filter (
                        where exists (
                            select 1 from idea_evidence ev
                             where ev.idea_id = up.idea_id
                        )
                    ), -1) as grounded
                  from up
                """,
                {"id": idea_id},
            ).fetchone()
        if row is None:
            return 0
        grounded = int(row["grounded"])
        return 0 if grounded < 0 else max(0, int(row["here"]) - grounded)

    def barren_explorations(self, *, project_id: str) -> int:
        """Explorer runs that succeeded and left the portfolio no new idea.

        Counted from the newest idea rather than from a stored counter, so it
        needs no column and cannot drift from the thing it describes: if the
        last idea is older than the last six successful explorations, then six
        explorations produced nothing, whatever any counter says.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(*) as n
                  from work_items w
                 where w.project_id = %(project_id)s
                   and w.kind = 'portfolio_explore'
                   and w.status = 'SUCCEEDED'
                   and w.created_at > coalesce(
                         (select max(created_at) from ideas
                           where project_id = %(project_id)s),
                         '-infinity'::timestamptz)
                """,
                {"project_id": project_id},
            ).fetchone()
        return int(row["n"]) if row else 0

    def explorations_in_flight(self, *, project_id: str) -> int:
        """Explorer work that is queued or running for this project.

        The allocator buys one explorer per tick, which bounds a single tick
        and bounds nothing across ticks: with a 120 s cadence and an explorer
        that takes longer, every tick adds another. One in flight at a time is
        the bound, and it costs nothing in the ordinary case because an
        explorer that finishes inside one cadence never blocks the next.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(*) as n
                  from work_items w
                 where w.project_id = %(project_id)s
                   and w.kind = 'portfolio_explore'
                   and w.status in ('PENDING', 'LEASED', 'WAITING')
                """,
                {"project_id": project_id},
            ).fetchone()
        return int(row["n"]) if row else 0

    def stale_actions(
        self, *, project_id: str, older_than_seconds: float, limit: int = 50
    ) -> tuple[IdeaAction, ...]:
        """Active actions whose work item can no longer advance them.

        The portfolio's equivalent of ``RuntimeStore.stranded_runs``, and it
        exists for the same reason: a worker killed between claiming a stage
        and completing it leaves an idea ACTIVE forever, which silently removes
        it from allocation. A grace period rather than an immediate check,
        because "active with no live work" is also the ordinary state between a
        failed attempt and the queue's backoff making it claimable again.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                f"""
                select {", ".join("a." + c.strip() for c in ACTION_COLUMNS.split(","))}
                  from idea_actions a
                  join ideas i on i.idea_id = a.idea_id
                 where i.project_id = %(project_id)s
                   and a.status = 'ACTIVE'
                   and a.updated_at < now() - make_interval(secs => %(grace)s)
                   and not exists (
                       select 1 from work_items w
                        where w.work_id = a.work_id
                          and w.status in ('PENDING','LEASED','WAITING')
                   )
                 order by a.updated_at
                 limit %(limit)s
                """,
                {
                    "project_id": project_id,
                    "grace": float(older_than_seconds),
                    "limit": limit,
                },
            ).fetchall()
        return tuple(IdeaAction.model_validate(row) for row in rows)

    def spend_for_idea(self, idea_id: str) -> Decimal:
        with self._db.tx() as conn:
            row = conn.execute(
                "select coalesce(sum(cost_usd), 0) as total from idea_actions "
                "where idea_id = %s",
                (idea_id,),
            ).fetchone()
        return Decimal(str(row["total"]))

    def spend_for_lineage(self, lineage_root: str) -> Decimal:
        with self._db.tx() as conn:
            row = conn.execute(
                """
                select coalesce(sum(a.cost_usd), 0) as total
                  from idea_actions a
                  join ideas i on i.idea_id = a.idea_id
                 where i.lineage_root = %s
                """,
                (lineage_root,),
            ).fetchone()
        return Decimal(str(row["total"]))

    # ----------------------------------------------------- portfolio state --
    def upsert_state(
        self, *, project_id: str, bounds: Mapping[str, Any] | None = None
    ) -> PortfolioState:
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into portfolio_state (project_id, bounds)
                values (%(project_id)s, coalesce(%(bounds)s, %(empty)s))
                on conflict (project_id) do update
                    set bounds = coalesce(%(bounds)s, portfolio_state.bounds),
                        updated_at = now()
                returning {STATE_COLUMNS}
                """,
                {
                    "project_id": project_id,
                    "bounds": jsonb(dict(bounds)) if bounds is not None else None,
                    # An f-string cannot carry a literal `'{}'::jsonb`, and the
                    # SQL needs a default because `bounds` is NOT NULL and a
                    # caller that supplies none means "the configured ones".
                    "empty": jsonb({}),
                },
            ).fetchone()
        return PortfolioState.model_validate(row)

    def get_state(self, project_id: str) -> PortfolioState | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {STATE_COLUMNS} from portfolio_state where project_id = %s",
                (project_id,),
            ).fetchone()
        return PortfolioState.model_validate(row) if row else None

    def set_portfolio_status(
        self,
        *,
        project_id: str,
        status: PortfolioStatus,
        detail: str | None = None,
        paused_by: str | None = None,
    ) -> PortfolioState:
        running = status is PortfolioStatus.RUNNING
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                update portfolio_state
                   set status = %(status)s,
                       detail = %(detail)s,
                       paused_at = case when %(running)s then null else now() end,
                       paused_by = case when %(running)s then null else %(by)s end,
                       updated_at = now()
                 where project_id = %(project_id)s
                returning {STATE_COLUMNS}
                """,
                {
                    "project_id": project_id,
                    "status": str(status),
                    "detail": detail,
                    "running": running,
                    "by": paused_by,
                },
            ).fetchone()
        if row is None:
            raise PortfolioStateError(f"{project_id} has no portfolio state")
        return PortfolioState.model_validate(row)

    def record_bank_write(self, *, project_id: str, commit: str, digest: str) -> None:
        """Record the commit and the snapshot the Curator just wrote.

        Read back before the next curation. A tip that is not this commit was
        written by something else, and the Curator refuses rather than
        committing on top of it -- which is what covers the blind spot the
        reserved ref namespace creates in the coding pipeline's escape check.
        """

        with self._db.tx() as conn:
            conn.execute(
                "update portfolio_state set bank_commit = %s, bank_digest = %s, "
                "bank_written_at = now(), updated_at = now() where project_id = %s",
                (commit, digest, project_id),
            )

    def touch_tick(self, project_id: str, *, charter_digest: str | None = None) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "update portfolio_state set last_tick_at = now(), "
                "charter_digest = coalesce(%s, charter_digest), updated_at = now() "
                "where project_id = %s",
                (charter_digest, project_id),
            )

    def mark_digest_scheduled(self, project_id: str) -> None:
        with self._db.tx() as conn:
            conn.execute(
                "update portfolio_state set last_digest_at = now(), updated_at = now() "
                "where project_id = %s",
                (project_id,),
            )

    # --------------------------------------------------------------- seeds --
    def add_seed(self, *, project_id: str, text: str, note: str = "") -> PortfolioSeed:
        seed_id = new_seed_id()
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into portfolio_seeds (seed_id, project_id, text, note)
                values (%s, %s, %s, %s)
                returning {SEED_COLUMNS}
                """,
                (seed_id, project_id, text, note),
            ).fetchone()
        return PortfolioSeed.model_validate(row)

    def pending_seeds(
        self, *, project_id: str, limit: int = 20
    ) -> tuple[PortfolioSeed, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {SEED_COLUMNS} from portfolio_seeds "
                "where project_id = %s and consumed_at is null "
                "order by created_at limit %s",
                (project_id, limit),
            ).fetchall()
        return tuple(PortfolioSeed.model_validate(row) for row in rows)

    def consume_seed(self, *, seed_id: str, consumed_by: str) -> bool:
        with self._db.tx() as conn:
            row = conn.execute(
                "update portfolio_seeds set consumed_at = now(), consumed_by = %s "
                "where seed_id = %s and consumed_at is null returning seed_id",
                (consumed_by, seed_id),
            ).fetchone()
        return row is not None

    def list_seeds(
        self, *, project_id: str, limit: int = 100
    ) -> tuple[PortfolioSeed, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {SEED_COLUMNS} from portfolio_seeds where project_id = %s "
                "order by created_at desc limit %s",
                (project_id, limit),
            ).fetchall()
        return tuple(PortfolioSeed.model_validate(row) for row in rows)

    # ------------------------------------------------------------- digests --
    def record_digest(
        self,
        *,
        project_id: str,
        period_start: datetime,
        period_end: datetime,
        payload: Mapping[str, Any],
        artifact_id: str | None = None,
    ) -> PortfolioDigestRecord:
        digest_id = new_portfolio_digest_id()
        with self._db.tx() as conn:
            row = conn.execute(
                f"""
                insert into portfolio_digests
                    (digest_id, project_id, period_start, period_end, payload,
                     artifact_id)
                values (%s, %s, %s, %s, %s, %s)
                returning {DIGEST_COLUMNS}
                """,
                (
                    digest_id,
                    project_id,
                    period_start,
                    period_end,
                    jsonb(dict(payload)),
                    artifact_id,
                ),
            ).fetchone()
        return PortfolioDigestRecord.model_validate(row)

    def latest_digest(self, project_id: str) -> PortfolioDigestRecord | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {DIGEST_COLUMNS} from portfolio_digests where project_id = %s "
                "order by created_at desc, digest_id desc limit 1",
                (project_id,),
            ).fetchone()
        return PortfolioDigestRecord.model_validate(row) if row else None

    def get_digest(self, digest_id: str) -> PortfolioDigestRecord | None:
        with self._db.tx() as conn:
            row = conn.execute(
                f"select {DIGEST_COLUMNS} from portfolio_digests where digest_id = %s",
                (digest_id,),
            ).fetchone()
        return PortfolioDigestRecord.model_validate(row) if row else None

    def list_digests(
        self, *, project_id: str, limit: int = 20
    ) -> tuple[PortfolioDigestRecord, ...]:
        with self._db.tx() as conn:
            rows = conn.execute(
                f"select {DIGEST_COLUMNS} from portfolio_digests where project_id = %s "
                "order by created_at desc, digest_id desc limit %s",
                (project_id, limit),
            ).fetchall()
        return tuple(PortfolioDigestRecord.model_validate(row) for row in rows)

    # ------------------------------------------------------------ counting --
    def counts_by_status(self, project_id: str) -> dict[IdeaStatus, int]:
        with self._db.tx() as conn:
            rows = conn.execute(
                "select status, count(*) as n from ideas where project_id = %s "
                "group by status",
                (project_id,),
            ).fetchall()
        return {IdeaStatus(str(row["status"])): int(row["n"]) for row in rows}

    def blocked_counts(self, project_id: str) -> dict[str, int]:
        """Live ideas that are blocked, by operational state.

        ``counts_by_status`` groups by the *scientific* status, which is the
        right thing for it to do and means a blocked idea is reported as
        PROMISING with nothing saying it cannot move. That was tolerable while
        nothing in this layer produced ``BLOCKED_EXTERNAL``; the stage-failure
        ceiling does, so a dead end would otherwise be counted as a healthy
        idea.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                "select operational_state, count(*) as n from ideas "
                "where project_id = %s "
                "  and operational_state <> 'IDLE' and operational_state <> 'ACTIVE' "
                "  and status not in ('REJECTED', 'SUPERSEDED', 'HUMAN_READY') "
                "group by operational_state",
                (project_id,),
            ).fetchall()
        return {str(row["operational_state"]): int(row["n"]) for row in rows}

    def forgive_stage_failures(self, *, project_id: str) -> None:
        """Mark this moment as the one a person said "look again".

        Not a reset. :meth:`failed_stage_counts` keeps returning the all-time
        count, because ``work_items.dedup_key`` is built from it against a
        permanently unique index -- resetting it would re-use a spent key and
        ``enqueue``'s ``on conflict do nothing`` would refuse the retry
        silently, which is the wedge the count exists to prevent. What moves
        is the point the *ceiling* counts from.
        """

        with self._db.tx() as conn:
            conn.execute(
                "update portfolio_state set failures_forgiven_at = now(), "
                "updated_at = now() where project_id = %s",
                (project_id,),
            )

    def failed_stage_counts(self, project_id: str) -> dict[tuple[str, str, str], int]:
        """How many times each ``(idea, stage, version)`` advance failed terminally.

        Two things read this, and they are the two halves of one fix.

        The **dedup key** includes the count, so a failed stage can be bought
        again. ``work_items.dedup_key`` is a permanent unique index and
        ``enqueue`` is ``on conflict do nothing``, so a key computed only from
        the idea and the stage is spent the first time that pair fails: the
        allocator goes on choosing it and ``enqueue`` goes on silently
        refusing. The first dogfood ran an hour of ticks each deciding the
        same eight things and enqueueing none of them, reporting RUNNING
        throughout -- and after the defect that caused the failures was fixed,
        the portfolio still could not recover, because the keys were gone.

        The **allocator** reads it as a ceiling, because a count in a dedup key
        with nothing bounding it is an infinite retry wearing a fresh name.

        **Counted over work items, not over ``idea_actions``.** The first
        version of this counted failed action rows, and that has a gap the
        dogfood happened not to land in: ``advance_idea`` reads the idea, the
        version and the snapshot, selects the stage and opens a run *before*
        it opens the action row, so a terminal failure in that window leaves
        no action row at all -- the count would not move, the key would stay
        spent, and the wedge would be back in exactly the shape this exists to
        prevent. A work item always exists by the time it can fail.

        The stage and version read here are the *allocator's*, from the
        payload, which is what the dedup key is built from. ``advance_idea``
        may legitimately select a different stage; agreeing with the key is
        what matters.

        Scoped by version because the key is: a stage that failed twice on
        version 1 starts again with a clean ceiling on version 2, which is
        the content it actually has to run against.

        A count rather than a timestamp so two concurrent ticks compute the
        same key. That is the property the key exists for, and it is why this
        is not "append the current time".
        """

        return {
            key: total
            for key, (total, _recent) in self.stage_failures(project_id).items()
        }

    def stage_failures(
        self, project_id: str
    ) -> dict[tuple[str, str, str], tuple[int, int, int]]:
        """``(all time, recent, recent refusals)`` per stage key.

        Two numbers because two things read them and they want different
        answers.

        The **dedup key** wants all time. It is built from the count against
        a permanently unique index, so the count must only ever go up: a key
        that repeats is one ``enqueue`` refuses silently.

        The **ceiling** wants recent. A stage that failed three times because
        a capability was missing must be retryable once the capability
        arrives, and `researchctl portfolio resume` is the only signal in
        this system that says it has. Before this, `resume` returned the
        ideas to IDLE and the next tick blocked them again on the same
        historical count -- measured on 2026-09-22, on the three empirical
        ideas whose evidence stage had failed while the experiment route did
        not exist.

        The third is **refusals**, and it wants one rather than three.
        `REFUSAL_CLASSES` are the answers the failure taxonomy already calls
        terminal: the system worked and said no. "No declared command can
        test this idea" is the same answer next time and the time after, and
        each retry is a paid frontier call -- six identical refusals were
        bought across two sessions before anyone counted. Retrying a
        *transient* is the point of the ceiling; retrying a policy answer is
        buying the same sentence three times.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                "select w.payload->>'idea_id' as idea_id, "
                "       w.payload->>'stage' as stage, "
                "       coalesce(w.payload->>'idea_version', '') as idea_version, "
                "       count(*) as n, "
                "       count(*) filter ("
                "           where s.failures_forgiven_at is null "
                "              or w.updated_at > s.failures_forgiven_at) as recent, "
                "       count(*) filter ("
                "           where (s.failures_forgiven_at is null "
                "                  or w.updated_at > s.failures_forgiven_at) "
                "             and w.failure_class = any(%(refusals)s)) as refusals "
                "  from work_items w "
                "  left join portfolio_state s on s.project_id = w.project_id "
                " where w.project_id = %(project_id)s "
                "   and w.kind = 'portfolio_advance_idea' "
                "   and w.status = 'FAILED' "
                "   and w.payload->>'idea_id' is not null "
                "   and w.payload->>'stage' is not null "
                " group by 1, 2, 3",
                {
                    "project_id": project_id,
                    "refusals": [str(item) for item in sorted(REFUSAL_CLASSES)],
                },
            ).fetchall()
        return {
            (str(row["idea_id"]), str(row["stage"]), str(row["idea_version"])): (
                int(row["n"]),
                int(row["recent"]),
                int(row["refusals"]),
            )
            for row in rows
        }

    def failed_work(
        self, project_id: str, *, limit: int = 5
    ) -> tuple[tuple[str, str, str], int]:
        """Portfolio work that failed for this project: samples, and the count.

        `portfolio status` had no notion of this, and the first dogfood is why
        it does now. A routing defect failed every ``portfolio_advance_idea``
        item the moment it reached a model, and the command a researcher of
        this layer would actually type answered

            portfolio  cg-sparse-regression  RUNNING
            tracks     0 in flight
              candidate      9

        -- nine ideas, no tracks, nothing wrong. The failures were visible in
        `researchctl runtime status`, which is the operational view of a
        different layer; this is the one that is supposed to say what the
        portfolio is doing.

        The same reasoning as the "not scheduled" line above it: a portfolio
        that cannot advance an idea is not RUNNING in any sense a researcher
        means, and a silence there reads as health.

        Returns ``(samples, total, stuck)``. ``stuck`` is whether the newest
        failure is newer than the newest success, and it exists because the
        first version of this line was itself misleading within the hour: the
        soak's first project carried eight failures from a defect fixed long
        before, kept advancing ideas past them, and was told by this command
        that "a portfolio that cannot advance an idea is not making progress".
        It was making progress. A count with no recency is not a diagnosis.

        Scoped to this project and to this layer's work kinds, so a failure
        belonging to an R5 objective is not reported here as a portfolio
        problem.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select kind, coalesce(failure_class, 'unknown') as failure_class,
                       coalesce(last_error, '') as last_error
                from work_items
                where project_id = %s and status = 'FAILED'
                  and kind like 'portfolio\\_%%' escape '\\'
                order by updated_at desc
                limit %s
                """,
                (project_id, limit),
            ).fetchall()
            totals = conn.execute(
                """
                select count(*) filter (where status = 'FAILED') as failed,
                       max(updated_at) filter (where status = 'FAILED') as last_fail,
                       max(updated_at) filter (where status = 'SUCCEEDED') as last_ok
                from work_items
                where project_id = %s
                  and kind like 'portfolio\\_%%' escape '\\'
                """,
                (project_id,),
            ).fetchone()
        samples = tuple(
            (str(row["kind"]), str(row["failure_class"]), str(row["last_error"]))
            for row in rows
        )
        last_fail = totals["last_fail"]
        last_ok = totals["last_ok"]
        stuck = last_fail is not None and (last_ok is None or last_ok < last_fail)
        return samples, int(totals["failed"]), stuck

    def active_count(self, project_id: str) -> int:
        """How many idea tracks are in flight.

        The capacity number, and the one line that makes a ``HUMAN_READY`` idea
        not stop the portfolio: its track has ended, so its operational state
        is not ACTIVE, so it is not counted here and its slot is free.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                "select count(*) as n from ideas "
                "where project_id = %s and operational_state = 'ACTIVE'",
                (project_id,),
            ).fetchone()
        return int(row["n"])

    def lineage_active_counts(self, project_id: str) -> dict[str, int]:
        """Ideas still in progress, per lineage: what the lineage ceiling bounds.

        A ``VALIDATED`` idea counts only while a stage is running on it. Idle,
        it is closed for synthesis and waits on nothing the lineage does, and
        counting it held the lineage's slots forever: every follow-up raised
        from a validated idea's own replication met a full lineage.
        """

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select lineage_root, count(*) as n from ideas
                 where project_id = %s
                   and (status in ('CANDIDATE','PROMISING','INVESTIGATING','REVIEW')
                        or (status = 'VALIDATED' and operational_state = 'ACTIVE'))
                 group by lineage_root
                """,
                (project_id,),
            ).fetchall()
        return {str(row["lineage_root"]): int(row["n"]) for row in rows}

    def uncurated_count(self, project_id: str) -> int:
        """Ideas whose current state the Curator has not written to Git yet.

        The exposure to losing the operational database, as a number. See
        docs/adr/0002: everything curated survives, and this is what would not.
        """

        with self._db.tx() as conn:
            row = conn.execute(
                """
                select count(*) as n from ideas i
                 where i.project_id = %s
                   and (i.curated_at is null or i.curated_at < i.updated_at)
                """,
                (project_id,),
            ).fetchone()
        return int(row["n"])

    def mark_curated(self, *, idea_ids: Iterable[str], snapshot_digest: str) -> int:
        ids = list(idea_ids)
        if not ids:
            return 0
        with self._db.tx() as conn:
            rows = conn.execute(
                "update ideas set curated_digest = %s, curated_at = now() "
                "where idea_id = any(%s) returning idea_id",
                (snapshot_digest, ids),
            ).fetchall()
        return len(rows)

    def adjudication_counts(self, project_id: str) -> dict[str, int]:
        """How the live ideas divide across adjudication types, for diversity."""

        with self._db.tx() as conn:
            rows = conn.execute(
                """
                select unnest(v.adjudication_types) as kind, count(*) as n
                  from idea_versions v
                  join ideas i on i.idea_id = v.idea_id
                               and i.current_version = v.version
                 where i.project_id = %s
                   and i.status not in ('REJECTED','SUPERSEDED')
                 group by 1
                """,
                (project_id,),
            ).fetchall()
        return {str(row["kind"]): int(row["n"]) for row in rows}


__all__ = [
    "ActiveTrackExistsError",
    "AdjudicationType",
    "DuplicateBasisError",
    "PortfolioStateError",
    "PortfolioStore",
]
