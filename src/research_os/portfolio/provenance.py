"""Trusted execution provenance: receipts the runner writes, and the replication chain.

``docs/ARCHITECTURE_INVARIANTS.md`` INV-02, INV-06, INV-07 and INV-08, and
``sql/0046``.

**Who writes an execution receipt.** Research OS's own runner, and nothing
else. The first integrity round made replication independence rest on an
``execution_receipt`` object the *scientific program* wrote into its own
output, and refused one found in a model-composed input by scanning the raw
bytes for the key. The independent review of ``8e92e8c`` sent the key
through escaped (``\\u0065xecution_receipt``) inside a JSON string of a
closed-schema plan; the declared command parsed and echoed it while ignoring
its seed, and the replication was recorded independent and SUPPORTS. Output
written by a program -- and therefore by anything that program echoes -- is
not a place provenance can come from.

So a receipt here is built by :func:`write_receipt` from what the runner
itself observed, at the moment the executor returned:

- the execution it launched (the job id), for which experiment, action, run
  and work item;
- the declared command's identity (its name and the digest of the
  researcher's declaration, attestation included) and the code identity (the
  workspace's base commit);
- the configuration it *delivered*: the argument vector, the environment and
  the seeds it handed the process;
- the immutable inputs it materialised and re-hashed;
- the digest of every declared output, hashed when the process exited;
- for a replication, the frozen manifest (artifact and digest) and the
  parent's receipt.

It is content-addressed in the artifact store and indexed by an immutable
row. A program's output may contain whatever domain metadata it likes; none
of it is read into a receipt, and none of it is read as one.

**What a replication can and cannot establish.** Three findings, kept apart
(:class:`ReplicationFinding`):

1. *configuration independence* -- proved: a separate execution, bound to a
   manifest frozen before it ran, was delivered a configuration that differs
   from its parent's receipt in exactly what the manifest says it varies;
2. *perturbation validity* -- **attested, never proved**: that the
   computation uses what was varied. No generic runtime can establish that
   arbitrary code used a delivered seed in its mathematics (a program can
   read ``--seed`` and ignore it -- the review's Case C). The researcher's
   declaration attests it (``perturbation_attestation`` in
   ``experiments.yaml``, frozen into the manifest), and the record says
   *attested*;
3. *agreement* -- the scientific readings compared, recorded beside.

A replication counts for readiness only with (1) and (2), over a chain that
:func:`replication_provenance` re-verifies against what is stored *now*.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from research_os.errors import ResearchOSError
from research_os.portfolio.ids import new_execution_receipt_id
from research_os.portfolio.models import (
    EvidenceKind,
    ExecutionReceipt,
    ExperimentRole,
    ExperimentState,
    IdeaEvidence,
    IdeaExperiment,
    ReplicationProvenance,
)

RECEIPT_SCHEMA = "research-os-execution-receipt-v1"
MANIFEST_SCHEMA = "portfolio-replication-manifest-v2"
#: Who writes receipts. Recorded in each one, and checked when one is read.
RECEIPT_WRITER = "research_os.portfolio.provenance.write_receipt"


class ProvenanceError(ResearchOSError):
    """A receipt, manifest or chain link that does not verify."""


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    ).encode("utf-8")


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


# ------------------------------------------------------------- identities --
def command_identity(declared: Any) -> dict[str, Any]:
    """The researcher's declaration of one command, as the receipt binds it."""

    return {
        "name": str(getattr(declared, "name", "")),
        "argv": list(getattr(declared, "argv", ())),
        "outputs": sorted(getattr(declared, "outputs", ())),
        "parameters": [
            {
                "name": item.name,
                "type": str(item.type),
                "required": bool(item.required),
            }
            for item in getattr(declared, "parameters", ())
        ],
        "perturbation_attestation": sorted(
            getattr(declared, "perturbation_attestation", ())
        ),
    }


