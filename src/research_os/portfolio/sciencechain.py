"""The frozen science chain, and the outcome ordinary code computes at its end.

``docs/SCIENCE_EXECUTION.md`` is the specification. The chain, in the only
order the database accepts it (`sql/0047`)::

    scientific contract   WHAT is tested: the hypothesis, the estimand, the
                          population, the success and failure criteria and the
                          inconclusive region -- frozen first
    experimental design   HOW it is tested: data, comparison groups,
                          repetitions, seed policy, exclusion rules, required
                          observables, the primary analysis -- names the
                          contract
    capability binding    which declared capability produces those
                          observables, resolved by code (``research_os.
                          capability``): EXECUTABLE or CAPABILITY_LIMITED
    execution plan        HOW the design maps onto that capability: the
                          capability and its digest, the code commit, the exact
                          immutable inputs, the command, the expected outputs,
                          the specification digest -- names the design
    execution             the runner's receipt names the plan
    outcome               SUPPORTED | REFUTED | INCONCLUSIVE | EXECUTION_FAILED
                          | CAPABILITY_LIMITED | INVALID_EVIDENCE | BUDGET_LIMITED,
                          computed here and nowhere else, bound to all of it

Each object is content-addressed: its digest is ``<kind>:<sha256 of its
canonical bytes>`` and the artifact store holds exactly those bytes, so an
object and its record cannot disagree without one failing to re-hash. A change
to anything frozen upstream is a new object with a new digest; downstream
evidence names the old digest and does not transfer to the new chain.

The three objects are *derived* from what the empirical route already
freezes -- the analysis the analysis designer fixed first, the design the
experiment designer fixed second, the specification the preregistration
holds -- and each is re-derived and compared when it is verified, so the
chain cannot drift from the contract row it describes. What they add is the
separation the route lacked: an explicit capability binding between design
and plan, and an outcome vocabulary that says why there is no finding when
there is none.

**No model computes an outcome.** A reading is the frozen decision rule
applied by :mod:`research_os.portfolio.analysis` to a result that first
passed its capability's declared schema; everything else is a mapping from
the failure taxonomy. A model may interpret an outcome afterwards; it cannot
make one.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from research_os.capability import (
    Binding,
    Capability,
    ObservableNeed,
    capability_digest,
)
from research_os.errors import ResearchOSError
from research_os.portfolio.contracts import AnalysisSpec, DesignSpecification
from research_os.portfolio.models import (
    READ_OUTCOMES,
    EmpiricalConclusion,
    EvidenceKind,
    ExecutionReceipt,
    ExperimentRole,
    ExperimentState,
    IdeaEvidence,
    IdeaExperiment,
    IdeaVersion,
    PrimaryOutcome,
    ScienceChain,
    ScienceObject,
    ScienceObjectKind,
    ScienceOutcome,
)
from research_os.runtime.failures import FailureClass

CONTRACT_SCHEMA = "research-os-science-contract-v1"
DESIGN_SCHEMA = "research-os-experiment-design-v1"
PLAN_SCHEMA = "research-os-execution-plan-v1"
#: A campaign plan: one frozen design compiled to several execution units of
#: one capability (`research_os.portfolio.campaign`). Still a PLAN -- it
#: realises one design and binds one capability -- with its units frozen in it.
CAMPAIGN_SCHEMA = "research-os-execution-campaign-v1"
OUTCOME_SCHEMA = "research-os-science-outcome-v1"
COMPUTED_BY = "research_os.portfolio.sciencechain"

PREFIX: Mapping[ScienceObjectKind, str] = {
    ScienceObjectKind.CONTRACT: "rscontract-v1",
    ScienceObjectKind.DESIGN: "rsdesign-v1",
    ScienceObjectKind.PLAN: "rsplan-v1",
}
SCHEMA: Mapping[ScienceObjectKind, str] = {
    ScienceObjectKind.CONTRACT: CONTRACT_SCHEMA,
    ScienceObjectKind.DESIGN: DESIGN_SCHEMA,
    ScienceObjectKind.PLAN: PLAN_SCHEMA,
}
#: Every schema a frozen object of each kind may carry.
SCHEMAS: Mapping[ScienceObjectKind, frozenset[str]] = {
    ScienceObjectKind.CONTRACT: frozenset({CONTRACT_SCHEMA}),
    ScienceObjectKind.DESIGN: frozenset({DESIGN_SCHEMA}),
    ScienceObjectKind.PLAN: frozenset({PLAN_SCHEMA, CAMPAIGN_SCHEMA}),
}

#: The largest result artifact this layer will parse to validate it.
MAX_RESULT_BYTES = 8 * 1024 * 1024


class ChainError(ResearchOSError):
    """A frozen object, or a link between two, that does not verify."""

    failure_class = FailureClass.MISSING_SCIENTIFIC_AUTHORITY


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def object_digest(kind: ScienceObjectKind, payload: Mapping[str, Any]) -> str:
    return f"{PREFIX[kind]}:{hashlib.sha256(canonical(payload)).hexdigest()}"


# ---------------------------------------------------------- requirements --
_NUMERIC_COMPARATORS = frozenset({"<", "<=", ">", ">="})


def requirements_from_analysis(spec: AnalysisSpec) -> tuple[ObservableNeed, ...]:
    """What a frozen analysis reads, as capability requirements. Ordinary code.

    Every field any part of the analysis reads -- an observable's required
    fields, its inclusion rules, a reduction's field, a regression's response
    and terms, a ``where`` -- and which of those it reads as numbers. The
    capability must declare each field, and each numeric one as a number.
    """

    if not spec.analysable:
        return ()
    needs: list[ObservableNeed] = []
    for observable in spec.observables:
        fields: list[str] = list(observable.fields)
        numeric: list[str] = []
        for condition in observable.include:
            fields.append(condition.field)
            if condition.comparator in _NUMERIC_COMPARATORS:
                numeric.append(condition.field)
        for reduction in spec.reductions:
            if reduction.observable != observable.name:
                continue
            fields.extend(reduction.fields_read())
            for condition in reduction.where:
                if condition.comparator in _NUMERIC_COMPARATORS:
                    numeric.append(condition.field)
            if reduction.op in {
                "mean",
                "median",
                "std",
                "min",
                "max",
                "sum",
                "quantile",
            }:
                numeric.append(reduction.field)
            elif reduction.op == "correlation":
                numeric.extend([reduction.field, reduction.other_field])
            elif reduction.op == "ols_coefficient":
                numeric.append(reduction.response)
                for term in reduction.terms:
                    numeric.extend(term.split(":"))
        needs.append(
            ObservableNeed(
                name=observable.name,
                source=observable.source,
                kind=observable.kind,
                path=observable.path,
                fields=tuple(dict.fromkeys(item for item in fields if item)),
                numeric=tuple(dict.fromkeys(item for item in numeric if item)),
            )
        )
    return tuple(needs)


def _need_record(need: ObservableNeed) -> dict[str, Any]:
    return {
        "name": need.name,
        "source": need.source,
        "kind": need.kind,
        "path": need.path,
        "fields": list(need.fields),
        "numeric_fields": list(need.numeric),
    }


# -------------------------------------------------------------- builders --
def _criterion(spec: AnalysisSpec, predicate: Any) -> dict[str, Any] | None:
    if predicate is None:
        return None
    return {
        "statistic": spec.primary_statistic,
        "comparator": predicate.comparator,
        "threshold": predicate.threshold,
        "applies_to": (
            f"every value of the {spec.uncertainty.level:g} "
            f"{spec.uncertainty.method} interval"
            if spec.uncertainty is not None
            else "the point estimate"
        ),
    }


def contract_payload(
    *, project_id: str, version: IdeaVersion, analysis: AnalysisSpec
) -> dict[str, Any]:
    """The scientific contract: WHAT is tested, and what would settle it.

    Derived from the idea version and its frozen analysis, and from nothing a
    design or a capability says: it is the same object for a primary and for
    every replication of it, which is what makes a replication a test of the
    same proposition.
    """

    from research_os.portfolio import scicontract

    return {
        "schema": CONTRACT_SCHEMA,
        "project_id": project_id,
        "idea_id": version.idea_id,
        "idea_version": version.version,
        "hypothesis": scicontract.hypothesis_record(version),
        "question": version.research_question,
        "analysable": analysis.analysable,
        "unanalysable_reason": analysis.unanalysable_reason,
        "estimand": analysis.estimand,
        "population": analysis.population,
        "target_claim": analysis.target_claim,
        "primary_statistic": analysis.primary_statistic,
        "success_criterion": _criterion(analysis, analysis.success),
        "failure_criterion": _criterion(analysis, analysis.failure),
        "inconclusive_region": {
            "state": str(PrimaryOutcome.INCONCLUSIVE),
            "when": [
                "neither criterion holds",
                "both criteria hold",
                (
                    "the data do not meet the frozen support requirements, or the "
                    "primary statistic is undefined on them"
                ),
            ],
        },
        "uncertainty": (
            analysis.uncertainty.model_dump(mode="json")
            if analysis.uncertainty is not None
            else None
        ),
        "stopping_rule": analysis.stopping_rule,
        "on_missing": analysis.on_missing,
        "analysis_digest": scicontract.analysis_digest(analysis),
    }


def design_payload(
    *,
    contract_digest: str,
    project_id: str,
    version: IdeaVersion,
    role: ExperimentRole,
    analysis: AnalysisSpec,
    design: DesignSpecification,
    composed: Mapping[str, str],
    seeds: Sequence[int],
    parent_design_digest: str | None = None,
    campaign: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """The experimental design: HOW the contract is tested. Names the contract.

    ``campaign`` is a campaign design's units -- each unit's parameters (a
    composed document by its digest) and seeds -- frozen here, in the design,
    before any plan binds them to a capability.
    """

    from research_os.portfolio import scicontract

    parameters: dict[str, Any] = {}
    for name, value in sorted(dict(design.command_parameters).items()):
        parameters[name] = (
            {"composed_sha256": composed[name]} if name in composed else value
        )
    variables = [item.model_dump(mode="json") for item in design.variables]
    exclusion: list[dict[str, Any]] = []
    for observable in analysis.observables:
        exclusion.append(
            {
                "observable": observable.name,
                "include_only_when": [item.rendered() for item in observable.include],
                "incomplete_records": observable.incomplete_records,
            }
        )
    for reduction in analysis.reductions:
        if reduction.where:
            exclusion.append(
                {
                    "quantity": reduction.name,
                    "include_only_when": [item.rendered() for item in reduction.where],
                }
            )
    return {
        "schema": DESIGN_SCHEMA,
        "project_id": project_id,
        "idea_id": version.idea_id,
        "idea_version": version.version,
        "role": str(role),
        "contract_digest": contract_digest,
        "parent_design_digest": parent_design_digest,
        "primary_analysis": {
            "analysis_digest": scicontract.analysis_digest(analysis),
            "observables": [
                item.model_dump(mode="json") for item in analysis.observables
            ],
            "reductions": [
                item.model_dump(mode="json") for item in analysis.reductions
            ],
            "primary_statistic": analysis.primary_statistic,
            "support": [item.model_dump(mode="json") for item in analysis.support],
        },
        "required_observables": [
            _need_record(item) for item in requirements_from_analysis(analysis)
        ],
        "exclusion_rules": exclusion,
        "data": {
            "dataset_identity": design.dataset_identity,
            "sampling": design.sampling,
        },
        "comparison_groups": [
            item for item in variables if item["role"] == "manipulated"
        ],
        "controls": [
            item for item in variables if item["role"] in {"controlled", "blocking"}
        ],
        "measured": [item for item in variables if item["role"] == "measured"],
        "repetitions": design.repetitions,
        "seed_policy": {
            "seeds": list(seeds),
            "delivered_as": "RESEARCH_OS_SEED_<n>",
        },
        "perturbation": {
            "variation_kind": design.variation_kind,
            "variation_detail": design.variation_detail,
        },
        "falsification_criterion": design.falsification_criterion,
        "measurement": {"command": design.command, "parameters": parameters},
        **(
            {
                "campaign": {
                    "units": [dict(item) for item in campaign],
                    "rationale": (
                        design.campaign.rationale if design.campaign is not None else ""
                    ),
                    "stopping_rule": analysis.stopping_rule,
                }
            }
            if campaign
            else {}
        ),
    }


def plan_payload(
    *,
    design_digest: str,
    contract_digest: str,
    project_id: str,
    version: IdeaVersion,
    role: ExperimentRole,
    binding: Binding,
    command_identity: Mapping[str, Any],
    spec: Any,
    spec_digest: str,
    variation_digest: str,
    parameters: Mapping[str, Any],
    input_artifacts: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """The execution plan: the design mapped onto one declared capability."""

    from research_os.portfolio import provenance

    capability = binding.capability
    return {
        "schema": PLAN_SCHEMA,
        "project_id": project_id,
        "idea_id": version.idea_id,
        "idea_version": version.version,
        "role": str(role),
        "design_digest": design_digest,
        "contract_digest": contract_digest,
        "capability": {
            **binding.record(),
            "declaration": capability.model_dump(mode="json", by_alias=True),
            "result_schema": capability.result.json_schema,
        },
        "code": {"commit": binding.commit},
        "command": {
            "name": capability.command,
            "declaration": dict(command_identity),
            "command_digest": provenance.digest(dict(command_identity)),
            "argv": list(spec.argv),
            "parameters": dict(parameters),
        },
        "inputs": {
            "composed": [list(item) for item in spec.inputs],
            "artifacts": [list(item) for item in input_artifacts],
        },
        "expected_outputs": sorted(spec.outputs),
        "result_artifact": binding.result_path,
        "configuration": {
            "env": dict(sorted(dict(spec.env).items())),
            "environment": dict(sorted(dict(spec.environment).items())),
            "seeds": list(spec.seeds),
            "cwd": spec.cwd,
        },
        "implementation": {
            "timeout_seconds": int(spec.timeout_seconds),
            "resources": dict(sorted(dict(spec.resources).items())),
        },
        "spec_digest": spec_digest,
        "variation_digest": variation_digest,
    }


def campaign_payload(
    *,
    design_digest: str,
    contract_digest: str,
    project_id: str,
    version: IdeaVersion,
    role: ExperimentRole,
    binding: Binding,
    command_identity: Mapping[str, Any],
    units: Sequence[Mapping[str, Any]],
    spec_digest: str,
    variation_digest: str,
    aggregation: Sequence[Mapping[str, Any]],
    resources: Mapping[str, Any],
    input_artifacts: Sequence[tuple[str, str]],
) -> dict[str, Any]:
    """The frozen execution campaign: the design mapped onto several executions.

    Everything a single plan binds, once -- the contract and design, the
    capability and its declaration, the code commit, the host command, the
    immutable input artifacts -- and then each unit's own frozen execution:
    argv, parameters, composed inputs, expected outputs, result artifact,
    configuration (env, environment, seeds, cwd), implementation bounds and
    its specification and variation digests. Plus the declared aggregation
    rule for every observable the analysis reads, the rule for a missing unit
    (the campaign is not read), and the bounded total resources. Changing any
    of it is a new plan with a new digest, and evidence bound to the old one
    does not transfer.
    """

    from research_os.portfolio import campaign as campaigns
    from research_os.portfolio import provenance

    capability = binding.capability
    return {
        "schema": CAMPAIGN_SCHEMA,
        "project_id": project_id,
        "idea_id": version.idea_id,
        "idea_version": version.version,
        "role": str(role),
        "design_digest": design_digest,
        "contract_digest": contract_digest,
        "capability": {
            **binding.record(),
            "declaration": capability.model_dump(mode="json", by_alias=True),
            "result_schema": capability.result.json_schema,
        },
        "code": {"commit": binding.commit},
        "command": {
            "name": capability.command,
            "declaration": dict(command_identity),
            "command_digest": provenance.digest(dict(command_identity)),
        },
        "inputs": {"artifacts": [list(item) for item in input_artifacts]},
        "units": [dict(item) for item in units],
        "aggregation": {
            "observables": [dict(item) for item in aggregation],
            "missing_units": campaigns.MISSING_UNITS,
            "stopping_rule": campaigns.CAMPAIGN_STOPPING_RULE,
        },
        "resources": dict(resources),
        "spec_digest": spec_digest,
        "variation_digest": variation_digest,
    }


def unit_record(
    *,
    index: int,
    label: str,
    spec: Any,
    spec_digest: str,
    variation_digest: str,
    parameters: Mapping[str, Any],
    result_artifact: str | None,
    input_artifacts: Sequence[tuple[str, str]],
    varies: Sequence[str],
) -> dict[str, Any]:
    """One campaign unit's frozen execution, as the campaign plan holds it."""

    return {
        "index": index,
        "label": label,
        "argv": list(spec.argv),
        "parameters": dict(parameters),
        "inputs": {
            "composed": [list(item) for item in spec.inputs],
            "artifacts": [list(item) for item in input_artifacts],
        },
        "expected_outputs": sorted(spec.outputs),
        "result_artifact": result_artifact,
        "configuration": {
            "env": dict(sorted(dict(spec.env).items())),
            "environment": dict(sorted(dict(spec.environment).items())),
            "seeds": list(spec.seeds),
            "cwd": spec.cwd,
        },
        "implementation": {
            "timeout_seconds": int(spec.timeout_seconds),
            "resources": dict(sorted(dict(spec.resources).items())),
        },
        "spec_digest": spec_digest,
        "variation_digest": variation_digest,
        "varies_from_first_unit": list(varies),
    }


