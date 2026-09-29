"""Typed scientific capabilities: what a science repository can execute and observe.

``docs/SCIENCE_EXECUTION.md`` is the specification. This module is the
generic half of it and knows nothing about any science: no solver, no metric,
no file name belongs here. What a *particular* repository can do is declared
by its researcher, in the repository itself, in :data:`MANIFEST_PATH`, and
read by Research OS from a **committed** tree -- never from a worktree a
command or a model has touched.

Two keys, held by two parties, and both are needed before anything runs:

```text
experiments.yaml (host, outside every worktree)   WHAT MAY RUN: the program,
                                                  its argv, its parameters,
                                                  its time ceiling
research-capabilities.yaml (science repository,   WHAT IT MEASURES: the result
committed at the pinned code commit)              artifact and its schema, the
                                                  typed observables, inputs,
                                                  determinism, the perturbations
                                                  the researcher attests
```

A capability names the host command that executes it, so the host operator's
authority over *what executes* is unchanged; the capability adds what the
execution produces, in types a machine can check. The consequence this module
exists for:

    a model cannot declare that an observable exists.

A frozen analysis says which observables it reads, which fields of them, and
which of those it treats as numbers. :func:`resolve` compares that, by
ordinary code, with what a declared capability produces, and answers either
``EXECUTABLE`` with an exact :class:`Binding` or ``CAPABILITY_LIMITED`` with
the exact requirements nothing meets. There is no third answer and no model
in between.

What this does **not** establish: that a program writes what its declaration
says it writes. That is checked after execution, against the declared result
schema, before a result is accepted (``portfolio.sciencechain``); a program
whose output does not fit is ``INVALID_EVIDENCE``, never a finding.
"""

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal, Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from research_os.errors import ResearchOSError

#: Where a science repository declares its capabilities, relative to its root.
#: Outside ``.research/`` (the capsule, whose layout ``docs/CAPSULE.md`` owns)
#: and outside ``.research-os/`` (which Research OS itself writes).
MANIFEST_PATH = "research-capabilities.yaml"
MANIFEST_SCHEMA = "research-os-capabilities-v1"
CAPABILITY_DIGEST_VERSION = "rcap-v1"

MAX_MANIFEST_BYTES = 256 * 1024
MAX_CAPABILITIES = 32
MAX_OBSERVABLES = 16
MAX_FIELDS = 64
MAX_INPUTS = 32
MAX_PERTURBATIONS = 16
MAX_TEXT = 2_000

#: The same field grammar a frozen analysis uses for a record field, so a
#: declared field and a field an analysis reads are compared as strings.
FIELD_PATTERN = r"^[A-Za-z_][A-Za-z0-9_\-]*(\.[A-Za-z0-9_\-]+)*$"
NAME_PATTERN = r"^[a-z][a-z0-9_]{0,47}$"
CAPABILITY_ID_PATTERN = r"^[a-z][a-z0-9]*([.-][a-z0-9]+)*$"
COMMAND_PATTERN = r"^[a-z][a-z0-9-]{0,63}$"
PARAMETER_PATTERN = r"^[a-z][a-z0-9_]{0,31}$"


class CapabilityError(ResearchOSError):
    """A capability declaration that cannot be read or does not validate."""


class CapabilityStatus(StrEnum):
    """The two answers capability resolution can give. There is no third."""

    EXECUTABLE = "EXECUTABLE"
    CAPABILITY_LIMITED = "CAPABILITY_LIMITED"


class FieldType(StrEnum):
    NUMBER = "number"
    INTEGER = "integer"
    STRING = "string"
    BOOLEAN = "boolean"


NUMERIC_TYPES: frozenset[FieldType] = frozenset({FieldType.NUMBER, FieldType.INTEGER})

#: JSON-schema ``type`` for each declared field type, so a declared observable
#: and the declared result schema can be checked against each other.
_SCHEMA_TYPE: dict[FieldType, str] = {
    FieldType.NUMBER: "number",
    FieldType.INTEGER: "integer",
    FieldType.STRING: "string",
    FieldType.BOOLEAN: "boolean",
}