def delivered_configuration(spec: Any) -> dict[str, Any]:
    """What the runner hands the process: argv, environment, seeds, bounds."""

    return {
        "argv": list(spec.argv),
        "env": dict(sorted(dict(spec.env).items())),
        "environment": dict(sorted(dict(spec.environment).items())),
        "seeds": list(spec.seeds),
        "timeout_seconds": int(spec.timeout_seconds),
        "resources": dict(sorted(dict(spec.resources).items())),
    }


# ---------------------------------------------------------------- writing --
def write_receipt(
    context: Any,
    experiment: IdeaExperiment,
    *,
    spec: Any,
    job_id: str,
    declared: Any,
    base_commit: str,
    outcome: Mapping[str, Any],
    outputs: Sequence[tuple[str, str, int]],
    manifest: tuple[str, str] | None = None,
    parent: Mapping[str, Any] | None = None,
) -> ExecutionReceipt:
    """Record, from the runner's own observation, what one execution was.

    Called by :func:`research_os.portfolio.empirical.submit` the moment the
    executor returns, before anything else can touch the workspace. Every
    field comes from the runner -- the spec it built, the job it launched, the
    digests it computed -- and nothing comes from what the program wrote
    except the output *digests*, which are hashes of bytes, not claims.
    """

    identity = command_identity(declared)
    delivered = delivered_configuration(spec)
    inputs = [[str(path), str(sha)] for path, sha in spec.inputs]
    produced = [[str(path), str(sha), int(size)] for path, sha, size in outputs]
    document: dict[str, Any] = {
        "schema": RECEIPT_SCHEMA,
        "written_by": RECEIPT_WRITER,
        "execution_id": job_id,
        "experiment_id": experiment.experiment_id,
        "idea_id": experiment.idea_id,
        "idea_version": experiment.idea_version,
        "role": str(experiment.role),
        "action_id": getattr(context, "action_id", None),
        "run_id": getattr(context, "run_id", None),
        "work_id": getattr(context, "work_id", None),
        "capability": {
            "command": experiment.command,
            "declaration": identity,
            "command_digest": digest(identity),
        },
        "code": {"base_commit": base_commit},
        "specification": {
            "spec_digest": experiment.spec_digest,
            "variation_digest": experiment.variation_digest,
            "contract_id": experiment.contract_id,
        },
        "delivered": delivered,
        "delivered_digest": digest(delivered),
        "inputs": inputs,
        "inputs_digest": digest(inputs),
        "outcome": dict(outcome),
        "outputs": produced,
        "outputs_digest": digest(produced),
        "manifest": (
            None
            if manifest is None
            else {"artifact_id": manifest[0], "digest": manifest[1]}
        ),
        "parent": dict(parent) if parent is not None else None,
    }
    ref = context.artifacts.put_bytes(
        canonical(document),
        media_type="application/json",
        role=f"execution_receipt:{experiment.experiment_id}",
        producer=RECEIPT_WRITER,
    )
    return context.portfolio.record_execution_receipt(
        receipt_id=new_execution_receipt_id(),
        job_id=job_id,
        experiment_id=experiment.experiment_id,
        idea_id=experiment.idea_id,
        idea_version=experiment.idea_version,
        role=experiment.role,
        action_id=document["action_id"],
        run_id=document["run_id"],
        work_id=document["work_id"],
        command=experiment.command,
        command_digest=document["capability"]["command_digest"],
        spec_digest=experiment.spec_digest,
        base_commit=base_commit,
        delivered_digest=document["delivered_digest"],
        inputs_digest=document["inputs_digest"],
        outputs_digest=document["outputs_digest"],
        exit_code=outcome.get("exit_code"),
        manifest_artifact_id=manifest[0] if manifest else None,
        manifest_digest=manifest[1] if manifest else None,
        parent_receipt_id=(parent or {}).get("receipt_id"),
        receipt_artifact_id=str(ref.artifact_id),
    )