# --------------------------------------------------------------- freezing --
def freeze(
    context: Any,
    kind: ScienceObjectKind,
    payload: Mapping[str, Any],
    *,
    parent: str | None = None,
    capability: tuple[str, str] | None = None,
    spec_digest: str | None = None,
    units: Sequence[tuple[int, str, str]] = (),
) -> ScienceObject:
    """Store one object's canonical bytes and freeze its row. Idempotent.

    The bytes go into the content-addressed store first, so a crash between
    the two leaves an unreferenced blob rather than a row naming bytes nobody
    kept. The row is refused by the database unless its parent exists, is of
    the kind it realises, belongs to the same idea version and was frozen no
    later than it.
    """

    data = canonical(payload)
    ref = context.artifacts.put_bytes(
        data,
        media_type="application/json",
        role=f"science_{str(kind).lower()}",
        producer=COMPUTED_BY,
    )
    context.artifacts.link(
        ref,
        role=f"science_{str(kind).lower()}",
        run_id=getattr(context, "run_id", None),
    )
    digest = f"{PREFIX[kind]}:{ref.artifact_id}"
    if digest != object_digest(kind, payload):  # pragma: no cover - one hash
        raise ChainError(f"the stored {kind} does not hash to its content")
    return context.portfolio.freeze_science_object(
        object_digest=digest,
        kind=kind,
        project_id=str(payload["project_id"]),
        idea_id=str(payload["idea_id"]),
        idea_version=int(payload["idea_version"]),
        artifact_id=str(ref.artifact_id),
        parent_digest=parent,
        capability_ref=capability[0] if capability is not None else None,
        capability_digest=capability[1] if capability is not None else None,
        spec_digest=spec_digest,
        units=units,
    )


