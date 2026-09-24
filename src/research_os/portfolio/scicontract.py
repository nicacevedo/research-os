"""The scientific contract: what is frozen, how it is hashed, how it is checked.

A contract binds three things, each by content digest:

```text
hypothesis   the idea version's content digest        -- what is claimed
analysis     the frozen AnalysisSpec                   -- what would settle it
design       the command, the grid, the parameters     -- how it is measured
```

and ``contract_digest`` covers all three. An execution is bound to a contract
by recording its digest in the preregistration, and an interpretation by
recording it in the analysis document -- so "this number was read under this
rule, from this measurement, of this claim" is a chain of hashes rather than a
chain of trust.

**What the digests deliberately do not cover.** No provider, model, call id or
prompt identity: those are *provenance*, recorded beside the digests in the
frozen document, and a contract derived by a different model family with the
same content is the same science. No time limit or resource setting either:
those are *implementation*, and an implementation repair may change them
without changing what is being measured -- see :func:`implementation_fields`.

**What verification refuses.** A contract whose stored document no longer
hashes to the digest on its row, a row whose digest differs from its document,
and a hypothesis that is not the idea version the contract names. Each is
``MISSING_SCIENTIFIC_AUTHORITY``: the thing about to run, or about to be read,
is not the thing that was written down, and that is not something this layer
may decide past.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_os.errors import ResearchOSError
from research_os.portfolio.contracts import AnalysisSpec, DesignSpecification
from research_os.portfolio.models import (
    ContractKind,
    ContractState,
    IdeaVersion,
    ScientificContract,
)
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ExecutionSpec

ANALYSIS_DIGEST_VERSION = "panalysis-v1"
DESIGN_DIGEST_VERSION = "pdesign-v1"
CONTRACT_DIGEST_VERSION = "pcontract-v1"

ANALYSIS_SCHEMA = "portfolio-analysis-v1"
DESIGN_SCHEMA = "portfolio-design-v1"
CONTRACT_SCHEMA = "portfolio-scientific-contract-v1"

#: The ExecutionSpec fields an implementation repair may change. Everything
#: else an execution carries is in the design digest, and a repair that moves
#: any of it is a different experiment.
IMPLEMENTATION_FIELDS: frozenset[str] = frozenset({"timeout_seconds", "resources"})


class ContractIntegrityError(ResearchOSError):
    """The stored contract does not hash to what its row says it is."""

    failure_class = FailureClass.MISSING_SCIENTIFIC_AUTHORITY


def canonical_bytes(payload: Any) -> bytes:
    """The one serialisation every digest here is taken over."""

    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def _digest(version: str, payload: Any) -> str:
    return f"{version}:{hashlib.sha256(canonical_bytes(payload)).hexdigest()}"


# ------------------------------------------------------------- analysis --
def analysis_payload(spec: AnalysisSpec) -> dict[str, Any]:
    return spec.model_dump(mode="json")


def analysis_digest(spec: AnalysisSpec) -> str:
    return _digest(ANALYSIS_DIGEST_VERSION, analysis_payload(spec))


def hypothesis_record(version: IdeaVersion) -> dict[str, Any]:
    """What the contract says it tests. Bound by ``content_digest``."""

    return {
        "idea_id": version.idea_id,
        "version": version.version,
        "content_digest": version.content_digest,
        "title": version.title,
        "research_question": version.research_question,
        "core_idea": version.core_idea,
        "mechanism": version.mechanism,
        "falsifier": version.falsifier,
    }


def analysis_document(
    *,
    contract_id: str,
    project_id: str,
    version: IdeaVersion,
    role: str,
    kind: ContractKind,
    spec: AnalysisSpec,
    provenance: Mapping[str, Any],
    parent_contract_id: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": ANALYSIS_SCHEMA,
        "contract_id": contract_id,
        "project_id": project_id,
        "role": role,
        "kind": str(kind),
        "parent_contract_id": parent_contract_id,
        "hypothesis": hypothesis_record(version),
        "hypothesis_digest": version.content_digest,
        "analysis": analysis_payload(spec),
        "analysis_digest": analysis_digest(spec),
        "provenance": dict(provenance),
    }


# --------------------------------------------------------------- design --
def design_payload(
    design: DesignSpecification,
    *,
    spec: ExecutionSpec,
    composed: Mapping[str, str],
) -> dict[str, Any]:
    """The scientific design, as it is hashed.

    ``composed`` maps a generated parameter's name to the sha256 of the
    document frozen for it, and the digest carries that sha256 rather than the
    document: the bytes live in the content-addressed store under exactly that
    digest, so the design is identified by what was composed without carrying
    it twice. The resolved ``argv``, the collected ``outputs`` and the
    composed ``inputs`` are here because they *are* the measurement; the time
    limit and resources are not, because a repair may change them.
    """

    parameters: dict[str, Any] = {}
    for name, value in sorted(dict(design.command_parameters).items()):
        parameters[name] = (
            {"composed_sha256": composed[name]} if name in composed else value
        )
    return {
        "command": design.command,
        "command_parameters": parameters,
        "seeds": list(spec.seeds),
        "variables": [item.model_dump(mode="json") for item in design.variables],
        "sampling": design.sampling,
        "dataset_identity": design.dataset_identity,
        "falsification_criterion": design.falsification_criterion,
        "variation_kind": design.variation_kind,
        "variation_detail": design.variation_detail,
        "argv": list(spec.argv),
        "outputs": sorted(spec.outputs),
        "inputs": [list(item) for item in spec.inputs],
        # The environment the program reads, and the environment it runs in.
        # The seeds reach the program through `env` (`RESEARCH_OS_SEED_i`),
        # so a design that froze `seeds` and not `env` froze a list the
        # program never reads: an independent mutation review ran a job with
        # a different seed under an unchanged, verifying contract.
        "env": dict(sorted(dict(spec.env).items())),
        "environment": dict(sorted(dict(spec.environment).items())),
    }


def design_digest(payload: Mapping[str, Any]) -> str:
    return _digest(DESIGN_DIGEST_VERSION, dict(payload))


def implementation_fields(spec: ExecutionSpec) -> dict[str, Any]:
    return {
        "timeout_seconds": spec.timeout_seconds,
        "resources": dict(sorted(spec.resources.items())),
    }


# ------------------------------------------------------------- contract --
def contract_payload(
    *,
    idea_id: str,
    idea_version: int,
    hypothesis_digest: str,
    role: str,
    kind: ContractKind,
    analysis_digest: str,
    design_digest: str,
    parent_contract_digest: str | None = None,
) -> dict[str, Any]:
    return {
        "schema": CONTRACT_SCHEMA,
        "idea_id": idea_id,
        "idea_version": idea_version,
        "hypothesis_digest": hypothesis_digest,
        "role": role,
        "kind": str(kind),
        "analysis_digest": analysis_digest,
        "design_digest": design_digest,
        "parent_contract_digest": parent_contract_digest,
    }


def contract_digest(**fields: Any) -> str:
    return _digest(CONTRACT_DIGEST_VERSION, contract_payload(**fields))


def contract_document(
    *,
    contract: ScientificContract | None = None,
    version: IdeaVersion,
    spec: AnalysisSpec,
    design: Mapping[str, Any],
    execution: Mapping[str, Any],
    provenance: Mapping[str, Any],
    parent_contract_digest: str | None = None,
    identity: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The whole frozen contract, as the artifact a reader reviews.

    ``execution`` is recorded and not hashed: the reserved experiment id, the
    specification the design resolved to, and the preregistration artifact.
    It is what lets a crash between freezing the contract and recording the
    experiment be recovered without designing anything twice.

    ``identity`` stands in for ``contract`` when the document has to exist
    before its row does -- an exploratory contract is inserted already frozen,
    so its document is written first. It carries the same keys.
    """

    ident: dict[str, Any] = (
        dict(identity)
        if identity is not None
        else {
            "contract_id": contract.contract_id,
            "project_id": contract.project_id,
            "idea_id": contract.idea_id,
            "idea_version": contract.idea_version,
            "role": str(contract.role),
            "kind": contract.kind,
            "hypothesis_digest": contract.hypothesis_digest,
            "analysis_digest": contract.analysis_digest,
            "parent_contract_id": contract.parent_contract_id,
        }
        if contract is not None
        else {}
    )
    design_hash = design_digest(design)
    fields = {
        "idea_id": ident["idea_id"],
        "idea_version": ident["idea_version"],
        "hypothesis_digest": ident["hypothesis_digest"],
        "role": str(ident["role"]),
        "kind": ContractKind(str(ident["kind"])),
        "analysis_digest": ident["analysis_digest"],
        "design_digest": design_hash,
        "parent_contract_digest": parent_contract_digest,
    }
    return {
        **contract_payload(**fields),
        "contract_id": ident["contract_id"],
        "project_id": ident["project_id"],
        "parent_contract_id": ident.get("parent_contract_id"),
        "hypothesis": hypothesis_record(version),
        "analysis": analysis_payload(spec),
        "design": dict(design),
        "contract_digest": contract_digest(**fields),
        "execution": dict(execution),
        "provenance": dict(provenance),
    }