# -------------------------------------------------------------- verifying --
def verified_json(artifacts: Any, artifact_id: str | None) -> dict[str, Any]:
    """A stored JSON document whose bytes still hash to its address."""

    if not artifact_id:
        raise ProvenanceError("no artifact is named")
    try:
        data = artifacts.get_bytes(artifact_id)
    except ResearchOSError as exc:
        raise ProvenanceError(
            f"artifact {artifact_id[:12]} is missing: {exc}"
        ) from None
    if hashlib.sha256(data).hexdigest() != artifact_id:
        raise ProvenanceError(
            f"artifact {artifact_id[:12]} no longer hashes to its address"
        )
    try:
        loaded = json.loads(data)
    except (ValueError, UnicodeDecodeError):
        raise ProvenanceError(f"artifact {artifact_id[:12]} is not JSON") from None
    if not isinstance(loaded, dict):
        raise ProvenanceError(f"artifact {artifact_id[:12]} is not a JSON object")
    return loaded


def verified_receipt(artifacts: Any, row: ExecutionReceipt) -> dict[str, Any]:
    """The receipt document, re-hashed and checked field by field against its row."""

    document = verified_json(artifacts, row.receipt_artifact_id)
    manifest = document.get("manifest") or {}
    expected = {
        "schema": RECEIPT_SCHEMA,
        "written_by": RECEIPT_WRITER,
        "execution_id": row.job_id,
        "experiment_id": row.experiment_id,
        "idea_id": row.idea_id,
        "idea_version": row.idea_version,
        "role": str(row.role),
    }
    for key, value in expected.items():
        if document.get(key) != value:
            raise ProvenanceError(
                f"receipt {row.receipt_id} says {key}={document.get(key)!r}, and "
                f"its row says {value!r}"
            )
    checks = (
        ("delivered_digest", digest(document.get("delivered")), row.delivered_digest),
        ("inputs_digest", digest(document.get("inputs")), row.inputs_digest),
        ("outputs_digest", digest(document.get("outputs")), row.outputs_digest),
        (
            "command_digest",
            digest((document.get("capability") or {}).get("declaration")),
            row.command_digest,
        ),
        ("manifest", manifest.get("artifact_id"), row.manifest_artifact_id),
        ("manifest digest", manifest.get("digest"), row.manifest_digest),
        (
            "parent",
            (document.get("parent") or {}).get("receipt_id"),
            row.parent_receipt_id,
        ),
    )
    for what, found, wanted in checks:
        if found != wanted:
            raise ProvenanceError(
                f"receipt {row.receipt_id}'s {what} does not match its row"
            )
    return document


# ------------------------------------------------------------- assessing --
@dataclass(frozen=True, slots=True)
class ReplicationFinding:
    """What a replication's trusted chain establishes, the findings apart."""

    configuration_independent: bool
    perturbation_attested: bool
    basis: str
    varied: tuple[str, ...] = ()
    attested: tuple[str, ...] = ()
    unattested: tuple[str, ...] = ()
    not_delivered: tuple[str, ...] = ()
    chain: dict[str, Any] = field(default_factory=dict)

    @property
    def independent(self) -> bool:
        """Configuration independence *and* an attested perturbation."""

        return self.configuration_independent and self.perturbation_attested

    def record(self) -> dict[str, Any]:
        return {
            "configuration_independent": self.configuration_independent,
            "perturbation_validity": (
                "ATTESTED_BY_RESEARCHER" if self.perturbation_attested else "UNATTESTED"
            ),
            "counts_as_independent_replication": self.independent,
            "varied": list(self.varied),
            "attested": list(self.attested),
            "unattested": list(self.unattested),
            "not_delivered": list(self.not_delivered),
            "basis": self.basis,
            "what_is_proved": (
                "a separate execution, bound to a manifest frozen before it ran, "
                "was delivered a configuration that differs from its parent's in "
                "what the manifest varies -- observed by the Research OS runner"
            ),
            "what_is_not_proved": (
                "that the computation used the varied values; that is the "
                "researcher's attestation where one is recorded, and nothing "
                "where it is not"
            ),
        }