# ------------------------------------------------------------- verifying --
@dataclass(frozen=True, slots=True)
class Frozen:
    row: ScienceObject
    payload: Mapping[str, Any]

    @property
    def digest(self) -> str:
        return self.row.object_digest


@dataclass(frozen=True, slots=True)
class VerifiedChain:
    """A plan, its design and its contract, each re-hashed and bound to the next."""

    contract: Frozen
    design: Frozen
    plan: Frozen
    capability: Capability

    @property
    def result_path(self) -> str | None:
        found = (self.plan.payload.get("capability") or {}).get("result_path")
        return str(found) if found else None

    @property
    def is_campaign(self) -> bool:
        return self.plan.payload.get("schema") == CAMPAIGN_SCHEMA

    @property
    def units(self) -> tuple[Mapping[str, Any], ...]:
        """A campaign's frozen units, in order; none for a single plan."""

        return tuple(self.plan.payload.get("units") or ()) if self.is_campaign else ()

    def unit(self, index: int) -> Mapping[str, Any]:
        for item in self.units:
            if int(item.get("index", -1)) == index:
                return item
        raise ChainError(f"the campaign {self.plan.digest[:24]} froze no unit {index}")

    def unit_result_path(self, index: int) -> str | None:
        found = self.unit(index).get("result_artifact") or self.result_path
        return str(found) if found else None

    @property
    def aggregation(self) -> tuple[Mapping[str, Any], ...]:
        block = dict(self.plan.payload.get("aggregation") or {})
        return tuple(block.get("observables") or ())

    @property
    def code_commit(self) -> str:
        return str((self.plan.payload.get("code") or {}).get("commit") or "")

    def digests(self) -> dict[str, Any]:
        return {
            "contract_digest": self.contract.digest,
            "design_digest": self.design.digest,
            "plan_digest": self.plan.digest,
            "capability_ref": self.capability.ref,
            "capability_digest": self.plan.row.capability_digest,
        }