# --------------------------------------------------------------- verify --
@dataclass(frozen=True, slots=True)
class VerifiedContract:
    """A contract whose stored bytes were re-hashed and matched its row."""

    contract: ScientificContract
    analysis: AnalysisSpec
    design: Mapping[str, Any] | None
    document: Mapping[str, Any]


def _load(artifacts: Any, artifact_id: str | None, *, what: str) -> dict[str, Any]:
    if not artifact_id:
        raise ContractIntegrityError(f"the {what} has no stored artifact")
    try:
        loaded = json.loads(artifacts.get_text(artifact_id))
    except (ResearchOSError, ValueError, UnicodeDecodeError) as exc:
        raise ContractIntegrityError(f"the {what} is unreadable: {exc}") from None
    if not isinstance(loaded, dict):
        raise ContractIntegrityError(f"the {what} is not a JSON object")
    return loaded


def _analysis_from(
    document: Mapping[str, Any], contract: ScientificContract
) -> AnalysisSpec:
    try:
        spec = AnalysisSpec.model_validate(document["analysis"])
    except (KeyError, ValueError) as exc:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored analysis is not an analysis: {exc}"
        ) from None
    rebuilt = analysis_digest(spec)
    if rebuilt != contract.analysis_digest:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored analysis hashes to {rebuilt} and the "
            f"contract says {contract.analysis_digest}. A rule that differs from "
            f"the one frozen is a rule fixed after the fact, and this layer may "
            f"not read a result under it."
        )
    if spec.analysable is not contract.analysable:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s row and stored analysis disagree about "
            f"whether an analysis exists"
        )
    return spec