def _chain(
    store: Any, artifacts: Any, experiment: IdeaExperiment, receipt: ExecutionReceipt
) -> tuple[
    dict[str, Any], dict[str, Any], dict[str, Any], ExecutionReceipt, IdeaExperiment
]:
    """The replication's receipt, manifest, parent receipt and parent -- all bound."""

    mine = verified_receipt(artifacts, receipt)
    if receipt.role is not ExperimentRole.REPLICATION:
        raise ProvenanceError(f"receipt {receipt.receipt_id} is not a replication's")
    if receipt.experiment_id != experiment.experiment_id:
        raise ProvenanceError(
            f"receipt {receipt.receipt_id} is of {receipt.experiment_id}, not of "
            f"{experiment.experiment_id}"
        )
    if experiment.execution_manifest_artifact_id != receipt.manifest_artifact_id:
        raise ProvenanceError(
            "the experiment's manifest pointer is not the manifest its execution "
            "was bound to"
        )
    manifest = verified_json(artifacts, receipt.manifest_artifact_id)
    if receipt.manifest_digest != receipt.manifest_artifact_id:
        raise ProvenanceError("the receipt's manifest digest is not its address")
    execution = manifest.get("execution") or {}
    if (
        manifest.get("schema") != MANIFEST_SCHEMA
        or manifest.get("experiment_id") != experiment.experiment_id
        or execution.get("job_id") != receipt.job_id
        or execution.get("spec_digest") != receipt.spec_digest
    ):
        raise ProvenanceError(
            "the frozen manifest does not name this execution of this replication"
        )
    parent_link = manifest.get("parent") or {}
    if parent_link.get("receipt_id") != receipt.parent_receipt_id:
        raise ProvenanceError("the manifest and the receipt name different parents")
    parent_receipt = (
        store.get_receipt(receipt.parent_receipt_id)
        if receipt.parent_receipt_id
        else None
    )
    if parent_receipt is None:
        raise ProvenanceError("the parent execution has no trusted receipt")
    theirs = verified_receipt(artifacts, parent_receipt)
    primary = store.get_experiment(
        idea_id=experiment.idea_id,
        idea_version=experiment.idea_version,
        role=ExperimentRole.PRIMARY,
    )
    if primary is None:
        raise ProvenanceError("the idea version has no primary experiment")
    if (
        parent_receipt.role is not ExperimentRole.PRIMARY
        or parent_receipt.experiment_id != primary.experiment_id
        or parent_link.get("experiment_id") != primary.experiment_id
        or primary.job_id != parent_receipt.job_id
    ):
        raise ProvenanceError(
            "the parent receipt is not the primary's execution of this idea version"
        )
    if (
        primary.state is not ExperimentState.INTERPRETED
        or primary.evidence_id != parent_link.get("evidence_id")
        or primary.analysis_artifact_id != parent_link.get("analysis_artifact_id")
        or not primary.evidence_id
    ):
        raise ProvenanceError(
            "the parent result the manifest froze is not the primary's result now"
        )
    if parent_receipt.job_id == receipt.job_id:
        raise ProvenanceError("the replication and its parent are one execution")
    return mine, manifest, theirs, parent_receipt, primary