def load(
    store: Any, artifacts: Any, digest: str | None, kind: ScienceObjectKind
) -> Frozen:
    """One frozen object, its bytes re-hashed against its address and row."""

    from research_os.portfolio import provenance

    row = store.get_science_object(digest)
    if row is None:
        raise ChainError(f"no frozen {str(kind).lower()} {digest or '(none named)'}")
    if row.kind is not kind:
        raise ChainError(f"{digest} is a {row.kind}, not a {kind}")
    if row.object_digest != f"{PREFIX[kind]}:{row.artifact_id}":
        raise ChainError(f"{digest} is not the digest of its stored bytes")
    try:
        payload = provenance.verified_json(artifacts, row.artifact_id)
    except provenance.ProvenanceError as exc:
        raise ChainError(
            f"the frozen {str(kind).lower()} {digest[:24]}: {exc}"
        ) from None
    if canonical(payload) != _stored_bytes(artifacts, row.artifact_id):
        raise ChainError(f"{digest[:24]} is not stored in canonical form")
    if (
        payload.get("schema") not in SCHEMAS[kind]
        or payload.get("idea_id") != row.idea_id
        or payload.get("idea_version") != row.idea_version
        or payload.get("project_id") != row.project_id
    ):
        raise ChainError(
            f"{digest[:24]} does not describe the idea version its row names"
        )
    return Frozen(row=row, payload=payload)


def _stored_bytes(artifacts: Any, artifact_id: str) -> bytes:
    return bytes(artifacts.get_bytes(artifact_id))


def verify_plan(
    store: Any,
    artifacts: Any,
    experiment: IdeaExperiment,
    *,
    version: IdeaVersion | None = None,
    analysis: AnalysisSpec | None = None,
) -> VerifiedChain:
    """Re-verify an experiment's whole frozen chain, or refuse with the break.

    Called before an execution runs, before its result is read and at
    readiness. With ``version`` and ``analysis`` -- the idea version and the
    contract row's verified analysis -- the contract object and the design's
    primary analysis are also *re-derived* and compared, so the chain cannot
    say something the contract row it describes does not.
    """

    if not experiment.plan_digest:
        raise ChainError(
            f"{experiment.experiment_id} realises no frozen execution plan: it "
            f"was designed without a capability manifest, so no declared "
            f"capability stands behind what it measures"
        )
    plan = load(store, artifacts, experiment.plan_digest, ScienceObjectKind.PLAN)
    design = load(store, artifacts, plan.row.parent_digest, ScienceObjectKind.DESIGN)
    contract = load(
        store, artifacts, design.row.parent_digest, ScienceObjectKind.CONTRACT
    )
    problems: list[str] = []
    if plan.payload.get("design_digest") != design.digest:
        problems.append("the plan names a different design than its row")
    if plan.payload.get("contract_digest") != contract.digest:
        problems.append("the plan names a different contract than its design")
    if design.payload.get("contract_digest") != contract.digest:
        problems.append("the design names a different contract than its row")
    if (
        plan.row.spec_digest != experiment.spec_digest
        or plan.payload.get("spec_digest") != experiment.spec_digest
    ):
        problems.append("the plan's specification is not the experiment's")
    for label, frozen in (("plan", plan), ("design", design)):
        if frozen.payload.get("role") != str(experiment.role):
            problems.append(f"the {label} is for another role")
    if (experiment.idea_id, experiment.idea_version) != (
        contract.row.idea_id,
        contract.row.idea_version,
    ):
        problems.append("the chain is of another idea version")
    block = dict(plan.payload.get("capability") or {})
    try:
        capability = Capability.model_validate(block.get("declaration") or {})
    except ValueError as exc:
        raise ChainError(
            f"the plan's capability declaration is not one: {exc}"
        ) from None
    digest = capability_digest(capability)
    if (
        digest != plan.row.capability_digest
        or digest != block.get("digest")
        or capability.ref != plan.row.capability_ref
    ):
        problems.append(
            "the plan's capability declaration does not hash to its binding"
        )
    if capability.command != experiment.command:
        problems.append("the bound capability is run by another command")
    if plan.payload.get("schema") == CAMPAIGN_SCHEMA:
        problems.extend(_campaign_problems(store, plan, experiment))
    elif store.campaign_units(plan.digest):
        problems.append("a single execution plan has campaign units recorded")
    if version is not None and analysis is not None:
        rebuilt = object_digest(
            ScienceObjectKind.CONTRACT,
            contract_payload(
                project_id=contract.row.project_id, version=version, analysis=analysis
            ),
        )
        if rebuilt != contract.digest:
            problems.append(
                "the frozen scientific contract is not the one its idea version "
                "and frozen analysis derive"
            )
        from research_os.portfolio import scicontract

        primary = dict(design.payload.get("primary_analysis") or {})
        if primary.get("analysis_digest") != scicontract.analysis_digest(analysis):
            problems.append("the design's primary analysis is not the frozen analysis")
    if problems:
        raise ChainError(
            f"the frozen chain of {experiment.experiment_id} does not hold: "
            + "; ".join(problems)
        )
    return VerifiedChain(
        contract=contract, design=design, plan=plan, capability=capability
    )