def verify(
    artifacts: Any,
    contract: ScientificContract,
    *,
    version: IdeaVersion | None = None,
    parent: ScientificContract | None = None,
) -> VerifiedContract:
    """Re-hash a contract's stored halves against its row, or refuse.

    Called before anything runs under a contract and again before anything
    is read under one. ``version``, when supplied, must be the idea version
    the contract names -- a contract is about one hypothesis, and reading a
    measurement against a revised one is reading it against a different
    claim. ``parent`` must be exactly the contract the row names as its
    parent (a replication's primary), or ``None`` when it names none: the
    parent's digest is inside this contract's.
    """

    if version is not None and (
        version.idea_id != contract.idea_id
        or version.version != contract.idea_version
        or version.content_digest != contract.hypothesis_digest
    ):
        raise ContractIntegrityError(
            f"{contract.contract_id} tests {contract.idea_id} v{contract.idea_version} "
            f"({contract.hypothesis_digest}); it cannot be used for "
            f"{version.idea_id} v{version.version} ({version.content_digest})"
        )
    if (parent.contract_id if parent is not None else None) != (
        contract.parent_contract_id
    ):
        raise ContractIntegrityError(
            f"{contract.contract_id} names parent contract "
            f"{contract.parent_contract_id or '(none)'} and was verified against "
            f"{parent.contract_id if parent is not None else '(none)'}"
        )
    if (
        parent is not None
        and contract.kind is ContractKind.PREREGISTERED
        and (
            contract.analysis_digest != parent.analysis_digest
            or contract.analysable is not parent.analysable
        )
    ):
        raise ContractIntegrityError(
            f"{contract.contract_id} names {parent.contract_id} as the primary it "
            f"replicates and freezes a different analysis; a replication inherits "
            f"its primary's analysis or it is not a replication of it"
        )
    analysis_doc = _load(artifacts, contract.analysis_artifact_id, what="analysis")
    if analysis_doc.get("parent_contract_id") != contract.parent_contract_id:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored analysis names a different parent "
            f"contract than its row"
        )
    if analysis_doc.get("hypothesis_digest") != contract.hypothesis_digest:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored analysis names a different hypothesis"
        )
    spec = _analysis_from(analysis_doc, contract)
    if contract.state is not ContractState.FROZEN:
        return VerifiedContract(
            contract=contract, analysis=spec, design=None, document=analysis_doc
        )

    document = _load(artifacts, contract.contract_artifact_id, what="contract")
    if document.get("parent_contract_id") != contract.parent_contract_id:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored contract names a different parent "
            f"contract than its row"
        )
    if _analysis_from(document, contract) != spec:
        raise ContractIntegrityError(  # pragma: no cover - digest equality implies it
            f"{contract.contract_id}'s two stored analyses differ"
        )
    design = document.get("design")
    if not isinstance(design, dict):
        raise ContractIntegrityError(f"{contract.contract_id} stores no design")
    rebuilt_design = design_digest(design)
    if rebuilt_design != contract.design_digest:
        raise ContractIntegrityError(
            f"{contract.contract_id}'s stored design hashes to {rebuilt_design} and "
            f"the contract says {contract.design_digest}"
        )
    rebuilt = contract_digest(
        idea_id=contract.idea_id,
        idea_version=contract.idea_version,
        hypothesis_digest=contract.hypothesis_digest,
        role=str(contract.role),
        kind=contract.kind,
        analysis_digest=contract.analysis_digest,
        design_digest=rebuilt_design,
        parent_contract_digest=parent.contract_digest if parent else None,
    )
    if (
        rebuilt != contract.contract_digest
        or document.get("contract_digest") != rebuilt
    ):
        raise ContractIntegrityError(
            f"{contract.contract_id} hashes to {rebuilt} and records "
            f"{contract.contract_digest}"
        )
    return VerifiedContract(
        contract=contract, analysis=spec, design=design, document=document
    )