def assess_replication(
    store: Any, artifacts: Any, experiment: IdeaExperiment, receipt: ExecutionReceipt
) -> ReplicationFinding:
    """Establish what a replication's trusted chain shows, and only that."""

    try:
        mine, manifest, theirs, parent_receipt, primary = _chain(
            store, artifacts, experiment, receipt
        )
    except ProvenanceError as exc:
        return ReplicationFinding(
            configuration_independent=False,
            perturbation_attested=False,
            basis=f"the trusted provenance chain does not hold: {exc}",
        )
    variables = dict(manifest.get("independence_variables") or {})
    delivered, parent_delivered = mine["delivered"], theirs["delivered"]
    inputs, parent_inputs = dict(mine["inputs"]), dict(theirs["inputs"])
    checks: list[tuple[str, bool]] = []
    if "seeds" in variables:
        wanted = list((variables["seeds"] or {}).get("replication") or ())
        checks.append(
            (
                "seeds",
                delivered["seeds"] == wanted
                and delivered["seeds"] != parent_delivered["seeds"],
            )
        )
    for name in sorted(dict(variables.get("parameters") or {})):
        checks.append(
            (
                f"parameter {name}",
                delivered["argv"] != parent_delivered["argv"]
                or inputs != parent_inputs,
            )
        )
    for path, values in sorted(dict(variables.get("inputs") or {}).items()):
        checks.append(
            (
                f"input {path}",
                inputs.get(path) == (values or {}).get("replication")
                and inputs.get(path) != parent_inputs.get(path),
            )
        )
    if "command" in variables:
        checks.append(
            (
                "command",
                mine["capability"]["command"] != theirs["capability"]["command"],
            )
        )
    varied = tuple(item for item, _ in checks)
    not_delivered = tuple(item for item, ok in checks if not ok)
    configuration_independent = bool(varied) and not not_delivered
    attestation = set(
        (manifest.get("capability") or {}).get("perturbation_attestation") or ()
    )
    generated = {
        str(item.get("path")): str(item.get("parameter"))
        for item in manifest.get("generated_inputs") or ()
    }

    def is_attested(item: str) -> bool:
        if item == "seeds":
            return "seeds" in attestation
        if item == "command":
            return "implementation" in attestation
        kind, _, name = item.partition(" ")
        if kind == "parameter":
            return name in attestation
        return generated.get(name) in attestation

    attested = tuple(item for item in varied if is_attested(item))
    unattested = tuple(item for item in varied if not is_attested(item))
    chain = {
        "receipt_id": receipt.receipt_id,
        "parent_receipt_id": parent_receipt.receipt_id,
        "parent_experiment_id": primary.experiment_id,
        "parent_evidence_id": primary.evidence_id,
        "parent_analysis_artifact_id": primary.analysis_artifact_id,
        "manifest_artifact_id": receipt.manifest_artifact_id,
        "manifest_digest": receipt.manifest_digest,
    }
    if not varied:
        basis = (
            "the frozen manifest records no seed, parameter, composed input or "
            "command that differs from the primary's specification"
        )
    elif not_delivered:
        basis = (
            f"the runner's receipts do not show the replication delivered a "
            f"different {', '.join(not_delivered)} from its parent's execution"
        )
    elif not attested:
        basis = (
            f"a separate execution was delivered a different "
            f"{', '.join(varied)} (proved by the runner's receipts), and no "
            f"declaration attests that the computation uses any of it. "
            f"Research OS cannot establish that arbitrary code used a delivered "
            f"value, so this is not a valid perturbation on record"
        )
    else:
        basis = (
            f"a separate execution was delivered a different {', '.join(varied)} "
            f"(proved by the runner's receipts); the researcher's declaration "
            f"attests the computation uses {', '.join(attested)} (attested, not "
            f"proved)"
        )
    return ReplicationFinding(
        configuration_independent=configuration_independent,
        perturbation_attested=configuration_independent and bool(attested),
        basis=basis,
        varied=varied,
        attested=attested,
        unattested=unattested,
        not_delivered=not_delivered,
        chain=chain,
    )