def _campaign_problems(
    store: Any, plan: Frozen, experiment: IdeaExperiment
) -> list[str]:
    """What does not hold about a campaign plan's units, against every record of them.

    The units are frozen twice -- in the plan's bytes and, row by row, in
    ``science_campaign_units`` -- and the campaign's specification digest is
    a function of theirs. All three must say the same thing.
    """

    from research_os.portfolio import campaign as campaigns

    problems: list[str] = []
    units = list(plan.payload.get("units") or ())
    if len(units) < 2:
        problems.append("the campaign plan freezes fewer than two units")
        return problems
    indexes = [item.get("index") for item in units]
    if indexes != list(range(len(units))):
        problems.append("the campaign's units are not numbered 0..n-1 in order")
    digests = [str(item.get("spec_digest") or "") for item in units]
    if len(set(digests)) != len(digests):
        problems.append("two campaign units share one specification")
    rebuilt = campaigns.campaign_spec_digest(digests)
    if rebuilt != plan.payload.get("spec_digest") or rebuilt != experiment.spec_digest:
        problems.append(
            "the campaign's specification digest is not the one its units derive"
        )
    variation = campaigns.campaign_variation_digest(
        [str(item.get("variation_digest") or "") for item in units]
    )
    if variation != plan.payload.get("variation_digest"):
        problems.append("the campaign's variation digest is not its units'")
    recorded = [
        (item.unit_index, item.spec_digest, item.variation_digest)
        for item in store.campaign_units(plan.digest)
    ]
    frozen = [
        (
            int(item.get("index", -1)),
            str(item.get("spec_digest") or ""),
            str(item.get("variation_digest") or ""),
        )
        for item in units
    ]
    if recorded != frozen:
        problems.append(
            "the campaign's units in the database are not the units its plan froze"
        )
    resources = dict(plan.payload.get("resources") or {})
    if int(resources.get("units") or 0) != len(units):
        problems.append("the campaign's resources are not bounded over its units")
    if not dict(plan.payload.get("aggregation") or {}).get("observables"):
        problems.append("the campaign freezes no aggregation rule")
    return problems


# ------------------------------------------------------ result validation --
@dataclass(frozen=True, slots=True)
class ResultCheck:
    """Whether an execution's result artifact is one its capability declares."""

    ok: bool
    reason: str
    detail: str
    path: str | None = None
    sha256: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "valid": self.ok,
            "reason": self.reason,
            "detail": self.detail,
            "path": self.path,
            "sha256": self.sha256,
        }


def _reject_constant(literal: str) -> Any:
    raise ValueError(f"{literal} is not a JSON number")


def _at(document: Any, path: str) -> tuple[bool, Any]:
    node = document
    for part in [item for item in path.split(".") if item]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        elif isinstance(node, list) and part.isdigit() and int(part) < len(node):
            node = node[int(part)]
        else:
            return False, None
    return True, node


def validate_result(
    chain: VerifiedChain,
    *,
    workspace: Path,
    recorded_outputs: Sequence[tuple[str, str, int]],
    result_path: str | None = None,
) -> ResultCheck:
    """Validate the result artifact against the capability's declaration.

    Before anything reads it: the artifact the plan names must be one the
    runner hashed at exit, the bytes read now must be those bytes, they must
    parse as strict JSON, satisfy the declared result schema, and hold every
    bound observable where the declaration says -- a number for a scalar, a
    list of records for records. Any failure is ``INVALID_EVIDENCE``: the
    execution happened and what it produced is not the declared result.
    """

    from research_os.automation.filescope import open_contained

    path = result_path or chain.result_path
    if not path:
        return ResultCheck(
            False, "result_unlocated", "the plan locates no result artifact"
        )
    recorded = {str(item[0]): (str(item[1]), int(item[2])) for item in recorded_outputs}
    if path not in recorded:
        return ResultCheck(
            False,
            "result_missing",
            f"the declared result artifact {path} was not produced (the runner "
            f"hashed no such output at exit)",
            path=path,
        )
    sha, size = recorded[path]
    if size > MAX_RESULT_BYTES:
        return ResultCheck(
            False, "result_too_large", f"{path} is {size} bytes", path=path, sha256=sha
        )
    handle = open_contained(workspace, path)
    if handle is None:
        return ResultCheck(
            False, "result_missing", f"{path} is not a file the run wrote", path=path
        )
    with handle:
        data = handle.read(MAX_RESULT_BYTES + 1)
    return validate_result_bytes(chain, path=path, data=data, recorded_sha=sha)