# --------------------------------------------------------------- render --
def requirements_block(spec: AnalysisSpec) -> list[str]:
    """What the experiment designer is shown of a frozen analysis.

    **Everything it must produce, and nothing it could aim at.** The
    observables, their fields and inclusion rules, the reductions and which
    fields they read, and the support the data must exhibit -- because a
    design that does not produce those cannot be analysed. Not the
    predicates: a designer that knows the threshold can choose a grid that
    lands on the right side of it, which is the co-design this split exists
    to remove.
    """

    if not spec.analysable:
        return [
            "NO ANALYSIS IS POSSIBLE for this idea with the observables available: "
            + spec.unanalysable_reason,
            (
                "Any measurement of it can conclude nothing stronger than "
                "INSUFFICIENT. Design one only if it would produce the "
                "observable that is missing; otherwise say it is not testable."
            ),
        ]
    lines = [f"estimand: {spec.estimand}"]
    for item in spec.observables:
        detail = (
            f"records at {item.path or '(the top level)'}"
            if item.kind == "records"
            else f"one number at {item.path or '(the top level)'}"
        )
        lines.append(
            f"observable {item.name}: the run must write {item.source} -- {detail}"
        )
        if item.fields:
            lines.append(f"    every record must carry: {', '.join(item.fields)}")
        for condition in item.include:
            lines.append(f"    records are analysed only when {condition.rendered()}")
        if item.kind == "records":
            lines.append(
                "    a record missing a required field "
                + (
                    "makes the whole analysis INSUFFICIENT"
                    if item.incomplete_records == "insufficient"
                    else "is excluded and counted"
                )
            )
    for reduction in spec.reductions:
        lines.append(f"quantity {reduction.name}: {_rendered_reduction(reduction)}")
    lines.append(f"primary statistic: {spec.primary_statistic}")
    for rule in spec.support:
        needs = [f"at least {rule.min_records} record(s)"] + [
            f"at least {count} distinct value(s) of {name}"
            for name, count in sorted(rule.min_distinct.items())
        ]
        lines.append(
            f"the data in {rule.observable} must hold "
            + " and ".join(needs)
            + " -- otherwise the result is INSUFFICIENT whatever it shows"
        )
    if spec.uncertainty is not None:
        lines.append(
            "the primary statistic's uncertainty is computed over the analysed "
            f"records ({spec.uncertainty.method}), so the design must produce "
            "enough of them for an interval to mean something"
        )
    lines.append(
        "(the thresholds that decide the conclusion were fixed with this analysis "
        "and are deliberately not shown to you)"
    )
    return lines


#: What a withheld threshold is replaced by in text the designer reads.
WITHHELD = "[threshold withheld]"


def withhold_thresholds(lines: Sequence[str], spec: AnalysisSpec) -> list[str]:
    """The same lines with every number equal to a decision threshold removed.

    For the idea itself, which the experiment designer must see: its
    falsifier is the sentence in which the bar is usually written ("the
    interaction exceeds 0.25"), and the analysis designer read that sentence
    and froze the bar from it. The analysis's own free text is refused at
    freeze time when it restates a threshold; the idea's text is not the
    analysis's to refuse, so here it is redacted instead. 0 and 1 are left,
    for the reason they are exempt there. Direction stays visible -- the
    designer has to know what the idea claims -- and that residue is stated
    in `docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md`, not hidden.
    """

    if not spec.analysable or spec.success is None or spec.failure is None:
        return list(lines)
    thresholds = {
        item.threshold
        for item in (spec.success, spec.failure)
        if item.threshold not in {0.0, 1.0}
    }
    if not thresholds:
        return list(lines)

    def redact(match: re.Match[str]) -> str:
        try:
            value = float(match.group(0))
        except ValueError:  # pragma: no cover - the pattern is numeric
            return match.group(0)
        return (
            WITHHELD if value in thresholds or -value in thresholds else match.group(0)
        )

    return [
        re.sub(r"(?<![\w.])-?\d+(?:\.\d+)?(?:[eE]-?\d+)?(?![\w.])", redact, line)
        for line in lines
    ]