# ------------------------------------------------------------ for the gate --
def replication_provenance(
    store: Any,
    artifacts: Any,
    *,
    idea_id: str,
    idea_version: int,
    evidence: Sequence[IdeaEvidence],
) -> tuple[ReplicationProvenance, ...]:
    """Every replication assessment of a version, its chain re-verified now.

    Read by the readiness gate (INV-08): presence of an assessment is not
    enough. For each one this re-hashes the receipts and the manifest, and
    checks that the evidence row, both receipts, the manifest, the current
    replication experiment and the current primary still name one another
    exactly as they did when the reading was recorded. Anything that moved --
    a repointed manifest, a parent result that is no longer the primary's, a
    receipt of another job, an artifact whose bytes changed -- is a
    ``problem`` and the chain is not intact.
    """

    by_id = {item.evidence_id: item for item in evidence}
    replication = store.get_experiment(
        idea_id=idea_id, idea_version=idea_version, role=ExperimentRole.REPLICATION
    )
    records: list[ReplicationProvenance] = []
    for assessment in store.replication_assessments(
        idea_id=idea_id, idea_version=idea_version
    ):
        problems: list[str] = []
        if assessment.legacy:
            problems.append("legacy evidence recorded before trusted receipts")
        else:
            row = by_id.get(assessment.evidence_id)
            receipt = (
                store.get_receipt(assessment.receipt_id)
                if assessment.receipt_id
                else None
            )
            if row is None or row.kind is not EvidenceKind.REPLICATION:
                problems.append("the assessment names no replication evidence row")
            if receipt is None:
                problems.append("the assessment's receipt is missing")
            if (
                replication is None
                or replication.experiment_id != assessment.experiment_id
            ):
                problems.append("the assessment is not of this version's replication")
            if (
                not problems
                and row is not None
                and receipt is not None
                and replication is not None
            ):
                if row.job_id != receipt.job_id:
                    problems.append("the evidence names another execution")
                if row.artifact_id != assessment.analysis_artifact_id:
                    problems.append("the evidence names another analysis")
                if (
                    replication.evidence_id != assessment.evidence_id
                    or replication.job_id != receipt.job_id
                    or replication.analysis_artifact_id
                    != assessment.analysis_artifact_id
                ):
                    problems.append(
                        "the replication experiment no longer names this reading"
                    )
                if receipt.receipt_id == assessment.parent_receipt_id:
                    problems.append("the replication is its own parent")
                try:
                    _mine, _manifest, _theirs, parent_receipt, primary = _chain(
                        store, artifacts, replication, receipt
                    )
                    if (
                        parent_receipt.receipt_id != assessment.parent_receipt_id
                        or primary.experiment_id != assessment.parent_experiment_id
                        or primary.evidence_id != assessment.parent_evidence_id
                        or primary.analysis_artifact_id
                        != assessment.parent_analysis_artifact_id
                        or receipt.manifest_artifact_id
                        != assessment.manifest_artifact_id
                        or receipt.manifest_digest != assessment.manifest_digest
                    ):
                        problems.append(
                            "the chain the assessment recorded is not the chain now"
                        )
                except ProvenanceError as exc:
                    problems.append(str(exc))
                for label, artifact_id in (
                    ("the replication's analysis", assessment.analysis_artifact_id),
                    ("the parent's analysis", assessment.parent_analysis_artifact_id),
                ):
                    try:
                        verified_json(artifacts, artifact_id)
                    except ProvenanceError as exc:
                        problems.append(f"{label}: {exc}")
        records.append(
            ReplicationProvenance(
                evidence_id=assessment.evidence_id,
                assessment_id=assessment.assessment_id,
                legacy=assessment.legacy,
                configuration_independent=assessment.configuration_independent,
                perturbation_attested=assessment.perturbation_attested,
                chain_intact=not problems,
                problems=tuple(problems),
            )
        )
    return tuple(records)


def workspace_outputs_match(
    receipt_document: Mapping[str, Any], outputs: Sequence[tuple[str, str, int]]
) -> list[str]:
    """Outputs a reading hashed that differ from what the runner hashed at exit."""

    recorded = {
        str(path): (str(sha), int(size))
        for path, sha, size in receipt_document.get("outputs") or ()
    }
    return [
        str(path)
        for path, sha, size in outputs
        if recorded.get(str(path)) != (str(sha), int(size))
    ]


__all__ = [
    "MANIFEST_SCHEMA",
    "RECEIPT_SCHEMA",
    "ProvenanceError",
    "ReplicationFinding",
    "assess_replication",
    "canonical",
    "command_identity",
    "delivered_configuration",
    "digest",
    "replication_provenance",
    "verified_json",
    "verified_receipt",
    "workspace_outputs_match",
    "write_receipt",
]
