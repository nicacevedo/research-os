"""Multi-execution scientific campaigns: one frozen design, several trusted executions.

``docs/SCIENCE_EXECUTION.md`` §3a is the specification. The first final
qualification was refused on a design whose valid sample -- independent seeds,
more instances than one bounded execution holds -- needed several executions
of the one declared capability, and could have had it only by padding one
execution with repetitions of identical observations. A campaign is the
minimum that measures it honestly::

    scientific contract (stopping rule: fixed_campaign)
      -> experimental design (its units)
        -> frozen execution campaign (a PLAN whose units are frozen)
          -> execution unit 1 .. N      each by the trusted runner, each its own receipt
          -> validated unit results     each against the capability's schema
          -> deterministic aggregation  the capability's declared rule, by code
          -> primary outcome            bound to the campaign and every receipt

Everything here is ordinary code over typed declarations. It compiles a design
into units and refuses a campaign that would not measure what it claims, and it
combines validated unit results. It never runs anything, never reads a model,
and never decides what a result means -- that is the frozen analysis's, applied
once to the combined result.

**No padding.** Two units are two observations only if they differ in
something the capability *attests* its computation uses
(``campaign.unit_varies``, a subset of its replication perturbations). Units
that differ in nothing are refused as a duplicate; units that differ only in
something unattested -- a seed a deterministic program ignores -- are refused
as ``CAPABILITY_LIMITED``: the capability cannot provide the independent
observations the design asks for. And a combined result in which two units
produced a record with the same declared identity is refused as invalid
evidence rather than counted twice.

**Nothing else varies.** What two units differ in is derived by code from
their frozen specifications (:func:`configuration`), never taken from a
list anyone wrote, and *every* difference must be one the frozen analysis
allows (:func:`allowed_variation`): ``actual <= allowed``, and never empty.
A difference that is allowed does not excuse one that is not -- a seed
change beside a changed slope is a campaign whose units measure different
things, which the preregistration did not permit.

**All or nothing.** A campaign is read once, over every unit, under
``fixed_campaign``: a unit that failed makes the attempt an operational
failure, and no partial campaign is ever read as the primary analysis. The
frozen contract's ``on_missing`` has one value, ``INSUFFICIENT``, and no rule
allows a subset.
"""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from research_os.capability import PARAMETER_PATTERN, Capability, Unmet
from research_os.experiment.spec import PLACEHOLDER_RE
from research_os.portfolio.contracts import AnalysisSpec, DesignSpecification

#: The version of a campaign's specification digest.
CAMPAIGN_SPEC_VERSION = "rscampaign-spec-v1"
#: The one stopping rule under which a campaign may be read.
CAMPAIGN_STOPPING_RULE = "fixed_campaign"
#: What a campaign missing a unit is: not read. Recorded in the frozen plan.
MISSING_UNITS = "refuse"
#: The prefix of a difference no declared input accounts for. A token of the
#: attestation vocabulary is ``seeds`` or a parameter name and has no dot, so
#: a difference named with this prefix is never an allowed one.
UNATTRIBUTED = "spec."
#: How the seeds reach the program (``empirical.build_spec``): a difference in
#: one of these variables is a difference in the seeds.
SEED_ENV_PREFIX = "RESEARCH_OS_SEED_"
#: At most this many paths inside one composed document are named per difference.
MAX_REPORTED_PATHS = 8