def _rendered_reduction(reduction: Any) -> str:
    """One reduction, said precisely -- without any threshold, which is not here."""

    where = (
        " where " + " and ".join(item.rendered() for item in reduction.where)
        if reduction.where
        else ""
    )
    op = reduction.op
    if op in {"difference", "ratio"}:
        return f"{op} of {reduction.of[0]} and {reduction.of[1]}"
    if op == "value":
        return f"the number read from observable {reduction.observable}"
    if op in {"count", "fraction"}:
        return f"{op} of records of {reduction.observable}{where}"
    if op == "correlation":
        return (
            f"correlation of {reduction.field} and {reduction.other_field} over "
            f"{reduction.observable}{where}"
        )
    if op == "ols_coefficient":
        return (
            f"least-squares coefficient of {reduction.coefficient} in "
            f"{reduction.response} ~ 1 + {' + '.join(reduction.terms)} over "
            f"{reduction.observable}{where}"
        )
    quantile = f" q={reduction.q:g}" if reduction.q is not None else ""
    return f"{op}{quantile} of {reduction.field} over {reduction.observable}{where}"


def analysis_lines(spec: AnalysisSpec) -> list[str]:
    """The full frozen analysis, thresholds included, for a reviewer."""

    lines = (
        requirements_block(spec)[:-1] if spec.analysable else requirements_block(spec)
    )
    if spec.analysable:
        lines.append(f"decision (fixed before the design): {spec.rendered_decision()}")
        lines.append(
            f"stopping rule: {spec.stopping_rule}; on missing information: {spec.on_missing}"
        )
    return lines


def capability_request_from_analysis(
    spec: AnalysisSpec, *, reason: str
) -> dict[str, Any]:
    """A capability request derived by ordinary code from a frozen analysis.

    Used when a project declares no command at all: the analysis already
    says, precisely, which raw outputs a command would have to write, so
    asking a model to restate that would be paying for a paraphrase.
    """

    outputs = [
        {
            "source": item.source,
            "kind": item.kind,
            "path": item.path,
            "fields": list(item.fields),
        }
        for item in spec.observables
    ]
    return {
        "name": "",
        "purpose": spec.estimand or "(no analysis was possible)",
        "inputs": "",
        "outputs": outputs,
        "why_declared_commands_do_not_suffice": reason,
        "derived_by": "portfolio.scicontract.capability_request_from_analysis",
    }


def declared_command_set(project_id: str) -> dict[str, Any]:
    """The experiment commands the researcher declared for this project.

    From ``experiments.yaml`` under the config home, outside every worktree:
    what exists here is a person's decision. Empty when nothing is declared
    or the file is absent. Lives here, beside the digest over it, so the
    tick can observe a change to it without importing the empirical route.
    """

    from research_os.errors import ResearchOSError as _Error
    from research_os.experiment.config import load_config as load_experiment_config

    try:
        config = load_experiment_config()
    except _Error:
        return {}
    project = config.projects.get(project_id)
    return dict(project.commands) if project is not None else {}


def command_set_digest(commands: Mapping[str, Any]) -> str:
    """The declared experiment capability of one project, by digest.

    Over each command's name, argv, parameters and outputs -- what decides
    whether an analysis's observables can be produced -- and nothing else, so
    a reworded description does not read as a new capability.
    """

    payload: list[Any] = []
    for name, spec in sorted(commands.items()):
        payload.append(
            {
                "name": name,
                "argv": list(getattr(spec, "argv", ())),
                "outputs": sorted(getattr(spec, "outputs", ())),
                "parameters": [
                    {
                        "name": item.name,
                        "type": str(item.type),
                        "required": bool(item.required),
                    }
                    for item in getattr(spec, "parameters", ())
                ],
            }
        )
    return _digest("pcommands-v1", payload)


__all__ = [
    "CONTRACT_SCHEMA",
    "IMPLEMENTATION_FIELDS",
    "ContractIntegrityError",
    "VerifiedContract",
    "analysis_digest",
    "analysis_document",
    "analysis_lines",
    "capability_request_from_analysis",
    "command_set_digest",
    "contract_digest",
    "contract_document",
    "design_digest",
    "design_payload",
    "hypothesis_record",
    "implementation_fields",
    "requirements_block",
    "verify",
]