def validate_result_bytes(
    chain: VerifiedChain, *, path: str, data: bytes, recorded_sha: str
) -> ResultCheck:
    """The declaration's checks over a result's bytes, wherever they are read from.

    The workspace right after the run, or -- for a campaign unit read again
    when the campaign is combined and at readiness -- the content-addressed
    store. Either way the bytes must be the ones the runner hashed at exit.
    """

    from research_os.errors import ExperimentSpecError
    from research_os.experiment.generated import validate_document

    sha = recorded_sha
    if len(data) > MAX_RESULT_BYTES:
        return ResultCheck(
            False,
            "result_too_large",
            f"{path} is {len(data)} bytes",
            path=path,
            sha256=sha,
        )
    if hashlib.sha256(data).hexdigest() != sha:
        return ResultCheck(
            False,
            "result_changed",
            f"{path} is not the bytes the runner hashed at exit",
            path=path,
            sha256=sha,
        )
    try:
        document = json.loads(data.decode("utf-8"), parse_constant=_reject_constant)
    except (UnicodeDecodeError, ValueError) as exc:
        return ResultCheck(
            False,
            "result_malformed",
            f"{path} is not strict JSON: {exc}",
            path=path,
            sha256=sha,
        )
    schema = (chain.plan.payload.get("capability") or {}).get("result_schema") or {}
    try:
        validate_document(document, schema, where=path)
    except ExperimentSpecError as exc:
        return ResultCheck(
            False,
            "result_schema_violation",
            f"{path} does not satisfy the capability's declared result schema: {exc}",
            path=path,
            sha256=sha,
        )
    bound = dict((chain.plan.payload.get("capability") or {}).get("observables") or {})
    for name in sorted(set(bound.values())):
        declared = chain.capability.observable(name)
        if declared is None:  # pragma: no cover - the binding came from it
            return ResultCheck(
                False, "observable_undeclared", name, path=path, sha256=sha
            )
        present, value = _at(document, declared.path)
        if declared.kind == "scalar":
            ok = (
                present
                and isinstance(value, int | float)
                and not isinstance(value, bool)
                and math.isfinite(float(value))
            )
        else:
            ok = (
                present
                and isinstance(value, list)
                and all(isinstance(item, Mapping) for item in value)
            )
        if not ok:
            return ResultCheck(
                False,
                "observable_absent",
                f"{path} does not hold the declared {declared.kind} observable "
                f"{name!r} at {declared.path or '(the top level)'!r}",
                path=path,
                sha256=sha,
            )
    return ResultCheck(True, "valid", "the result satisfies its declaration", path, sha)


# ------------------------------------------------------------- outcomes --
#: How a reading's conclusion becomes an outcome, once its result validated.
READING_OUTCOME: Mapping[EmpiricalConclusion, tuple[PrimaryOutcome, str]] = {
    EmpiricalConclusion.SUPPORTS: (PrimaryOutcome.SUPPORTED, "success_criterion_held"),
    EmpiricalConclusion.CONTRADICTS: (PrimaryOutcome.REFUTED, "failure_criterion_held"),
    EmpiricalConclusion.INCONCLUSIVE: (
        PrimaryOutcome.INCONCLUSIVE,
        "inconclusive_region",
    ),
    EmpiricalConclusion.INSUFFICIENT: (
        PrimaryOutcome.INCONCLUSIVE,
        "support_unmet_or_statistic_undefined",
    ),
}

#: How an operational failure becomes an outcome. A class not here is not an
#: outcome of the science at all (a provider that did not answer a *design*
#: question, a lease lost) and records none.
FAILURE_OUTCOME: Mapping[FailureClass, PrimaryOutcome] = {
    FailureClass.EXECUTOR_FAILED: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.SCHEDULER_UNAVAILABLE: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.SLURM_TIMEOUT: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.SLURM_OUT_OF_MEMORY: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.SLURM_NODE_FAILURE: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.SLURM_PREEMPTED: PrimaryOutcome.EXECUTION_FAILED,
    FailureClass.CAPABILITY_DENIED: PrimaryOutcome.CAPABILITY_LIMITED,
    FailureClass.BUDGET_EXHAUSTED: PrimaryOutcome.BUDGET_LIMITED,
    FailureClass.ARTIFACT_MISSING: PrimaryOutcome.INVALID_EVIDENCE,
    FailureClass.POLICY_REFUSED: PrimaryOutcome.INVALID_EVIDENCE,
    FailureClass.MISSING_SCIENTIFIC_AUTHORITY: PrimaryOutcome.INVALID_EVIDENCE,
}


def reading_outcome(
    conclusion: EmpiricalConclusion, result: ResultCheck
) -> tuple[PrimaryOutcome, str]:
    """The outcome of one reading: the frozen rule's, on a validated result only."""

    if not result.ok:
        return PrimaryOutcome.INVALID_EVIDENCE, result.reason
    return READING_OUTCOME.get(
        conclusion, (PrimaryOutcome.INVALID_EVIDENCE, f"unreadable_{conclusion}")
    )