def canonical(value: Any) -> bytes:
    return json.dumps(
        value,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")


# ------------------------------------------------------------ the units --
def is_campaign(design: DesignSpecification) -> bool:
    return design.campaign is not None


def unit_designs(design: DesignSpecification) -> tuple[DesignSpecification, ...]:
    """Each unit as the complete single-execution design it realises.

    A unit's parameters override the design's key by key and its seeds, when
    it names any, replace the design's. A design with no campaign is its own
    single unit.
    """

    if design.campaign is None:
        return (design,)
    units: list[DesignSpecification] = []
    for unit in design.campaign.units:
        units.append(
            design.model_copy(
                update={
                    "command_parameters": {
                        **dict(design.command_parameters),
                        **dict(unit.command_parameters),
                    },
                    "seeds": tuple(unit.seeds) or tuple(design.seeds),
                    "campaign": None,
                }
            )
        )
    return tuple(units)


def campaign_spec_digest(unit_spec_digests: Sequence[str]) -> str:
    """The campaign's specification digest: its units' digests, in order."""

    return hashlib.sha256(
        canonical({"schema": CAMPAIGN_SPEC_VERSION, "units": list(unit_spec_digests)})
    ).hexdigest()


def campaign_variation_digest(unit_variation_digests: Sequence[str]) -> str:
    """The campaign's scientific identity, where it ran removed: its units'."""

    return hashlib.sha256(
        canonical(
            {
                "schema": CAMPAIGN_SPEC_VERSION,
                "variations": list(unit_variation_digests),
            }
        )
    ).hexdigest()


@dataclass(frozen=True, slots=True)
class UnitInput:
    """One unit as compiled: its design, specification and composed inputs."""

    index: int
    label: str
    design: DesignSpecification
    spec: Any
    frozen_inputs: tuple[Any, ...]
    spec_digest: str
    variation_digest: str
    #: parameter name -> the value, or the composed document's sha256.
    parameters: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Compiled:
    """A campaign that may be frozen: its units, digests, aggregation, bounds."""

    units: tuple[UnitInput, ...]
    spec_digest: str
    variation_digest: str
    #: One entry per observable the frozen analysis reads.
    aggregation: tuple[dict[str, Any], ...]
    resources: dict[str, Any]
    #: Per unit after the first: every token it differs from unit 0 in --
    #: all of them allowed, or the campaign would not have compiled.
    assignments: tuple[tuple[str, ...], ...]


@dataclass(frozen=True, slots=True)
class Refused:
    """Why a design does not compile to a campaign that could be run."""

    unmet: tuple[Unmet, ...]
    #: Whether a different design could compile (a retry), or no design can.
    retry: bool
    #: The outcome the refusal is recorded as.
    state: str = "CAPABILITY_LIMITED"

    def summary(self) -> str:
        prefix = (
            "BUDGET_LIMITED" if self.state == "BUDGET_LIMITED" else "CAPABILITY_LIMITED"
        )
        return f"{prefix}: " + "; ".join(item.rendered() for item in self.unmet)

    def record(self) -> dict[str, Any]:
        return {
            "status": self.state,
            "compiled_by": "research_os.portfolio.campaign.compile_campaign",
            "unmet": [item.record() for item in self.unmet],
            "retry": self.retry,
        }


# ------------------------------------------------ what units differ in --
@dataclass(frozen=True, slots=True)
class Configuration:
    """One unit's scientific configuration, keyed by the difference it would be.

    ``values`` maps a token of the attestation vocabulary -- ``seeds`` or a
    parameter name -- or an :data:`UNATTRIBUTED` name, to the canonical bytes
    of what the unit's frozen specification says of it; two units differ in
    exactly the keys whose bytes are not equal. ``documents`` holds a
    composed parameter's document when its bytes are at hand, so a
    difference inside it can be reported where it is (``plan.slope``).
    """

    values: Mapping[str, bytes]
    documents: Mapping[str, Any] = field(default_factory=dict)


def configuration(
    *,
    argv: Sequence[str],
    seeds: Sequence[int],
    env: Mapping[str, str],
    environment: Mapping[str, str],
    outputs: Sequence[str],
    composed: Sequence[Sequence[str]],
    parameters: Mapping[str, Any],
    command_argv: Sequence[str],
    capability: Capability | None,
    documents: Mapping[str, Any] | None = None,
) -> Configuration:
    """What one frozen execution measures, derived from its specification alone.

    ``command_argv`` is the host command's declared argument vector (its
    placeholders are how a value reaches the program:
    ``experiment.spec.resolve_command`` substitutes whole tokens only), and
    ``capability`` the declaration of the capability the command runs (none:
    nothing is taken as operational). Every part of the specification
    the program can read is attributed to the input that put it there:

    - the seeds, and the ``RESEARCH_OS_SEED_<n>`` variables that deliver them,
      to ``seeds``;
    - a composed document -- the content, by its digest, whatever its path --
      to the parameter that composed it, so a change *anywhere inside* it (a
      slope nested in a plan) is a difference in that parameter;
    - an argument filled from a placeholder, to that parameter;
    - a parameter's recorded value, to that parameter;
    - anything else -- a literal argument, another environment variable, the
      expected outputs, a composed input no parameter accounts for -- to an
      :data:`UNATTRIBUTED` name, which no analysis can allow.

    Not scientific, and not compared: where it ran (``cwd``), its name, its
    implementation bounds (``scicontract.IMPLEMENTATION_FIELDS``), its label
    and every identifier or digest derived from the rest -- and the
    parameter the capability declares as its result's location
    (``result.artifact_parameter``), which says where the result is written,
    not what it measures.
    """

    operational = (
        {capability.result.artifact_parameter} - {""}
        if capability is not None
        else set()
    )
    found: dict[str, dict[str, Any]] = {}

    def put(token: str, part: str, value: Any) -> None:
        found.setdefault(token, {})[part] = value

    put("seeds", "seeds", [int(item) for item in seeds])
    for key, value in sorted(env.items()):
        if key.startswith(SEED_ENV_PREFIX):
            put("seeds", key, str(value))
        else:
            put(UNATTRIBUTED + "env", key, str(value))
    for key, value in sorted(environment.items()):
        put(UNATTRIBUTED + "environment", key, str(value))
    put(UNATTRIBUTED + "outputs", "outputs", sorted(str(item) for item in outputs))
    paths: set[str] = set()
    for path, sha in (tuple(item) for item in composed):
        paths.add(str(path))
        name, _, suffix = (
            str(path).rsplit("/", 1)[-1].removesuffix(".json").rpartition("-")
        )
        if re.fullmatch(PARAMETER_PATTERN, name) and suffix == str(sha)[:16]:
            found.setdefault(name, {}).setdefault("composed", []).append(str(sha))
        else:
            put(UNATTRIBUTED + "inputs", str(path), str(sha))
    template = [str(item) for item in command_argv]
    if len(template) != len(argv):
        put(UNATTRIBUTED + "argv", "argv", [str(item) for item in argv])
    else:
        for position, (declared, token) in enumerate(zip(template, argv, strict=True)):
            placeholder = PLACEHOLDER_RE.fullmatch(declared)
            if placeholder is None:
                put(UNATTRIBUTED + "argv", str(position), str(token))
            elif placeholder.group(1) in operational:
                continue
            elif str(token) in paths:
                # A composed document's path: where Research OS placed it.
                # Its content is compared above, by digest.
                continue
            else:
                put(placeholder.group(1), f"argv[{position}]", str(token))
    for name, value in sorted(parameters.items()):
        if name in operational:
            continue
        if isinstance(value, Mapping) and set(value) == {"composed_sha256"}:
            # A composed document, recorded by its digest; compared above,
            # from the specification's own inputs.
            continue
        put(str(name), "value", value)
    return Configuration(
        values={token: canonical(parts) for token, parts in sorted(found.items())},
        documents=dict(documents or {}),
    )


def unit_configuration(
    unit: UnitInput, *, command_argv: Sequence[str], capability: Capability | None
) -> Configuration:
    """:func:`configuration` of a unit as the designer's route compiles it."""

    documents: dict[str, Any] = {}
    for item in unit.frozen_inputs:
        try:
            documents[str(item.parameter)] = json.loads(item.canonical)
        except (AttributeError, TypeError, ValueError):
            continue
    spec = unit.spec
    return configuration(
        argv=tuple(spec.argv),
        seeds=tuple(spec.seeds),
        env=dict(spec.env),
        environment=dict(spec.environment),
        outputs=tuple(spec.outputs),
        composed=tuple(spec.inputs),
        parameters=dict(unit.parameters),
        command_argv=command_argv,
        capability=capability,
        documents=documents,
    )


def record_configuration(
    record: Mapping[str, Any], *, command_argv: Sequence[str], capability: Capability
) -> Configuration:
    """:func:`configuration` of a unit as a frozen campaign plan holds it."""

    settings = dict(record.get("configuration") or {})
    return configuration(
        argv=tuple(record.get("argv") or ()),
        seeds=tuple(settings.get("seeds") or ()),
        env=dict(settings.get("env") or {}),
        environment=dict(settings.get("environment") or {}),
        outputs=tuple(record.get("expected_outputs") or ()),
        composed=tuple(dict(record.get("inputs") or {}).get("composed") or ()),
        parameters=dict(record.get("parameters") or {}),
        command_argv=command_argv,
        capability=capability,
    )


def differences(first: Configuration, second: Configuration) -> tuple[str, ...]:
    """Every token two units differ in: ``seeds`` first, then by name. None dropped."""

    tokens = [
        token
        for token in set(first.values) | set(second.values)
        if first.values.get(token) != second.values.get(token)
    ]
    return tuple(sorted(tokens, key=lambda token: (token != "seeds", token)))


def _paths(first: Any, second: Any, prefix: str) -> list[str]:
    """Where inside two documents they differ, as dotted paths."""

    if isinstance(first, Mapping) and isinstance(second, Mapping):
        found: list[str] = []
        for key in sorted(set(first) | set(second), key=str):
            if key not in first or key not in second:
                found.append(f"{prefix}.{key}")
            else:
                found.extend(_paths(first[key], second[key], f"{prefix}.{key}"))
        return found
    if (
        isinstance(first, list)
        and isinstance(second, list)
        and len(first) == len(second)
    ):
        found = []
        for position, (mine, theirs) in enumerate(zip(first, second, strict=True)):
            found.extend(_paths(mine, theirs, f"{prefix}[{position}]"))
        return found
    return [] if canonical(first) == canonical(second) else [prefix]


@dataclass(frozen=True, slots=True)
class PairDifference:
    """What two units of one campaign differ in: every scientific difference."""

    first: int
    second: int
    tokens: tuple[str, ...]
    #: A composed parameter -> the paths inside its document that differ.
    paths: Mapping[str, tuple[str, ...]] = field(default_factory=dict)

    def rendered(self, tokens: Iterable[str] | None = None) -> str:
        chosen = set(self.tokens if tokens is None else tokens)
        shown: list[str] = []
        for token in self.tokens:
            if token not in chosen:
                continue
            inside = list(self.paths.get(token) or ())
            if len(inside) > MAX_REPORTED_PATHS:
                inside = [
                    *inside[:MAX_REPORTED_PATHS],
                    f"{len(inside) - MAX_REPORTED_PATHS} more",
                ]
            shown.append(token + (f" (at {', '.join(inside)})" if inside else ""))
        return ", ".join(shown)


def pair_differences(
    configurations: Sequence[tuple[int, Configuration]],
) -> tuple[PairDifference, ...]:
    """Every pair of units, in order, with everything each pair differs in."""

    pairs: list[PairDifference] = []
    for position, (index, second) in enumerate(configurations):
        for other, first in configurations[:position]:
            tokens = differences(first, second)
            pairs.append(
                PairDifference(
                    first=other,
                    second=index,
                    tokens=tokens,
                    paths={
                        token: tuple(
                            _paths(
                                first.documents[token], second.documents[token], token
                            )
                        )
                        for token in tokens
                        if token in first.documents and token in second.documents
                    },
                )
            )
    return tuple(pairs)


def stated_variation(analysis: AnalysisSpec) -> tuple[str, ...]:
    """What the frozen analysis's execution shape names as unit differences."""

    shape = analysis.execution_shape
    return tuple(shape.unit_varies) if shape is not None else ()


def allowed_variation(stated: Sequence[str], capability: Capability) -> tuple[str, ...]:
    """What a frozen analysis lets campaign units differ in, and never more.

    ``stated`` is its ``execution_shape.unit_varies``; empty, or no shape, is
    whatever the capability allows. Never more than the capability attests
    (``campaign.unit_varies``): the pre-freeze check refuses an analysis that
    names more, and this does not rely on it. **Allowed is not required:** a
    v1 analysis marks no difference as one every pair must have, so units
    that differ in some of what is allowed -- and in nothing else --
    realise it; whether what they differ in can hold the frozen support is
    ``portfolio.shape``'s question, and the data's.
    """

    attested = (
        tuple(capability.campaign.unit_varies)
        if capability.campaign is not None
        else ()
    )
    return tuple(token for token in (tuple(stated) or attested) if token in attested)


def unit_variation(
    pairs: Sequence[PairDifference],
    *,
    allowed: Sequence[str],
    attested: Sequence[str],
    ref: str,
) -> list[Unmet]:
    """Why the units of one campaign do not vary as the frozen analysis allows.

    For every pair: it differs in something (``actual != {}``), and in
    nothing the frozen analysis does not allow (``actual <= allowed``). One
    allowed difference never masks another that is not, and every one that
    is not is reported.
    """

    unmet: list[Unmet] = []
    varies = set(attested)
    permitted = set(allowed)
    for pair in pairs:
        differs = pair.tokens
        forbidden = [item for item in differs if item not in permitted]
        if not differs:
            unmet.append(
                Unmet(
                    "campaign units",
                    f"units {pair.first} and {pair.second} are the same execution: "
                    f"a repeated identical run is the same observation, and a "
                    f"sample is not made larger by repeating it",
                    ref,
                )
            )
        elif not set(differs) & varies:
            unmet.append(
                Unmet(
                    "campaign units",
                    f"units {pair.first} and {pair.second} differ only in "
                    f"{pair.rendered()}, which {ref} does not attest its "
                    f"computation uses between units (it attests "
                    f"{', '.join(sorted(varies))}); they would be duplicate "
                    f"observations, not independent ones",
                    ref,
                )
            )
        elif forbidden:
            unmet.append(
                Unmet(
                    "campaign units",
                    f"units {pair.first} and {pair.second} differ in "
                    f"{pair.rendered(forbidden)}, and the frozen analysis lets "
                    f"campaign units differ only in "
                    f"{', '.join(allowed) or 'nothing'}; what else they differ in "
                    f"does not excuse it -- a campaign whose units vary what the "
                    f"preregistration fixed measures something it did not "
                    f"preregister",
                    ref,
                )
            )
    return unmet


def compile_campaign(
    units: Sequence[UnitInput],
    *,
    analysis: AnalysisSpec,
    capability: Capability,
    observables: Sequence[tuple[str, str]],
    max_units: int,
    max_seconds: int,
    command_argv: Sequence[str],
) -> Compiled | Refused:
    """Compile a design's units into a campaign, or refuse it -- before anything runs.

    ``observables`` is the capability binding's analysis-to-capability map;
    ``max_units`` and ``max_seconds`` are the portfolio's human-set bounds;
    ``command_argv`` is the host command's declared argument vector.
    Checked, in order, by ordinary code:

    1. the frozen contract's stopping rule is ``fixed_campaign``;
    2. the capability declares campaign support, and allows this many units;
    3. every pair of units differs, in something the capability attests its
       computation uses -- never padding one observation into several -- and
       in nothing the frozen analysis does not allow: what they differ in is
       derived from their specifications (:func:`configuration`), and one
       allowed difference never excuses another (:func:`unit_variation`);
    4. every observable the analysis reads has a declared rule to combine it;
    5. the campaign fits the human-set bounds: units and total seconds.
    """

    ref = capability.ref
    count = len(units)
    if analysis.stopping_rule != CAMPAIGN_STOPPING_RULE:
        return Refused(
            (
                Unmet(
                    "stopping rule",
                    f"the frozen contract fixes {analysis.stopping_rule}; a campaign of "
                    f"{count} executions is read only under {CAMPAIGN_STOPPING_RULE}, "
                    f"so this design must be one execution",
                    ref,
                ),
            ),
            retry=True,
        )
    support = capability.campaign
    if support is None:
        return Refused(
            (
                Unmet(
                    "campaign",
                    f"{ref} declares no campaign support: its executions cannot be "
                    f"combined into one measurement, so a sample larger than one "
                    f"execution holds is not something it can provide",
                    ref,
                ),
            ),
            retry=True,
        )
    if count > support.max_units:
        return Refused(
            (
                Unmet(
                    "campaign units",
                    f"the design has {count} units and {ref} allows at most "
                    f"{support.max_units} in one campaign",
                    ref,
                ),
            ),
            retry=True,
        )
    configurations = [
        (
            unit.index,
            unit_configuration(unit, command_argv=command_argv, capability=capability),
        )
        for unit in units
    ]
    unmet: list[Unmet] = unit_variation(
        pair_differences(configurations),
        allowed=allowed_variation(stated_variation(analysis), capability),
        attested=support.unit_varies,
        ref=ref,
    )
    assignments: list[tuple[str, ...]] = [
        differences(configurations[0][1], configured)
        for _index, configured in configurations[1:]
    ]
    aggregation: list[dict[str, Any]] = []
    for analysis_name, capability_name in observables:
        rule = support.rule_for(capability_name)
        if rule is None:
            unmet.append(
                Unmet(
                    "campaign aggregation",
                    f"the analysis reads {capability_name!r}, which {ref} declares no "
                    f"rule to combine across executions",
                    ref,
                )
            )
            continue
        declared = capability.observable(capability_name)
        aggregation.append(
            {
                "analysis_observable": analysis_name,
                "capability_observable": capability_name,
                "kind": declared.kind if declared is not None else None,
                "path": declared.path if declared is not None else None,
                "rule": rule.rule,
                "identity": list(rule.identity),
            }
        )
    if unmet:
        return Refused(tuple(unmet), retry=True)
    seconds = sum(int(unit.spec.timeout_seconds) for unit in units)
    bounded: list[Unmet] = []
    if count > max_units:
        bounded.append(
            Unmet(
                "campaign authority",
                f"the design has {count} units and this portfolio permits "
                f"{max_units} (bounds.max_campaign_units, a person's number)",
                ref,
            )
        )
    if seconds > max_seconds:
        bounded.append(
            Unmet(
                "campaign authority",
                f"its units may run up to {seconds}s together and this portfolio "
                f"permits {max_seconds}s for one campaign "
                f"(bounds.max_campaign_seconds, a person's number)",
                ref,
            )
        )
    if bounded:
        return Refused(tuple(bounded), retry=True, state="BUDGET_LIMITED")
    return Compiled(
        units=tuple(units),
        spec_digest=campaign_spec_digest([unit.spec_digest for unit in units]),
        variation_digest=campaign_variation_digest(
            [unit.variation_digest for unit in units]
        ),
        aggregation=tuple(aggregation),
        resources={
            "units": count,
            "seconds_upper_bound": seconds,
            "max_unit_seconds": max(int(unit.spec.timeout_seconds) for unit in units),
            "work_items": count,
            "cpus": capability.resources.cpus,
            "memory_mb": capability.resources.memory_mb,
        },
        assignments=tuple(assignments),
    )


def pair_with_primary(
    replication: Sequence[UnitInput], primary: Sequence[Mapping[str, Any]]
) -> list[Unmet]:
    """A replication campaign measures the primary's campaign again, unit by unit.

    Unit ``i`` of the replication is compared with unit ``i`` of the primary
    -- its configuration independence and its agreement are established per
    pair -- so the two must have the same number of units, and every pair
    must differ in science (not only in where it ran). And no replication
    unit may be *any* primary unit run again: the primary's seeds shifted or
    permuted by one position differ pair by pair and re-measure the primary's
    own observations, which the replication would then count as independent.
    """

    unmet: list[Unmet] = []
    if len(replication) != len(primary):
        unmet.append(
            Unmet(
                "replication campaign",
                f"the primary campaign has {len(primary)} units and this "
                f"replication {len(replication)}; each replication unit replicates "
                f"the primary unit of the same index",
            )
        )
        return unmet
    measured = {
        str(theirs.get("variation_digest") or ""): position
        for position, theirs in enumerate(primary)
    }
    for mine in replication:
        position = measured.get(mine.variation_digest)
        if position is not None:
            unmet.append(
                Unmet(
                    "replication campaign",
                    f"unit {mine.index} is the primary's unit {position} run again: "
                    f"a replication varies something the capability attests, and "
                    f"no replication unit repeats any unit of the primary",
                )
            )
    return unmet


# ------------------------------------------------------------ combining --
@dataclass(frozen=True, slots=True)
class Combined:
    """A campaign's combined result, or why the units' results do not combine."""

    ok: bool
    reason: str
    detail: str
    document: dict[str, Any] | None = None
    data: bytes | None = None
    sha256: str | None = None


def _at(document: Any, path: str) -> tuple[bool, Any]:
    node = document
    for part in [item for item in path.split(".") if item]:
        if isinstance(node, Mapping) and part in node:
            node = node[part]
        else:
            return False, None
    return True, node


def _set_at(document: Any, path: str, value: Any) -> Any:
    """``document`` with ``value`` at ``path``; the value itself at the top level.

    ``None`` when the paths of two rules cannot both hold: one observable at
    the top level and another anywhere else.
    """

    parts = [item for item in path.split(".") if item]
    if not parts:
        return value if document is None else None
    if document is None:
        document = {}
    node = document
    for part in parts[:-1]:
        if not isinstance(node, dict):
            return None
        node = node.setdefault(part, {})
    if not isinstance(node, dict):
        return None
    node[parts[-1]] = value
    return document


def combine(
    aggregation: Sequence[Mapping[str, Any]],
    documents: Sequence[tuple[int, Mapping[str, Any]]],
) -> Combined:
    """Combine every unit's *validated* result by the frozen aggregation rules.

    ``documents`` is ``(unit index, parsed result)``, one per unit, each
    already validated against the capability's declared schema. Records are
    concatenated in unit order; numeric scalars are summed, or their minimum
    or maximum taken. A declared record identity seen in two units is the same
    observation twice, and the combination is refused. The combined document
    holds only the combined observables, at their declared paths, and its
    canonical bytes are what the analysis reads and the outcome names.
    """

    combined: Any = None

    def place(path: str, value: Any, name: str) -> Combined | None:
        nonlocal combined
        placed = _set_at(combined, path, value)
        if placed is None:
            return Combined(
                False,
                "unit_observable_absent",
                f"{name!r} at {path or '(the top level)'!r} cannot be combined "
                f"beside the other observables' paths",
            )
        combined = placed
        return None

    for rule in aggregation:
        path = str(rule.get("path") or "")
        kind = rule.get("kind")
        values: list[tuple[int, Any]] = []
        for unit, document in documents:
            present, value = _at(document, path)
            if not present:
                return Combined(
                    False,
                    "unit_observable_absent",
                    f"unit {unit} holds no {rule['capability_observable']!r} at "
                    f"{path or '(the top level)'!r}",
                )
            values.append((unit, value))
        if kind == "records":
            identity = [str(item) for item in rule.get("identity") or ()]
            seen: dict[str, int] = {}
            merged: list[Any] = []
            for unit, records in values:
                if not isinstance(records, list):
                    return Combined(
                        False,
                        "unit_observable_absent",
                        f"unit {unit}'s {rule['capability_observable']!r} is not a list",
                    )
                keys_this_unit: set[str] = set()
                for record in records:
                    if identity and isinstance(record, Mapping):
                        key = canonical([record.get(name) for name in identity]).decode(
                            "utf-8"
                        )
                        if (
                            key in seen
                            and seen[key] != unit
                            and key not in keys_this_unit
                        ):
                            return Combined(
                                False,
                                "duplicate_observation",
                                f"units {seen[key]} and {unit} produced the same "
                                f"observation of {rule['capability_observable']!r} "
                                f"(identity {', '.join(identity)} = {key}): a campaign "
                                f"does not count one observation twice",
                            )
                        seen.setdefault(key, unit)
                        keys_this_unit.add(key)
                    merged.append(record)
            refused = place(path, merged, str(rule["capability_observable"]))
            if refused is not None:
                return refused
            continue
        numbers: list[float] = []
        for unit, value in values:
            if isinstance(value, bool) or not isinstance(value, int | float):
                return Combined(
                    False,
                    "unit_observable_absent",
                    f"unit {unit}'s {rule['capability_observable']!r} is not a number",
                )
            numbers.append(value)
        operation = str(rule.get("rule"))
        total: Any = (
            sum(numbers)
            if operation == "sum"
            else min(numbers)
            if operation == "min"
            else max(numbers)
        )
        if not math.isfinite(total):
            return Combined(
                False,
                "unit_observable_absent",
                f"the {operation} of {rule['capability_observable']!r} over the "
                f"units is not a finite number",
            )
        refused = place(path, total, str(rule["capability_observable"]))
        if refused is not None:
            return refused
    if combined is None:
        combined = {}
    data = canonical(combined)
    return Combined(
        True,
        "combined",
        f"{len(documents)} unit result(s) combined by their frozen rules",
        document=combined,
        data=data,
        sha256=hashlib.sha256(data).hexdigest(),
    )


__all__ = [
    "CAMPAIGN_SPEC_VERSION",
    "CAMPAIGN_STOPPING_RULE",
    "MISSING_UNITS",
    "UNATTRIBUTED",
    "Combined",
    "Compiled",
    "Configuration",
    "PairDifference",
    "Refused",
    "UnitInput",
    "allowed_variation",
    "campaign_spec_digest",
    "campaign_variation_digest",
    "combine",
    "compile_campaign",
    "configuration",
    "differences",
    "is_campaign",
    "pair_differences",
    "pair_with_primary",
    "record_configuration",
    "stated_variation",
    "unit_configuration",
    "unit_designs",
    "unit_variation",
]
