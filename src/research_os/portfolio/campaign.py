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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from research_os.capability import Capability, Unmet
from research_os.portfolio.contracts import AnalysisSpec, DesignSpecification

#: The version of a campaign's specification digest.
CAMPAIGN_SPEC_VERSION = "rscampaign-spec-v1"
#: The one stopping rule under which a campaign may be read.
CAMPAIGN_STOPPING_RULE = "fixed_campaign"
#: What a campaign missing a unit is: not read. Recorded in the frozen plan.
MISSING_UNITS = "refuse"


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
    #: Per unit after the first: the attested tokens it differs from unit 0 in.
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


def _differences(first: UnitInput, second: UnitInput) -> tuple[str, ...]:
    """What two units differ in, in the attestation vocabulary."""

    tokens: list[str] = []
    if tuple(first.spec.seeds) != tuple(second.spec.seeds):
        tokens.append("seeds")
    for name in sorted(set(first.parameters) | set(second.parameters)):
        if canonical(first.parameters.get(name)) != canonical(
            second.parameters.get(name)
        ):
            tokens.append(name)
    return tuple(tokens)


def compile_campaign(
    units: Sequence[UnitInput],
    *,
    analysis: AnalysisSpec,
    capability: Capability,
    observables: Sequence[tuple[str, str]],
    max_units: int,
    max_seconds: int,
) -> Compiled | Refused:
    """Compile a design's units into a campaign, or refuse it -- before anything runs.

    ``observables`` is the capability binding's analysis-to-capability map;
    ``max_units`` and ``max_seconds`` are the portfolio's human-set bounds.
    Checked, in order, by ordinary code:

    1. the frozen contract's stopping rule is ``fixed_campaign``;
    2. the capability declares campaign support, and allows this many units;
    3. every pair of units differs, and in something the capability attests
       its computation uses -- never padding one observation into several;
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
    unmet: list[Unmet] = []
    varies = set(support.unit_varies)
    assignments: list[tuple[str, ...]] = []
    for index, unit in enumerate(units):
        for other in units[:index]:
            differs = _differences(other, unit)
            if not differs:
                unmet.append(
                    Unmet(
                        "campaign units",
                        f"units {other.index} and {unit.index} are the same execution: "
                        f"a repeated identical run is the same observation, and a "
                        f"sample is not made larger by repeating it",
                        ref,
                    )
                )
            elif not set(differs) & varies:
                unmet.append(
                    Unmet(
                        "campaign units",
                        f"units {other.index} and {unit.index} differ only in "
                        f"{', '.join(differs)}, which {ref} does not attest its "
                        f"computation uses between units (it attests "
                        f"{', '.join(sorted(varies))}); they would be duplicate "
                        f"observations, not independent ones",
                        ref,
                    )
                )
        if index:
            assignments.append(
                tuple(item for item in _differences(units[0], unit) if item in varies)
            )
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
    "Combined",
    "Compiled",
    "Refused",
    "UnitInput",
    "campaign_spec_digest",
    "campaign_variation_digest",
    "combine",
    "compile_campaign",
    "is_campaign",
    "pair_with_primary",
    "unit_designs",
]