def record_outcome(
    context: Any,
    *,
    state: PrimaryOutcome,
    reason: str,
    role: ExperimentRole,
    idea_id: str,
    idea_version: int,
    detail: Mapping[str, Any],
    contract_id: str | None = None,
    experiment: IdeaExperiment | None = None,
    chain: VerifiedChain | None = None,
    receipt: ExecutionReceipt | None = None,
    result: ResultCheck | None = None,
    estimate: float | None = None,
    units: Sequence[tuple[int, str, str | None]] = (),
) -> ScienceOutcome:
    """Store the outcome document and its immutable row. Ordinary code only.

    ``units`` -- ``(index, receipt id, validated result sha256)`` -- are every
    execution a campaign's outcome was computed from; ``receipt`` is then the
    campaign's last unit, the receipt the reading is anchored on, and
    ``result`` the combined result's check.
    """

    digests = (
        chain.digests()
        if chain is not None
        else row_digests(
            context.portfolio, experiment.plan_digest if experiment else None
        )
    )
    document = {
        "schema": OUTCOME_SCHEMA,
        "computed_by": COMPUTED_BY,
        "state": str(state),
        "reason": reason,
        "project_id": context.project_id,
        "idea_id": idea_id,
        "idea_version": idea_version,
        "role": str(role),
        "contract_id": contract_id,
        "experiment_id": experiment.experiment_id if experiment else None,
        "receipt_id": receipt.receipt_id if receipt else None,
        "chain": digests,
        "result": result.record() if result is not None else None,
        "estimate": estimate,
        "detail": dict(detail),
        **(
            {
                "campaign": {
                    "unit_count": len(units),
                    "units": [
                        {"index": index, "receipt_id": receipt_id, "result_sha256": sha}
                        for index, receipt_id, sha in units
                    ],
                }
            }
            if units
            else {}
        ),
    }
    ref = context.artifacts.put_bytes(
        canonical(document),
        media_type="application/json",
        role="science_outcome",
        producer=COMPUTED_BY,
    )
    context.artifacts.link(
        ref, role="science_outcome", run_id=getattr(context, "run_id", None)
    )
    return context.portfolio.record_outcome(
        project_id=context.project_id,
        idea_id=idea_id,
        idea_version=idea_version,
        role=role,
        state=state,
        reason=reason[:500],
        contract_id=contract_id,
        experiment_id=experiment.experiment_id if experiment else None,
        receipt_id=receipt.receipt_id if receipt else None,
        result_sha256=(result.sha256 if result is not None and result.ok else None),
        estimate=estimate,
        record_artifact_id=str(ref.artifact_id),
        unit_count=len(units) if units else None,
        units=tuple(units),
        **digests,
    )


def row_digests(store: Any, plan_digest: str | None) -> dict[str, Any]:
    """The chain an experiment's rows name, for an outcome whose chain broke.

    Unverified, and used only to *name* what a refused execution was bound
    to: an INVALID_EVIDENCE outcome must still say which plan it is about,
    and the database checks the names agree with one another.
    """

    plan = store.get_science_object(plan_digest)
    if plan is None:
        return {}
    design = store.get_science_object(plan.parent_digest)
    return {
        "contract_digest": design.parent_digest if design is not None else None,
        "design_digest": plan.parent_digest,
        "plan_digest": plan.object_digest,
        "capability_ref": plan.capability_ref,
        "capability_digest": plan.capability_digest,
    }


def estimate_of(result: Any) -> float | None:
    """The primary statistic's value from an analysis result, when it is finite."""

    value = getattr(result, "value", None)
    if value is None and isinstance(result, Mapping):
        value = result.get("primary_statistic_value")
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


# ---------------------------------------------------------- for the gate --
def _reading_chain(
    store: Any, artifacts: Any, row: IdeaEvidence, *, role: ExperimentRole
) -> tuple[ScienceChain, ScienceOutcome | None]:
    from research_os.portfolio import provenance

    problems: list[str] = []
    receipt = store.receipt_for_job(row.job_id) if row.job_id else None
    experiment: IdeaExperiment | None = None
    outcome: ScienceOutcome | None = None
    capability_ref: str | None = None
    if receipt is None:
        problems.append("no trusted execution receipt names this evidence's execution")
    else:
        experiment = store.require_experiment(receipt.experiment_id)
        live = store.get_experiment(
            idea_id=row.idea_id, idea_version=row.idea_version, role=role
        )
        if live is None or live.experiment_id != experiment.experiment_id:
            problems.append(
                "the execution is no longer this version's live experiment; its "
                "chain was replaced and its evidence does not transfer"
            )
        if (
            experiment.evidence_id != row.evidence_id
            or experiment.job_id != receipt.job_id
            or experiment.state is not ExperimentState.INTERPRETED
        ):
            problems.append("the experiment no longer names this reading")
        try:
            document = provenance.verified_receipt(artifacts, receipt)
        except provenance.ProvenanceError as exc:
            problems.append(str(exc))
            document = {}
        try:
            chain = verify_plan(store, artifacts, experiment)
            capability_ref = chain.capability.ref
            if receipt.plan_digest != chain.plan.digest:
                problems.append("the receipt names another plan")
            science = dict(document.get("science") or {})
            if (
                science.get("plan_digest") != chain.plan.digest
                or science.get("design_digest") != chain.design.digest
                or science.get("contract_digest") != chain.contract.digest
            ):
                problems.append("the receipt's science block does not name this chain")
            if (document.get("code") or {}).get("base_commit") != chain.code_commit:
                problems.append("the execution ran a commit other than the plan's")
            outcome = store.outcome_for_receipt(receipt.receipt_id)
            if outcome is None:
                problems.append("no system-computed outcome reads this execution")
            else:
                if outcome.state not in READ_OUTCOMES:
                    problems.append(f"the execution's outcome is {outcome.state}")
                digests = chain.digests()
                if any(
                    getattr(outcome, key) != value for key, value in digests.items()
                ):
                    problems.append("the recorded outcome names another chain")
                try:
                    provenance.verified_json(artifacts, outcome.record_artifact_id)
                except provenance.ProvenanceError as exc:
                    problems.append(f"the outcome record: {exc}")
                if chain.is_campaign:
                    problems.extend(
                        campaign_reading_problems(
                            store, artifacts, chain, anchor=receipt, outcome=outcome
                        )
                    )
                elif store.outcome_units(outcome.outcome_id):
                    problems.append("a single execution's outcome names campaign units")
        except ChainError as exc:
            problems.append(str(exc))
    return (
        ScienceChain(
            evidence_id=row.evidence_id,
            experiment_id=experiment.experiment_id if experiment else None,
            role=role,
            outcome=outcome.state if outcome is not None else None,
            capability_ref=capability_ref,
            chain_intact=not problems,
            problems=tuple(problems),
        ),
        outcome,
    )