class _Declared(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


def _text(value: str, what: str, *, limit: int = MAX_TEXT) -> str:
    stripped = value.strip()
    if len(stripped) > limit:
        raise ValueError(f"{what} is at most {limit} characters")
    return stripped


def _relative(value: str, what: str) -> str:
    stripped = value.strip()
    if not stripped or stripped.startswith(("/", "~")) or "\\" in stripped:
        raise ValueError(f"{what} must be a relative POSIX path: {value!r}")
    if any(part in {"", ".", ".."} for part in stripped.split("/")):
        raise ValueError(f"{what} must not contain empty, '.' or '..' segments")
    if any(ch in stripped for ch in "\n\r\t\x00"):
        raise ValueError(f"{what} must not contain control characters")
    return stripped


class ObservableField(_Declared):
    """One typed field every record of a ``records`` observable carries."""

    name: str = Field(pattern=FIELD_PATTERN, max_length=128)
    type: FieldType
    unit: str = Field(default="", max_length=32)
    #: ``False`` for a field whose value varies between identical executions
    #: -- a wall-clock time, a host name. Recorded in every binding, so a
    #: reader of a result knows which quantities are measurements of the
    #: machine as much as of the science.
    deterministic: bool = True
    description: str = ""


class CapabilityObservable(_Declared):
    """One quantity the result artifact provides, at a dotted path in it."""

    name: str = Field(pattern=NAME_PATTERN)
    kind: Literal["scalar", "records"]
    path: str = Field(default="", max_length=256)
    #: ``scalar`` only.
    type: FieldType | None = None
    unit: str = Field(default="", max_length=32)
    deterministic: bool = True
    #: ``records`` only: every field a record carries.
    fields: tuple[ObservableField, ...] = ()
    description: str = ""

    @model_validator(mode="after")
    def _shape(self) -> Self:
        path = self.path.strip()
        if path and any(not part for part in path.split(".")):
            raise ValueError(f"observable {self.name!r}: empty segment in its path")
        if self.kind == "scalar":
            if self.type is None or self.fields:
                raise ValueError(
                    f"observable {self.name!r} is a scalar: it declares a `type` "
                    f"and no `fields`"
                )
            if not path:
                raise ValueError(f"scalar observable {self.name!r} needs a path")
        else:
            if self.type is not None or not self.fields:
                raise ValueError(
                    f"observable {self.name!r} is records: it declares `fields` "
                    f"and no `type`"
                )
            if len(self.fields) > MAX_FIELDS:
                raise ValueError(
                    f"observable {self.name!r}: at most {MAX_FIELDS} fields"
                )
            names = [item.name for item in self.fields]
            if len(set(names)) != len(names):
                raise ValueError(f"observable {self.name!r}: a field is declared twice")
        return self

    def field(self, name: str) -> ObservableField | None:
        for item in self.fields:
            if item.name == name:
                return item
        return None


class ResultArtifact(_Declared):
    """The one file an execution's result is read from, and its schema.

    Either a literal repository-relative path the command declares as an
    output, or the name of the command's ``path`` parameter whose value names
    it. The schema is the subset ``experiment.generated`` honours -- the same
    closed-object checker a composed input faces -- and it is **required**:
    an artifact whose shape nothing states cannot be validated before it is
    accepted, and validating before accepting is the point.
    """

    artifact: str = ""
    artifact_parameter: str = ""
    format: Literal["json"] = "json"
    json_schema: dict[str, Any] = Field(alias="schema")

    @model_validator(mode="after")
    def _one_location(self) -> Self:
        if bool(self.artifact) == bool(self.artifact_parameter):
            raise ValueError(
                "a result names exactly one of `artifact` (a path) or "
                "`artifact_parameter` (the path parameter that names it)"
            )
        if self.artifact:
            _relative(self.artifact, "the result artifact")
        elif re.fullmatch(PARAMETER_PATTERN, self.artifact_parameter) is None:
            raise ValueError("`artifact_parameter` must name a parameter")
        if not self.json_schema:
            raise ValueError("a result declares the schema it is validated against")
        from research_os.errors import ExperimentSpecError
        from research_os.experiment.generated import assert_schema_supported

        try:
            assert_schema_supported(self.json_schema, where="the result schema")
        except ExperimentSpecError as exc:
            raise ValueError(str(exc)) from None
        return self


class InputArtifact(_Declared):
    """An immutable input: a file tracked at the pinned code commit, hashed there."""

    path: str
    description: str = ""

    @model_validator(mode="after")
    def _path(self) -> Self:
        _relative(self.path, "an input artifact")
        return self


class Perturbation(_Declared):
    """One input the researcher attests the computation genuinely uses.

    The attestation vocabulary is the one ``experiments.yaml`` already has
    (INV-07): ``seeds`` -- the design seeds, delivered as
    ``RESEARCH_OS_SEED_<n>``; a declared parameter; or ``implementation``.
    The description is required, because it *is* the attestation: the
    researcher's statement of how the varied value reaches the computation.
    Research OS records it as attested and never as proved.
    """

    kind: Literal["seeds", "parameter", "implementation"]
    name: str = ""
    description: str

    @model_validator(mode="after")
    def _named(self) -> Self:
        if self.kind == "parameter":
            if re.fullmatch(PARAMETER_PATTERN, self.name) is None:
                raise ValueError("a parameter perturbation names the parameter")
        elif self.name:
            raise ValueError(f"a {self.kind} perturbation takes no name")
        if not self.description.strip():
            raise ValueError(
                "a perturbation says how the computation uses the varied input; "
                "that sentence is the attestation"
            )
        return self

    @property
    def token(self) -> str:
        return self.name if self.kind == "parameter" else self.kind


class Replication(_Declared):
    perturbations: tuple[Perturbation, ...] = ()
    #: How a replication's reading is compared with its primary's, fixed here
    #: before either exists: the two system-computed outcomes must be the
    #: same state. The only rule v1 has; a field, so the record names it.
    comparison: Literal["same_outcome"] = "same_outcome"

    @model_validator(mode="after")
    def _distinct(self) -> Self:
        tokens = [item.token for item in self.perturbations]
        if len(tokens) > MAX_PERTURBATIONS or len(set(tokens)) != len(tokens):
            raise ValueError("perturbations are distinct and at most 16")
        return self


class Resources(_Declared):
    #: The longest one execution needs. A host whose ceiling is shorter cannot
    #: run it, and resolution says so rather than starting a run that is
    #: certain to be killed.
    timeout_seconds: int = Field(ge=1, le=86_400)
    cpus: int = Field(default=1, ge=1, le=256)
    memory_mb: int | None = Field(default=None, ge=1, le=4_194_304)


class ParameterNote(_Declared):
    """The capability's words about one of its command's parameters."""

    name: str = Field(pattern=PARAMETER_PATTERN)
    description: str = ""
    unit: str = Field(default="", max_length=32)


class CampaignAggregation(_Declared):
    """How one observable's values from several executions become one value.

    ``concatenate`` for records -- the campaign's records are every unit's
    records, in unit order -- and ``sum``, ``min`` or ``max`` for a numeric
    scalar. ``identity`` names the record fields that identify one
    observation: two units of one campaign that produce a record with the
    same identity have produced the same observation twice, and the
    campaign's result is refused rather than counted twice.
    """

    observable: str = Field(pattern=NAME_PATTERN)
    rule: Literal["concatenate", "sum", "min", "max"]
    identity: tuple[str, ...] = ()


class CampaignSupport(_Declared):
    """That several executions of this capability may form one measurement, and how.

    The researcher's statement, like the rest of the declaration. It says
    which attested perturbations may differ between the units of one
    campaign -- a unit that differs from another in nothing the computation
    is attested to use would be the same observation again, and Research OS
    refuses such a campaign rather than let it pad a sample -- how each
    observable combines across units, and how many units one campaign may
    have. A capability without it is run one execution per measurement.
    """

    max_units: int = Field(ge=2, le=64)
    unit_varies: tuple[str, ...] = Field(min_length=1)
    aggregation: tuple[CampaignAggregation, ...] = Field(min_length=1)

    def rule_for(self, observable: str) -> CampaignAggregation | None:
        for item in self.aggregation:
            if item.observable == observable:
                return item
        return None


class Capability(_Declared):
    id: str = Field(pattern=CAPABILITY_ID_PATTERN, max_length=64)
    version: int = Field(ge=1, le=1_000_000)
    title: str = Field(min_length=1, max_length=200)
    description: str = ""
    command: str = Field(pattern=COMMAND_PATTERN)
    parameters: tuple[ParameterNote, ...] = ()
    inputs: tuple[InputArtifact, ...] = ()
    result: ResultArtifact
    observables: tuple[CapabilityObservable, ...] = Field(min_length=1)
    determinism: Literal["deterministic", "seeded", "nondeterministic"]
    determinism_notes: str = ""
    replication: Replication = Replication()
    resources: Resources
    #: Whether, and how, several executions form one measurement
    #: (`research_os.portfolio.campaign`). Absent: one execution each.
    campaign: CampaignSupport | None = None

    @model_validator(mode="after")
    def _coherent(self) -> Self:
        _text(self.description, "a description")
        _text(self.determinism_notes, "determinism notes")
        if len(self.observables) > MAX_OBSERVABLES:
            raise ValueError(f"at most {MAX_OBSERVABLES} observables")
        if len(self.inputs) > MAX_INPUTS:
            raise ValueError(f"at most {MAX_INPUTS} input artifacts")
        names = [item.name for item in self.observables]
        if len(set(names)) != len(names):
            raise ValueError("observable names are distinct")
        parameters = [item.name for item in self.parameters]
        if len(set(parameters)) != len(parameters):
            raise ValueError("a parameter is described twice")
        paths = [item.path for item in self.inputs]
        if len(set(paths)) != len(paths):
            raise ValueError("an input artifact is listed twice")
        _observables_fit_schema(self)
        if self.campaign is not None:
            _campaign_coherent(self, self.campaign)
        return self

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    def observable(self, name: str) -> CapabilityObservable | None:
        for item in self.observables:
            if item.name == name:
                return item
        return None


class CapabilityManifest(_Declared):
    schema_name: Literal["research-os-capabilities-v1"] = Field(alias="schema")
    capabilities: tuple[Capability, ...] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique(self) -> Self:
        if len(self.capabilities) > MAX_CAPABILITIES:
            raise ValueError(f"at most {MAX_CAPABILITIES} capabilities")
        ids = [item.id for item in self.capabilities]
        if len(set(ids)) != len(ids):
            raise ValueError(
                "a capability id is declared twice; one commit declares one "
                "version of each capability"
            )
        commands = [item.command for item in self.capabilities]
        if len(set(commands)) != len(commands):
            raise ValueError(
                "two capabilities name one command; a command backs at most one "
                "capability, so which one an execution realises is never a choice"
            )
        return self

    def by_command(self, command: str) -> Capability | None:
        for item in self.capabilities:
            if item.command == command:
                return item
        return None

    def by_id(self, capability_id: str) -> Capability | None:
        for item in self.capabilities:
            if item.id == capability_id:
                return item
        return None


def _schema_node(schema: Mapping[str, Any], path: str) -> Mapping[str, Any] | None:
    """The sub-schema at a dotted path through ``properties``, or ``None``."""

    node: Mapping[str, Any] = schema
    for part in [item for item in path.split(".") if item]:
        if node.get("type") != "object":
            return None
        child = (node.get("properties") or {}).get(part)
        if not isinstance(child, Mapping):
            return None
        node = child
    return node


def _observables_fit_schema(capability: Capability) -> None:
    """Every declared observable is where the result schema says it is.

    Two statements about one file, both the researcher's, checked against each
    other once, when the declaration is read: an observable whose path the
    schema does not have, or whose declared type the schema contradicts, is a
    declaration that could never be satisfied, and discovering that after an
    execution is discovering it too late.
    """

    schema = capability.result.json_schema
    for item in capability.observables:
        node = _schema_node(schema, item.path)
        if node is None:
            raise ValueError(
                f"observable {item.name!r}: the result schema has nothing at "
                f"{item.path or '(the top level)'!r}"
            )
        if item.kind == "scalar":
            assert item.type is not None
            if not _schema_admits(node, item.type):
                raise ValueError(
                    f"observable {item.name!r} is declared {item.type} and the "
                    f"result schema says {node.get('type')!r} at {item.path!r}"
                )
            continue
        if node.get("type") != "array" or not isinstance(node.get("items"), Mapping):
            raise ValueError(
                f"records observable {item.name!r}: the result schema has no "
                f"array of records at {item.path or '(the top level)'!r}"
            )
        record = node["items"]
        for entry in item.fields:
            sub = _schema_node(record, entry.name)
            if sub is None:
                raise ValueError(
                    f"observable {item.name!r} field {entry.name!r} is not in "
                    f"the result schema's record"
                )
            if not _schema_admits(sub, entry.type):
                raise ValueError(
                    f"observable {item.name!r} field {entry.name!r} is declared "
                    f"{entry.type} and the result schema says {sub.get('type')!r}"
                )


def _campaign_coherent(capability: Capability, campaign: CampaignSupport) -> None:
    """A campaign declaration that could be honoured, checked once when read.

    What units may differ in must be perturbations the same declaration
    attests the computation uses, and never ``implementation``: every unit
    of a campaign runs one command. Every aggregation names a declared
    observable, once; records concatenate and numeric scalars sum, take a
    minimum or a maximum; an identity names declared record fields.
    """

    attested = {item.token for item in capability.replication.perturbations}
    for token in campaign.unit_varies:
        if token == "implementation":
            raise ValueError(
                "the units of one campaign run one command; they cannot differ in "
                "implementation"
            )
        if token not in attested:
            raise ValueError(
                f"campaign units may differ in {token!r}, which this capability "
                f"does not attest its computation uses (attested: "
                f"{sorted(attested) or 'nothing'}); units differing in it would "
                f"not be different observations"
            )
    if len(set(campaign.unit_varies)) != len(campaign.unit_varies):
        raise ValueError("campaign.unit_varies names a perturbation twice")
    seen: set[str] = set()
    for item in campaign.aggregation:
        if item.observable in seen:
            raise ValueError(f"observable {item.observable!r} is aggregated twice")
        seen.add(item.observable)
        observable = capability.observable(item.observable)
        if observable is None:
            raise ValueError(
                f"campaign aggregation names {item.observable!r}, which is not a "
                f"declared observable"
            )
        if observable.kind == "records":
            if item.rule != "concatenate":
                raise ValueError(
                    f"records observable {item.observable!r} combines by "
                    f"concatenation, not {item.rule!r}"
                )
            for name in item.identity:
                if observable.field(name) is None:
                    raise ValueError(
                        f"the identity of {item.observable!r} names field "
                        f"{name!r}, which it does not declare"
                    )
            continue
        if item.identity:
            raise ValueError(f"scalar {item.observable!r} has no record identity")
        if item.rule == "concatenate" or observable.type not in NUMERIC_TYPES:
            raise ValueError(
                f"scalar {item.observable!r} combines by sum, min or max, and only "
                f"when it is a number"
            )


def _schema_admits(node: Mapping[str, Any], declared: FieldType) -> bool:
    found = node.get("type")
    if found is None:
        return False
    if declared is FieldType.NUMBER:
        return found in {"number", "integer"}
    return found == _SCHEMA_TYPE[declared]


# ------------------------------------------------------------ loading --
def parse_manifest(raw: bytes, *, where: str = MANIFEST_PATH) -> CapabilityManifest:
    """Parse and validate one manifest's bytes. Strict, as canonical YAML is."""

    from research_os.capsule import DuplicateYamlKeyError, UniqueKeySafeLoader

    if len(raw) > MAX_MANIFEST_BYTES:
        raise CapabilityError(
            f"{where} is {len(raw)} bytes; a capability manifest is at most "
            f"{MAX_MANIFEST_BYTES}"
        )
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise CapabilityError(f"{where} is not UTF-8: {exc}") from None
    try:
        data = yaml.load(text, Loader=UniqueKeySafeLoader)
    except DuplicateYamlKeyError as exc:
        raise CapabilityError(f"{where}: duplicate key {exc.key!r}") from None
    except yaml.YAMLError as exc:
        raise CapabilityError(f"{where} is not valid YAML: {exc}") from None
    if not isinstance(data, dict):
        raise CapabilityError(f"{where} must hold one mapping")
    try:
        return CapabilityManifest.model_validate(data)
    except ValidationError as exc:
        raise CapabilityError(f"{where} does not validate: {exc}") from None


@dataclass(frozen=True, slots=True)
class LoadedManifest:
    """A manifest as read from one commit: the bytes are what is bound."""

    manifest: CapabilityManifest
    raw: bytes
    sha256: str
    commit: str
    path: str = MANIFEST_PATH


def committed_bytes(repository: Path, *, commit: str, path: str) -> bytes | None:
    """One tracked file's bytes at ``commit``, or ``None`` when it is not there."""

    from research_os.automation.gitutil import git_bytes

    return git_bytes(["cat-file", "blob", f"{commit}:{path}"], cwd=repository)


def load_committed(
    repository: Path, *, commit: str | None = None
) -> LoadedManifest | None:
    """The manifest committed at ``commit`` (default ``HEAD``); ``None`` if absent.

    Read from Git's object store, so what is bound is what was committed at
    that commit -- not a working tree, not a worktree a command ran in.
    Raises :class:`CapabilityError` for a manifest that is present and
    invalid: that is a declaration the researcher got wrong, and the caller
    says so as ``CAPABILITY_LIMITED`` rather than treating it as absent.
    """

    from research_os.automation.gitutil import has_commits, head_commit

    if not has_commits(repository):
        return None
    pinned = commit or head_commit(repository)
    raw = committed_bytes(repository, commit=pinned, path=MANIFEST_PATH)
    if raw is None:
        return None
    return LoadedManifest(
        manifest=parse_manifest(raw, where=f"{MANIFEST_PATH}@{pinned[:12]}"),
        raw=raw,
        sha256=hashlib.sha256(raw).hexdigest(),
        commit=pinned,
    )


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


def capability_digest(capability: Capability) -> str:
    """One capability's declaration, by content. Wording included: it is short."""

    payload = capability.model_dump(mode="json", by_alias=True)
    # Absent is omitted rather than hashed as `null`, so no declaration
    # written before campaigns existed changes its digest -- and no plan that
    # pinned one stops verifying.
    if payload.get("campaign") is None:
        payload.pop("campaign", None)
    return (
        f"{CAPABILITY_DIGEST_VERSION}:{hashlib.sha256(canonical(payload)).hexdigest()}"
    )


# ---------------------------------------------------- cross-validation --
@dataclass(frozen=True, slots=True)
class Unmet:
    """One requirement nothing declared meets, said exactly."""

    requirement: str
    detail: str
    capability: str | None = None

    def record(self) -> dict[str, Any]:
        return {
            "capability": self.capability,
            "requirement": self.requirement,
            "detail": self.detail,
        }

    def rendered(self) -> str:
        where = f"[{self.capability}] " if self.capability else ""
        return f"{where}{self.requirement}: {self.detail}"


def check_against_command(
    capability: Capability, commands: Mapping[str, Any]
) -> list[Unmet]:
    """What makes this declaration unusable with the host's declared commands.

    The capability is the science repository's statement; the command is the
    host operator's. They must describe one program: the command exists, the
    capability describes exactly the parameters the command declares, the
    result artifact is something the command writes (a declared output, or
    the value of one of its ``path`` parameters) and every attested
    perturbation names something the command takes.
    """

    ref = capability.ref
    command = commands.get(capability.command)
    if command is None:
        return [
            Unmet(
                "host command",
                f"names command {capability.command!r}, which experiments.yaml "
                f"does not declare for this project; the host operator decides "
                f"what may run",
                ref,
            )
        ]
    unmet: list[Unmet] = []
    declared = {item.name: item for item in getattr(command, "parameters", ())}
    described = {item.name for item in capability.parameters}
    if described and described != set(declared):
        unmet.append(
            Unmet(
                "parameters",
                f"describes {sorted(described)} and command {capability.command!r} "
                f"declares {sorted(declared)}",
                ref,
            )
        )
    result = capability.result
    if result.artifact:
        if result.artifact not in set(getattr(command, "outputs", ())):
            unmet.append(
                Unmet(
                    "result artifact",
                    f"{result.artifact!r} is not a declared output of "
                    f"{capability.command!r}, so the runner would not hash it "
                    f"into the execution receipt",
                    ref,
                )
            )
    else:
        parameter = declared.get(result.artifact_parameter)
        if parameter is None or str(parameter.type) != "path":
            unmet.append(
                Unmet(
                    "result artifact",
                    f"`artifact_parameter` {result.artifact_parameter!r} is not a "
                    f"path parameter of {capability.command!r}",
                    ref,
                )
            )
    for item in capability.replication.perturbations:
        if item.kind == "parameter" and item.name not in declared:
            unmet.append(
                Unmet(
                    "perturbation",
                    f"attests parameter {item.name!r}, which {capability.command!r} "
                    f"does not take",
                    ref,
                )
            )
    ceiling = int(getattr(command, "timeout_seconds", 0) or 0)
    if ceiling and capability.resources.timeout_seconds > ceiling:
        unmet.append(
            Unmet(
                "resources",
                f"needs {capability.resources.timeout_seconds}s and the host "
                f"declares {capability.command!r} with a {ceiling}s ceiling",
                ref,
            )
        )
    return unmet


# ----------------------------------------------------------- resolution --
@dataclass(frozen=True, slots=True)
class ObservableNeed:
    """One observable a frozen analysis reads, as a requirement."""

    name: str
    source: str
    kind: str
    path: str
    #: Every record field read anywhere: required fields, inclusion rules,
    #: reduction fields, regression terms.
    fields: tuple[str, ...] = ()
    #: The subset read as numbers.
    numeric: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class Requirements:
    observables: tuple[ObservableNeed, ...]
    #: The host command a design chose, once one has.
    command: str | None = None
    #: The resolved parameter values, once a design exists: how a result named
    #: by a path parameter is located.
    parameters: Mapping[str, str] = field(default_factory=dict)
    #: A replication's intended variation, in the attestation vocabulary.
    perturbations: tuple[str, ...] = ()
    #: The longest execution this host permits.
    max_seconds: int | None = None


@dataclass(frozen=True, slots=True)
class Binding:
    """An exact capability binding: which declaration, at which commit, reads what."""

    capability: Capability
    digest: str
    manifest_sha256: str
    commit: str
    #: The result artifact's path, when it is known (always, once a design has
    #: supplied its parameters).
    result_path: str | None
    #: analysis observable name -> capability observable name.
    observables: tuple[tuple[str, str], ...]
    #: Fields the analysis reads that the capability declares nondeterministic.
    nondeterministic: tuple[str, ...] = ()

    def record(self) -> dict[str, Any]:
        cap = self.capability
        return {
            "id": cap.id,
            "version": cap.version,
            "ref": cap.ref,
            "digest": self.digest,
            "command": cap.command,
            "manifest_path": MANIFEST_PATH,
            "manifest_sha256": self.manifest_sha256,
            "commit": self.commit,
            "result_path": self.result_path,
            "result_format": cap.result.format,
            "observables": dict(self.observables),
            "perturbations": [item.token for item in cap.replication.perturbations],
            "comparison": cap.replication.comparison,
            "determinism": cap.determinism,
            "nondeterministic_fields_read": list(self.nondeterministic),
            "inputs": [item.path for item in cap.inputs],
            "timeout_seconds": cap.resources.timeout_seconds,
        }


@dataclass(frozen=True, slots=True)
class Resolution:
    status: CapabilityStatus
    binding: Binding | None = None
    unmet: tuple[Unmet, ...] = ()
    considered: tuple[str, ...] = ()

    @property
    def executable(self) -> bool:
        return self.status is CapabilityStatus.EXECUTABLE

    def record(self) -> dict[str, Any]:
        return {
            "status": str(self.status),
            "binding": self.binding.record() if self.binding else None,
            "unmet": [item.record() for item in self.unmet],
            "considered": list(self.considered),
        }

    def summary(self) -> str:
        if self.binding is not None:
            return f"EXECUTABLE via {self.binding.capability.ref}"
        return "CAPABILITY_LIMITED: " + "; ".join(
            item.rendered() for item in self.unmet
        )


def _result_path(capability: Capability, parameters: Mapping[str, str]) -> str | None:
    if capability.result.artifact:
        return capability.result.artifact
    value = parameters.get(capability.result.artifact_parameter)
    return str(value) if value else None


def result_path_for(
    capability: Capability, parameters: Mapping[str, Any]
) -> str | None:
    """Where one execution's result artifact is, given its parameter values."""

    return _result_path(
        capability,
        {
            str(name): str(value)
            for name, value in dict(parameters).items()
            if not isinstance(value, dict | list)
        },
    )


def _match(
    capability: Capability, need: ObservableNeed, *, result_path: str | None
) -> tuple[CapabilityObservable | None, list[Unmet], list[str]]:
    """The declared observable meeting ``need``, what it lacks, and what varies."""

    ref = capability.ref
    where = (
        f"observable {need.name!r} ({need.kind} at "
        f"{need.path or '(the top level)'!r} in {need.source!r})"
    )
    if result_path is not None and need.source != result_path:
        return (
            None,
            [
                Unmet(
                    where,
                    f"is read from {need.source!r}; the capability's result "
                    f"artifact is {result_path!r}",
                    ref,
                )
            ],
            [],
        )
    candidates = [
        item
        for item in capability.observables
        if item.kind == need.kind and item.path.strip() == need.path.strip()
    ]
    if not candidates:
        declared = ", ".join(
            f"{item.name} ({item.kind} at {item.path or '(top)'})"
            for item in capability.observables
        )
        return (
            None,
            [
                Unmet(
                    where, f"no declared observable is that; declared: {declared}", ref
                )
            ],
            [],
        )
    observable = candidates[0]
    unmet: list[Unmet] = []
    varies: list[str] = []
    if observable.kind == "scalar":
        assert observable.type is not None
        if observable.type not in NUMERIC_TYPES:
            unmet.append(
                Unmet(
                    where,
                    f"is read as a number and {observable.name!r} is declared "
                    f"{observable.type}",
                    ref,
                )
            )
        if not observable.deterministic:
            varies.append(observable.name)
        return (None if unmet else observable), unmet, varies
    for name in need.fields:
        declared_field = observable.field(name)
        if declared_field is None:
            unmet.append(
                Unmet(
                    where,
                    f"reads field {name!r}, which {observable.name!r} does not "
                    f"declare (declared: "
                    f"{', '.join(item.name for item in observable.fields)})",
                    ref,
                )
            )
            continue
        if name in need.numeric and declared_field.type not in NUMERIC_TYPES:
            unmet.append(
                Unmet(
                    where,
                    f"reads field {name!r} as a number and it is declared "
                    f"{declared_field.type}",
                    ref,
                )
            )
        if not declared_field.deterministic:
            varies.append(f"{observable.name}.{name}")
    return (None if unmet else observable), unmet, varies


def _evaluate(
    loaded: LoadedManifest,
    capability: Capability,
    requirements: Requirements,
    commands: Mapping[str, Any],
) -> Resolution:
    unmet = check_against_command(capability, commands)
    result_path = _result_path(capability, requirements.parameters)
    pairs: list[tuple[str, str]] = []
    varies: list[str] = []
    for need in requirements.observables:
        observable, missing, drift = _match(capability, need, result_path=result_path)
        unmet.extend(missing)
        varies.extend(drift)
        if observable is not None:
            pairs.append((need.name, observable.name))
    offered = {item.token for item in capability.replication.perturbations}
    for token in requirements.perturbations:
        if token not in offered:
            unmet.append(
                Unmet(
                    "replication perturbation",
                    f"the replication varies {token!r}; this capability attests "
                    f"{sorted(offered) or 'no perturbation at all'}, so a "
                    f"variation of {token!r} is not one its computation is "
                    f"declared to use",
                    capability.ref,
                )
            )
    if (
        requirements.max_seconds is not None
        and capability.resources.timeout_seconds > requirements.max_seconds
    ):
        unmet.append(
            Unmet(
                "resources",
                f"one execution needs up to {capability.resources.timeout_seconds}s "
                f"and this host permits {requirements.max_seconds}s",
                capability.ref,
            )
        )
    if unmet:
        return Resolution(
            CapabilityStatus.CAPABILITY_LIMITED,
            unmet=tuple(unmet),
            considered=(capability.ref,),
        )
    return Resolution(
        CapabilityStatus.EXECUTABLE,
        binding=Binding(
            capability=capability,
            digest=capability_digest(capability),
            manifest_sha256=loaded.sha256,
            commit=loaded.commit,
            result_path=result_path,
            observables=tuple(pairs),
            nondeterministic=tuple(dict.fromkeys(varies)),
        ),
        considered=(capability.ref,),
    )


def resolve(
    requirements: Requirements,
    *,
    loaded: LoadedManifest | None,
    commands: Mapping[str, Any],
) -> Resolution:
    """``EXECUTABLE`` with an exact binding, or ``CAPABILITY_LIMITED`` with why.

    Ordinary code, deterministic, and total: every declared capability is
    considered (in id order), or only the one backing ``requirements.command``
    once a design has chosen one. The first that meets every requirement is
    the binding; when none does, every unmet requirement of every candidate is
    reported, so the refusal says exactly what a researcher would have to
    declare.
    """

    if loaded is None:
        return Resolution(
            CapabilityStatus.CAPABILITY_LIMITED,
            unmet=(
                Unmet(
                    "capability manifest",
                    f"the repository declares no {MANIFEST_PATH} at the pinned "
                    f"commit, so nothing declares what any command produces",
                ),
            ),
        )
    manifest = loaded.manifest
    if requirements.command is not None:
        chosen = manifest.by_command(requirements.command)
        if chosen is None:
            return Resolution(
                CapabilityStatus.CAPABILITY_LIMITED,
                unmet=(
                    Unmet(
                        "capability",
                        f"command {requirements.command!r} backs no declared "
                        f"capability; declared: "
                        + ", ".join(
                            f"{item.ref} ({item.command})"
                            for item in manifest.capabilities
                        ),
                    ),
                ),
                considered=tuple(item.ref for item in manifest.capabilities),
            )
        candidates: Sequence[Capability] = (chosen,)
    else:
        candidates = sorted(manifest.capabilities, key=lambda item: item.id)
    unmet: list[Unmet] = []
    considered: list[str] = []
    for capability in candidates:
        outcome = _evaluate(loaded, capability, requirements, commands)
        considered.append(capability.ref)
        if outcome.executable:
            return Resolution(
                CapabilityStatus.EXECUTABLE,
                binding=outcome.binding,
                considered=tuple(considered),
            )
        unmet.extend(outcome.unmet)
    return Resolution(
        CapabilityStatus.CAPABILITY_LIMITED,
        unmet=tuple(unmet),
        considered=tuple(considered),
    )


def input_digests(
    repository: Path, *, commit: str, capability: Capability
) -> tuple[list[tuple[str, str]], list[Unmet]]:
    """Each declared input artifact's sha256 at ``commit``, or why it has none."""

    found: list[tuple[str, str]] = []
    unmet: list[Unmet] = []
    for item in capability.inputs:
        data = committed_bytes(repository, commit=commit, path=item.path)
        if data is None:
            unmet.append(
                Unmet(
                    "input artifact",
                    f"{item.path!r} is not a tracked file at {commit[:12]}",
                    capability.ref,
                )
            )
            continue
        found.append((item.path, hashlib.sha256(data).hexdigest()))
    return sorted(found), unmet


# ------------------------------------------------------------ rendering --
def catalogue_lines(
    loaded: LoadedManifest | None, commands: Mapping[str, Any] | None = None
) -> list[str]:
    """The declared capabilities, typed, as a designer is shown them.

    Everything resolution will check and nothing it will not: the result
    artifact, each observable's kind and path, each field's type and unit,
    which values vary between identical executions, the attested
    perturbations and the time each execution needs.
    """

    if loaded is None:
        return []
    lines = [
        (
            f"DECLARED CAPABILITIES ({MANIFEST_PATH} at commit {loaded.commit[:12]}). "
            "These are the only observables that exist. An analysis may read only "
            "these, by exactly this source, kind and path, and only their declared "
            "fields; Research OS checks it mechanically before anything is designed."
        ),
    ]
    for cap in sorted(loaded.manifest.capabilities, key=lambda item: item.id):
        usable = (
            not check_against_command(cap, commands) if commands is not None else True
        )
        lines.append("")
        lines.append(
            f"capability {cap.ref} -- {cap.title} (run by command {cap.command}"
            + ("" if usable else "; NOT usable: it does not match the host command")
            + ")"
        )
        if cap.description:
            lines.append(f"    {cap.description}")
        source = (
            cap.result.artifact
            or f"the path given to parameter `{cap.result.artifact_parameter}`"
        )
        lines.append(f"    result artifact: {source} ({cap.result.format})")
        for item in cap.observables:
            unit = f" [{item.unit}]" if item.unit else ""
            varies = "" if item.deterministic else " NONDETERMINISTIC"
            if item.kind == "scalar":
                lines.append(
                    f"    observable {item.name}: scalar {item.type}{unit}{varies} "
                    f"at path {item.path!r}"
                    + (f" -- {item.description}" if item.description else "")
                )
                continue
            lines.append(
                f"    observable {item.name}: records at path "
                f"{item.path or '(the top level)'!r}"
                + (f" -- {item.description}" if item.description else "")
            )
            for entry in item.fields:
                unit = f" [{entry.unit}]" if entry.unit else ""
                varies = "" if entry.deterministic else " NONDETERMINISTIC"
                lines.append(
                    f"        {entry.name}: {entry.type}{unit}{varies}"
                    + (f" -- {entry.description}" if entry.description else "")
                )
        for note in cap.parameters:
            if note.description or note.unit:
                unit = f" [{note.unit}]" if note.unit else ""
                lines.append(f"    parameter {note.name}{unit}: {note.description}")
        lines.append(f"    determinism: {cap.determinism}")
        if cap.determinism_notes:
            lines.append(f"        {cap.determinism_notes}")
        attested = cap.replication.perturbations
        lines.append(
            "    a replication may vary: "
            + (", ".join(item.token for item in attested) if attested else "nothing")
        )
        for item in attested:
            lines.append(f"        {item.token}: {item.description}")
        lines.append(f"    one execution needs up to {cap.resources.timeout_seconds}s")
        if cap.campaign is None:
            lines.append(
                "    campaign: not declared -- one execution per measurement; its "
                "results cannot be combined with another's"
            )
            continue
        lines.append(
            f"    campaign: up to {cap.campaign.max_units} executions may form one "
            f"measurement; the units must differ in "
            + " or ".join(cap.campaign.unit_varies)
            + " (anything else would repeat an observation, and is refused)"
        )
        for rule in cap.campaign.aggregation:
            identity = (
                f"; one observation is identified by {', '.join(rule.identity)}, and "
                f"a repeated one refuses the result"
                if rule.identity
                else ""
            )
            lines.append(
                f"        {rule.observable}: combined by {rule.rule} across units"
                + identity
            )
        missing = [
            item.name
            for item in cap.observables
            if cap.campaign.rule_for(item.name) is None
        ]
        if missing:
            lines.append(
                "        not combinable across units (a campaign analysis cannot "
                "read them): " + ", ".join(missing)
            )
    return lines


def perturbation_tokens(capability: Capability) -> tuple[str, ...]:
    return tuple(item.token for item in capability.replication.perturbations)


def iter_fields(capability: Capability) -> Iterable[tuple[str, ObservableField]]:
    for item in capability.observables:
        for entry in item.fields:
            yield item.name, entry


__all__ = [
    "MANIFEST_PATH",
    "MANIFEST_SCHEMA",
    "Binding",
    "CampaignAggregation",
    "CampaignSupport",
    "Capability",
    "CapabilityError",
    "CapabilityManifest",
    "CapabilityStatus",
    "LoadedManifest",
    "ObservableNeed",
    "Requirements",
    "Resolution",
    "Unmet",
    "capability_digest",
    "catalogue_lines",
    "check_against_command",
    "committed_bytes",
    "input_digests",
    "load_committed",
    "parse_manifest",
    "perturbation_tokens",
    "resolve",
    "result_path_for",
]