def campaign_reading_problems(
    store: Any,
    artifacts: Any,
    chain: VerifiedChain,
    *,
    anchor: ExecutionReceipt,
    outcome: ScienceOutcome,
) -> list[str]:
    """Everything a campaign's reading rests on, re-verified now.

    Every unit the plan froze is named by the outcome, once, from one
    attempt; each unit's receipt is the runner's, re-hashed, of that unit of
    that plan at the plan's commit; each unit's result is stored by content,
    re-hashes, is the output that receipt recorded and still satisfies the
    declared schema; and the combined result, rebuilt from those bytes by the
    frozen aggregation, hashes to what the outcome names. Nothing here trusts
    that a row exists: each claim is recomputed from what is stored.
    """

    from research_os.portfolio import campaign as campaigns
    from research_os.portfolio import provenance

    problems: list[str] = []
    named = store.outcome_units(outcome.outcome_id)
    frozen = chain.units
    if outcome.unit_count != len(frozen) or len(named) != len(frozen):
        problems.append(
            f"the reading names {len(named)} of the campaign's {len(frozen)} units"
        )
        return problems
    if anchor.receipt_id not in {item.receipt_id for item in named}:
        problems.append("the reading's receipt is not one of its units")
    documents: list[tuple[int, Mapping[str, Any]]] = []
    for unit in named:
        receipt = store.get_receipt(unit.receipt_id)
        if receipt is None:
            problems.append(f"unit {unit.unit_index} has no receipt")
            continue
        try:
            document = provenance.verified_receipt(artifacts, receipt)
        except provenance.ProvenanceError as exc:
            problems.append(f"unit {unit.unit_index}: {exc}")
            continue
        try:
            frozen_unit = chain.unit(unit.unit_index)
        except ChainError as exc:
            problems.append(str(exc))
            continue
        if (
            receipt.experiment_id != anchor.experiment_id
            or receipt.plan_digest != chain.plan.digest
            or receipt.unit_index != unit.unit_index
            or receipt.unit_attempt != anchor.unit_attempt
            or receipt.spec_digest != frozen_unit.get("spec_digest")
        ):
            problems.append(
                f"unit {unit.unit_index}'s receipt is not that unit of this "
                f"campaign's attempt"
            )
        science = dict(document.get("science") or {})
        if (
            science.get("plan_digest") != chain.plan.digest
            or science.get("design_digest") != chain.design.digest
            or science.get("contract_digest") != chain.contract.digest
        ):
            problems.append(
                f"unit {unit.unit_index}'s receipt does not name this chain"
            )
        if (document.get("code") or {}).get("base_commit") != chain.code_commit:
            problems.append(
                f"unit {unit.unit_index} ran a commit other than the plan's"
            )
        stored = store.unit_result(unit.receipt_id)
        if stored is None or stored.result_sha256 != unit.result_sha256:
            problems.append(f"unit {unit.unit_index}'s validated result is not stored")
            continue
        path = chain.unit_result_path(unit.unit_index) or ""
        recorded = {
            str(item[0]): str(item[1]) for item in document.get("outputs") or ()
        }
        if recorded.get(path) != stored.result_sha256:
            problems.append(
                f"unit {unit.unit_index}'s stored result is not the output its "
                f"receipt recorded"
            )
            continue
        try:
            data = bytes(artifacts.get_bytes(stored.result_artifact_id))
        except ResearchOSError as exc:
            problems.append(f"unit {unit.unit_index}'s result bytes: {exc}")
            continue
        check = validate_result_bytes(
            chain, path=path, data=data, recorded_sha=stored.result_sha256
        )
        if not check.ok:
            problems.append(f"unit {unit.unit_index}'s result: {check.detail}")
            continue
        documents.append((unit.unit_index, json.loads(data.decode("utf-8"))))
    if problems:
        return problems
    combined = campaigns.combine(chain.aggregation, documents)
    if not combined.ok:
        problems.append(f"the campaign's units do not combine: {combined.detail}")
    elif combined.sha256 != outcome.result_sha256:
        problems.append(
            "the combined result rebuilt from the stored units is not the one the "
            "outcome names"
        )
    return problems


def science_chains(
    store: Any,
    artifacts: Any,
    *,
    idea_id: str,
    idea_version: int,
    evidence: Sequence[IdeaEvidence],
) -> tuple[ScienceChain, ...]:
    """Every executed experiment and replication row's chain, re-verified now.

    Read by the readiness gate (INV-08, ``docs/SCIENCE_EXECUTION.md``): the
    presence of an evidence row is not enough. For a replication, agreement
    is the capability's frozen comparison rule -- the two system-computed
    outcomes are the same state -- measured here from the two recorded
    outcomes, never from prose.
    """

    records: list[ScienceChain] = []
    primary_state: PrimaryOutcome | None = None
    pending: list[
        tuple[ScienceChain, ScienceOutcome | None, ExecutionReceipt | None]
    ] = []
    for row in evidence:
        if row.idea_id != idea_id or row.idea_version != idea_version or not row.job_id:
            continue
        if row.kind is EvidenceKind.EXPERIMENT:
            record, outcome = _reading_chain(
                store, artifacts, row, role=ExperimentRole.PRIMARY
            )
            if record.chain_intact and outcome is not None:
                primary_state = outcome.state
            records.append(record)
        elif row.kind is EvidenceKind.REPLICATION:
            record, outcome = _reading_chain(
                store, artifacts, row, role=ExperimentRole.REPLICATION
            )
            pending.append((record, outcome, store.receipt_for_job(row.job_id)))
    for record, outcome, receipt in pending:
        agrees: bool | None = None
        problems = list(record.problems)
        if outcome is not None and receipt is not None:
            parent = store.outcome_for_receipt(receipt.parent_receipt_id)
            if parent is None:
                problems.append("the primary execution it replicates has no outcome")
            else:
                agrees = parent.state == outcome.state
                if primary_state is not None and parent.state != primary_state:
                    problems.append(
                        "it replicates a primary reading that is not current"
                    )
        records.append(
            record.model_copy(
                update={
                    "agrees_with_primary": agrees,
                    "problems": tuple(problems),
                    "chain_intact": not problems,
                }
            )
        )
    return tuple(records)


__all__ = [
    "CAMPAIGN_SCHEMA",
    "CONTRACT_SCHEMA",
    "DESIGN_SCHEMA",
    "FAILURE_OUTCOME",
    "PLAN_SCHEMA",
    "ChainError",
    "ResultCheck",
    "VerifiedChain",
    "campaign_payload",
    "campaign_reading_problems",
    "contract_payload",
    "design_payload",
    "freeze",
    "load",
    "object_digest",
    "plan_payload",
    "reading_outcome",
    "record_outcome",
    "requirements_from_analysis",
    "science_chains",
    "unit_record",
    "validate_result",
    "validate_result_bytes",
    "verify_plan",
]
