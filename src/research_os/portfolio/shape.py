"""Whether a proposed analysis can be executed as it says -- decided before it is frozen.

``docs/SCIENCE_EXECUTION.md`` §2a is the specification. The second final v1
qualification (66d5704) had two independently generated lineages reach the
evidence stage, and in both the analysis author froze
``fixed_single_execution`` for a support requirement one execution of the
only capability could not hold: at least ten instances where a plan holds
four, and twelve distinct seeds where one execution draws four. The
experiment designer then saw the per-execution bound, correctly refused, and
the frozen contracts were blocked for good -- although a campaign the
capability declared, within the human-set bounds, could have held both. The
author had been told campaigns existed; it had not been told, in anything
it could compute with, where one execution ends.

So the order is now::

    scientific evidence requirement      the analysis's own support requirements
    -> committed capability envelope      research_os.capability.ExecutionEnvelope
    -> proposed analysis / stopping rule  the analysis author, shown the envelope
    -> mechanical feasibility check       this module, ordinary code
    -> revision, if the shape was wrong   the author again, shown exactly why
    -> frozen executable analysis         portfolio.empirical._freeze_analysis
    -> design -> execution or campaign

and the check has four answers:

    VALID_SINGLE_EXECUTION    one execution holds every requirement
    VALID_CAMPAIGN            an allowed campaign, in the stated shape, holds them
    EXECUTION_SHAPE_MISMATCH  the stated shape cannot, and another allowed one can
    CAPABILITY_LIMITED        neither one execution nor any allowed campaign can

plus ``UNRESOLVED`` when there is nothing to check (the analysis reads nothing
a declared capability produces, which capability resolution then reports
exactly as it always did).

**What it computes is a lower bound on what the analysis needs**, never an
estimate of what a design will manage. From the frozen support requirements
-- ``min_records`` and ``min_distinct`` per analysed observable -- and the
observables' inclusion rules: two observables selecting *different* values of
one field of a bounded input (``family == 'sparse'``, ``family == 'illcond'``)
select records from different slots of it, so their needs add; otherwise they
may share, and the larger one counts. A refusal therefore means the
requirement *cannot* be met in that shape; a valid verdict means only that
the envelope does not rule it out -- the design is still checked when it is
compiled, and the data when they are read.

**What it never does:** rewrite an analysis, convert a single execution into
a campaign, change a hypothesis, lower a requirement, or read a capacity
from anything the model wrote. A model's ``execution_shape`` is a claim this
module checks; the capacities it computes with come from the committed
envelope alone.
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from research_os import capability as capabilities
from research_os.portfolio.campaign import PairDifference
from research_os.portfolio.contracts import AnalysisSpec, Observable
from research_os.portfolio.models import ExecutionShapeVerdict

CHECK_SCHEMA = "research-os-execution-shape-check-v1"
CHECKED_BY = "research_os.portfolio.shape.check"
CAMPAIGN_RULE = "fixed_campaign"

#: How many proposals of one idea version's analysis may be refused before
#: freezing, under one envelope: the first, and the one revision the author
#: is shown the refusal for. The retry that carries the revision is the
#: queue's existing one (``MODEL_OUTPUT_INVALID``); beyond this no further
#: analysis call is made for that version until the envelope changes, so an
#: impossible analysis is not bought again and again. Counted from
#: ``analysis_drafts`` rows, never from memory.
MAX_REFUSED_DRAFTS = 2


# ---------------------------------------------------------------- demands --
@dataclass(frozen=True, slots=True)
class Demand:
    """One thing the analysis's support needs of one bounded quantity."""

    #: The capability observable whose records are needed.
    observable: str
    #: ``records``, or the name of an input bound.
    bound: str
    #: The field whose distinct values are needed; ``None`` for records, or
    #: for the input's slots jointly (every field of it at once).
    field: str | None
    needed: int
    one_execution: int
    #: The unit differences that renew this quantity across a campaign.
    renewed_by: tuple[str, ...]
    #: Which analysis observables need it, and how much each.
    because: tuple[str, ...] = ()

    def capacity(self, units: int, varies: Iterable[str]) -> int:
        """The most a campaign of ``units`` units differing in ``varies`` could hold."""

        if units <= 1:
            return self.one_execution
        if self.field is None or set(self.renewed_by) & set(varies):
            return units * self.one_execution
        return self.one_execution

    def rendered(self) -> str:
        what = (
            "records"
            if self.bound == "records"
            else (f"distinct {self.field}" if self.field else f"{self.bound} (jointly)")
        )
        return f"{what} of {self.observable}: needs {self.needed}" + (
            f" ({'; '.join(self.because)})" if self.because else ""
        )

    def record(self, *, units: int, varies: Sequence[str]) -> dict[str, Any]:
        return {
            "observable": self.observable,
            "bound": self.bound,
            "field": self.field,
            "needed": self.needed,
            "one_execution": self.one_execution,
            "campaign": self.capacity(units, varies) if units >= 2 else None,
            "renewed_by": list(self.renewed_by),
            "because": list(self.because),
        }


def _pins(observable: Observable) -> dict[str, Any]:
    """The fields an observable's inclusion rules fix to one value."""

    pins: dict[str, Any] = {}
    for condition in observable.include:
        if condition.comparator == "==":
            pins.setdefault(condition.field, condition.value)
    return pins


def _key(value: Any) -> str:
    return json.dumps(value, sort_keys=True)


def _needed(
    per_observable: Mapping[str, int],
    pins: Mapping[str, Mapping[str, Any]],
    partition_fields: Iterable[str],
    labels: Mapping[str, str],
) -> tuple[int, tuple[str, ...]]:
    """The least of a quantity the observables together need, and why.

    The largest single need, or -- for a field every observable in a group
    fixes to a *different* value -- the sum over the values of the largest
    need at each, when that is larger: records selected by different values
    of one field of a bounded input come from different slots of it.
    """

    wanted = {name: need for name, need in per_observable.items() if need > 0}
    if not wanted:
        return 0, ()
    best = max(wanted.values())
    because = tuple(
        f"{labels[name]} needs {need}"
        for name, need in sorted(wanted.items())
        if need == best
    )[:1]
    for field_name in partition_fields:
        groups: dict[str, int] = {}
        members: dict[str, list[str]] = {}
        for name, need in wanted.items():
            if field_name not in pins[name]:
                continue
            key = _key(pins[name][field_name])
            groups[key] = max(groups.get(key, 0), need)
            members.setdefault(key, []).append(name)
        if len(groups) < 2:
            continue
        total = sum(groups.values())
        if total > best:
            best = total
            because = tuple(
                f"{labels[name]} needs {wanted[name]}"
                for key in sorted(members)
                for name in sorted(members[key])
                if wanted[name] == groups[key]
            )
    return best, because


def demands(
    analysis: AnalysisSpec,
    *,
    envelope: capabilities.CapabilityEnvelope,
    observables: Sequence[tuple[str, str]],
) -> tuple[Demand, ...]:
    """Everything the analysis's support needs of the envelope's bounded quantities.

    ``observables`` is the capability binding's analysis-to-capability map.
    Only bounded quantities appear: a field the declaration does not bound
    cannot make an analysis unrealisable here.
    """

    specs = {item.name: item for item in analysis.observables}
    by_capability: dict[str, list[str]] = {}
    for analysis_name, capability_name in observables:
        spec = specs.get(analysis_name)
        if spec is not None and spec.kind == "records":
            by_capability.setdefault(capability_name, []).append(analysis_name)
    records: dict[str, int] = {}
    distinct: dict[str, dict[str, int]] = {}
    for rule in analysis.support:
        records[rule.observable] = max(
            records.get(rule.observable, 0), rule.min_records
        )
        wanted = distinct.setdefault(rule.observable, {})
        for name, count in rule.min_distinct.items():
            wanted[name] = max(wanted.get(name, 0), count)
    supported = set(records)
    found: list[Demand] = []
    for capability_name, names in sorted(by_capability.items()):
        pins = {name: _pins(specs[name]) for name in names}
        labels = {
            name: name
            + (
                " ("
                + ", ".join(f"{key} == {value!r}" for key, value in pins[name].items())
                + ")"
                if pins[name]
                else ""
            )
            for name in names
        }
        every_pin = sorted({key for name in names for key in pins[name]})
        limit = envelope.records_max(capability_name)
        if limit is not None:
            need, because = _needed(
                {name: records.get(name, 0) for name in names}, pins, every_pin, labels
            )
            if need > 1:
                found.append(
                    Demand(
                        observable=capability_name,
                        bound="records",
                        field=None,
                        needed=need,
                        one_execution=limit,
                        renewed_by=envelope.unit_varies,
                        because=because,
                    )
                )
        for bound in envelope.inputs_of(capability_name):
            fields = tuple(bound.fields)
            for field_name in fields:
                need, because = _needed(
                    {name: distinct.get(name, {}).get(field_name, 0) for name in names},
                    pins,
                    fields,
                    labels,
                )
                if need > 0:
                    found.append(
                        Demand(
                            observable=capability_name,
                            bound=bound.name,
                            field=field_name,
                            needed=need,
                            one_execution=int(bound.max),
                            renewed_by=envelope.renewed_by(bound, field_name),
                            because=because,
                        )
                    )
            joint, because = _needed(
                {
                    name: _slots(name, fields, distinct, pins, supported)
                    for name in names
                },
                pins,
                fields,
                labels,
            )
            single = max(
                (
                    item.needed
                    for item in found
                    if item.bound == bound.name and item.field is not None
                ),
                default=0,
            )
            if joint > single:
                found.append(
                    Demand(
                        observable=capability_name,
                        bound=bound.name,
                        field=None,
                        needed=joint,
                        one_execution=int(bound.max),
                        renewed_by=envelope.unit_varies,
                        because=because,
                    )
                )
    return tuple(found)


def _slots(
    name: str,
    fields: Sequence[str],
    distinct: Mapping[str, Mapping[str, int]],
    pins: Mapping[str, Mapping[str, Any]],
    supported: set[str],
) -> int:
    """The slots of one bounded input one analysed observable needs, at least.

    As many as the most distinct values it needs of any field of the input;
    and at least one when it must hold records and selects one value of a
    field of the input -- two observables of two families each need an
    instance of their family, whatever else they need.
    """

    need = max((distinct.get(name, {}).get(item, 0) for item in fields), default=0)
    if name in supported and any(item in pins[name] for item in fields):
        need = max(need, 1)
    return need


def _fixed_joint(
    analysis: AnalysisSpec,
    *,
    envelope: capabilities.CapabilityEnvelope,
    observables: Sequence[tuple[str, str]],
    varies: Sequence[str],
) -> list[Demand]:
    """What a campaign differing only in ``varies`` cannot renew, needed jointly.

    Units that renew none of an input's fields hold, of those fields
    together, only the slots one execution has: one analysis needing three
    distinct sizes and another three distinct conditions, of disjoint
    families, need six instances the seeds cannot supply.
    """

    specs = {item.name: item for item in analysis.observables}
    distinct: dict[str, dict[str, int]] = {}
    for rule in analysis.support:
        wanted = distinct.setdefault(rule.observable, {})
        for name, count in rule.min_distinct.items():
            wanted[name] = max(wanted.get(name, 0), count)
    supported = {rule.observable for rule in analysis.support}
    out: list[Demand] = []
    for capability_name in sorted({item[1] for item in observables}):
        names = [
            analysis_name
            for analysis_name, bound_name in observables
            if bound_name == capability_name
            and analysis_name in specs
            and specs[analysis_name].kind == "records"
        ]
        pins = {name: _pins(specs[name]) for name in names}
        for bound in envelope.inputs_of(capability_name):
            fixed = [
                item
                for item in bound.fields
                if not set(envelope.renewed_by(bound, item)) & set(varies)
            ]
            if len(fixed) < 2:
                continue
            need, because = _needed(
                {
                    name: _slots(name, fixed, distinct, pins, supported)
                    for name in names
                },
                pins,
                bound.fields,
                {name: name for name in names},
            )
            if need > int(bound.max):
                out.append(
                    Demand(
                        observable=capability_name,
                        bound=bound.name,
                        field=None,
                        needed=need,
                        one_execution=int(bound.max),
                        renewed_by=(),
                        because=because,
                    )
                )
    return out


# ------------------------------------------------------------------ check --
@dataclass(frozen=True, slots=True)
class ShapeCheck:
    """The verdict, and everything it was decided from. Recorded, never re-derived."""

    verdict: ExecutionShapeVerdict
    envelope_digest: str | None
    commit: str | None
    capability: str | None
    stopping_rule: str
    stated: Mapping[str, Any] | None
    demands: tuple[Demand, ...] = ()
    single_fits: bool = False
    campaign_supported: bool = False
    max_units: int = 0
    unit_varies: tuple[str, ...] = ()
    campaign_fits: bool = False
    min_units: int | None = None
    problems: tuple[str, ...] = ()
    suggestion: str = ""
    unresolved: str = ""
    #: The shape a design must realise: the stated units and differences, or
    #: the envelope's. Recorded so the design is checked against it.
    shape_units: int | None = None
    shape_varies: tuple[str, ...] = field(default_factory=tuple)

    @property
    def valid(self) -> bool:
        return self.verdict in {
            ExecutionShapeVerdict.VALID_SINGLE_EXECUTION,
            ExecutionShapeVerdict.VALID_CAMPAIGN,
        }

    @property
    def refused(self) -> bool:
        return self.verdict in {
            ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH,
            ExecutionShapeVerdict.CAPABILITY_LIMITED,
        }

    def record(self) -> dict[str, Any]:
        units = self.max_units if self.campaign_supported else 0
        return {
            "schema": CHECK_SCHEMA,
            "checked_by": CHECKED_BY,
            "verdict": str(self.verdict),
            "envelope_digest": self.envelope_digest,
            "commit": self.commit,
            "capability": self.capability,
            "stopping_rule": self.stopping_rule,
            "stated_shape": dict(self.stated) if self.stated is not None else None,
            "single_execution": {"fits": self.single_fits},
            "campaign": {
                "supported": self.campaign_supported,
                "max_units": self.max_units,
                "unit_varies": list(self.unit_varies),
                "fits": self.campaign_fits,
                "min_units": self.min_units,
            },
            "demands": [
                item.record(units=units, varies=self.unit_varies)
                for item in self.demands
            ],
            "problems": list(self.problems),
            "suggestion": self.suggestion,
            "unresolved": self.unresolved,
            "shape": {
                "units": self.shape_units,
                "unit_varies": list(self.shape_varies),
            },
        }

    def summary(self) -> str:
        head = f"{self.verdict}"
        if self.capability:
            head += f" against {self.capability} (envelope {self.envelope_digest})"
        if self.problems:
            head += ": " + "; ".join(self.problems)
        if self.suggestion:
            head += ". " + self.suggestion
        return head


def _unresolved(
    analysis: AnalysisSpec,
    envelope: capabilities.ExecutionEnvelope | None,
    reason: str,
) -> ShapeCheck:
    return ShapeCheck(
        verdict=ExecutionShapeVerdict.UNRESOLVED,
        envelope_digest=envelope.digest if envelope is not None else None,
        commit=envelope.commit if envelope is not None else None,
        capability=None,
        stopping_rule=analysis.stopping_rule,
        stated=(
            analysis.execution_shape.model_dump(mode="json")
            if analysis.execution_shape is not None
            else None
        ),
        unresolved=reason,
    )


def _fits(
    found: Sequence[Demand],
    *,
    units: int,
    varies: Sequence[str],
    analysis: AnalysisSpec,
    envelope: capabilities.CapabilityEnvelope,
    observables: Sequence[tuple[str, str]],
) -> list[str]:
    """Why a campaign of ``units`` differing in ``varies`` cannot hold it; empty if it can."""

    short = [
        f"{item.rendered()}, and "
        + (
            f"one execution holds {item.one_execution}"
            if units <= 1
            else f"{units} units differing in {', '.join(varies) or 'nothing'} "
            f"hold at most {item.capacity(units, varies)}"
        )
        for item in found
        if item.needed > item.capacity(units, varies)
    ]
    if units >= 2:
        short.extend(
            f"{item.rendered()} of what units differing in "
            f"{', '.join(varies)} cannot renew, and a campaign holds only one "
            f"execution's {item.one_execution} of it"
            for item in _fixed_joint(
                analysis, envelope=envelope, observables=observables, varies=varies
            )
        )
    return short


def check(
    analysis: AnalysisSpec,
    *,
    envelope: capabilities.ExecutionEnvelope,
    binding: capabilities.Binding | None,
) -> ShapeCheck:
    """The execution-shape verdict for one proposed analysis. Deterministic.

    ``binding`` is capability resolution's answer for the analysis's
    observables; without one there is nothing this module could check.
    """

    if not analysis.analysable:
        return _unresolved(analysis, envelope, "the analysis is not analysable")
    if binding is None:
        return _unresolved(
            analysis,
            envelope,
            "what the analysis reads resolves to no declared capability",
        )
    cap = envelope.capability(binding.capability.ref)
    if cap is None:  # pragma: no cover - the binding is of this manifest
        return _unresolved(analysis, envelope, "the binding is not in the envelope")
    stated = analysis.execution_shape
    observables = binding.observables
    found = demands(analysis, envelope=cap, observables=observables)
    allowed_varies = cap.unit_varies
    uncombinable = sorted(
        {
            capability_name
            for _analysis_name, capability_name in observables
            if not cap.combinable(capability_name)
        }
    )
    campaign_supported = cap.campaigns

    def campaign_short(units: int, varies: Sequence[str]) -> list[str]:
        if not campaign_supported or units < 2:
            return ["no campaign is available"]
        if uncombinable:
            return [
                (
                    f"a campaign cannot combine {', '.join(uncombinable)}: "
                    f"{cap.ref} declares no rule for it"
                )
            ]
        return _fits(
            found,
            units=units,
            varies=varies,
            analysis=analysis,
            envelope=cap,
            observables=observables,
        )

    single_short = _fits(
        found,
        units=1,
        varies=(),
        analysis=analysis,
        envelope=cap,
        observables=observables,
    )
    single_fits = not single_short
    allowed_fits = campaign_supported and not campaign_short(cap.units, allowed_varies)
    min_units = next(
        (
            units
            for units in range(2, cap.units + 1)
            if not campaign_short(units, allowed_varies)
        ),
        None,
    )

    problems: list[str] = []
    # A claim is refused whatever else is true of the analysis: the envelope
    # is what the laboratory can do, and a model's number for it is not.
    if stated is not None and stated.capability and stated.capability != cap.ref:
        problems.append(
            f"the stated shape plans on {stated.capability}, and what the analysis "
            f"reads binds to {cap.ref}"
        )
    for name, claimed in sorted((stated.per_execution if stated else {}).items()):
        committed = _committed_capacity(cap, name, observables)
        if committed is not None and claimed > committed:
            problems.append(
                f"it claims one execution holds {claimed} {name}; the committed "
                f"envelope says {committed}"
            )
    rule = analysis.stopping_rule
    shape_units: int | None
    shape_varies: tuple[str, ...]
    if rule != CAMPAIGN_RULE:
        shape_units, shape_varies = 1, ()
        if not single_fits:
            problems.extend(
                f"one execution cannot hold it: {item}" for item in single_short
            )
    else:
        shape_units = stated.units if stated is not None else None
        shape_varies = (
            tuple(stated.unit_varies)
            if stated is not None and stated.unit_varies
            else allowed_varies
        )
        if not campaign_supported:
            problems.append(
                f"it fixes a campaign, and {cap.ref} "
                + (
                    "declares no campaign support"
                    if cap.capability.campaign is None
                    else f"may have only {cap.units} unit(s) under the human-set bounds"
                )
            )
        else:
            unattested = [item for item in shape_varies if item not in allowed_varies]
            if unattested:
                problems.append(
                    f"its units would differ in {', '.join(unattested)}, which "
                    f"{cap.ref} does not attest campaign units may differ in "
                    f"(allowed: {', '.join(allowed_varies)})"
                )
            if shape_units is not None and shape_units > cap.units:
                problems.append(
                    f"it fixes {shape_units} units and at most {cap.units} are "
                    f"allowed here"
                )
            if not problems:
                short = campaign_short(shape_units or cap.units, shape_varies)
                problems.extend(
                    f"the stated campaign cannot hold it: {item}" for item in short
                )

    if not problems:
        verdict = (
            ExecutionShapeVerdict.VALID_CAMPAIGN
            if rule == CAMPAIGN_RULE
            else ExecutionShapeVerdict.VALID_SINGLE_EXECUTION
        )
        suggestion = ""
    elif single_fits or allowed_fits:
        verdict = ExecutionShapeVerdict.EXECUTION_SHAPE_MISMATCH
        suggestion = _suggestion(
            cap=cap,
            single_fits=single_fits,
            min_units=min_units,
            allowed_varies=allowed_varies,
        )
    else:
        verdict = ExecutionShapeVerdict.CAPABILITY_LIMITED
        suggestion = (
            f"Neither one execution of {cap.ref} nor a campaign of up to "
            f"{cap.units} units can hold what this analysis requires. It is not "
            f"converted, weakened or frozen; a larger capability, or larger "
            f"human-set bounds, is a person's decision"
        )
    return ShapeCheck(
        verdict=verdict,
        envelope_digest=envelope.digest,
        commit=envelope.commit,
        capability=cap.ref,
        stopping_rule=rule,
        stated=stated.model_dump(mode="json") if stated is not None else None,
        demands=found,
        single_fits=single_fits,
        campaign_supported=campaign_supported,
        max_units=cap.units,
        unit_varies=allowed_varies,
        campaign_fits=allowed_fits,
        min_units=min_units,
        problems=tuple(problems),
        suggestion=suggestion,
        shape_units=shape_units,
        shape_varies=shape_varies,
    )


def _committed_capacity(
    cap: capabilities.CapabilityEnvelope,
    name: str,
    observables: Sequence[tuple[str, str]],
) -> int | None:
    """What the committed envelope says one execution holds of ``name``, if it says."""

    bound = cap.input_named(name)
    if bound is not None:
        return int(bound.max)
    for _analysis_name, capability_name in observables:
        if name == "records":
            limit = cap.records_max(capability_name)
            if limit is not None:
                return limit
        for item in cap.inputs_of(capability_name):
            if name in item.fields:
                return int(item.max)
    return None


def _suggestion(
    *,
    cap: capabilities.CapabilityEnvelope,
    single_fits: bool,
    min_units: int | None,
    allowed_varies: Sequence[str],
) -> str:
    """What another allowed shape is, in words -- offered, never applied."""

    options: list[str] = []
    if min_units is not None:
        options.append(
            f"stopping_rule fixed_campaign with {min_units} to {cap.units} units of "
            f"{cap.ref} differing in {' and/or '.join(allowed_varies)} can hold it"
        )
    if single_fits:
        options.append(
            f"one execution of {cap.ref} (fixed_single_execution) can hold it"
        )
    return (
        "Another execution shape the envelope allows can hold the same requirement: "
        + "; ".join(options)
        + ". The analysis author may revise how it is executed; Research OS does "
        "not convert it"
    )


# --------------------------------------------------------------- planning --
@dataclass(frozen=True, slots=True)
class Planning:
    """What the analysis author is shown of the laboratory, and how its proposal is checked.

    Built once per evidence stage from the committed manifest, the host's
    commands and the human-set bounds; nothing a model writes is an input.
    """

    envelope: capabilities.ExecutionEnvelope
    loaded: capabilities.LoadedManifest
    commands: Mapping[str, Any]

    def check(self, analysis: AnalysisSpec) -> ShapeCheck:
        from research_os.portfolio import sciencechain

        if not analysis.analysable:
            return _unresolved(
                analysis, self.envelope, "the analysis is not analysable"
            )
        resolution = capabilities.resolve(
            capabilities.Requirements(
                observables=sciencechain.requirements_from_analysis(analysis),
                max_seconds=int(self.envelope.bounds.max_execution_seconds),
            ),
            loaded=self.loaded,
            commands=self.commands,
        )
        return check(analysis, envelope=self.envelope, binding=resolution.binding)


def planning_for(
    loaded: capabilities.LoadedManifest | None,
    commands: Mapping[str, Any],
    bounds: Any,
) -> Planning | None:
    """The planning inputs for a governed project; ``None`` without a readable manifest.

    ``bounds`` is the portfolio's human-set bounds (``portfolio.yaml``).
    """

    envelope = capabilities.execution_envelope(loaded, commands, human_bounds(bounds))
    if envelope is None or loaded is None:
        return None
    return Planning(envelope=envelope, loaded=loaded, commands=commands)


def human_bounds(bounds: Any) -> capabilities.HumanBounds:
    return capabilities.HumanBounds(
        max_execution_seconds=int(bounds.max_experiment_seconds),
        max_campaign_units=int(bounds.max_campaign_units),
        max_campaign_seconds=int(bounds.max_campaign_seconds),
    )


# ----------------------------------------------------------- the revision --
def refused_lines(
    *, draft_record: Mapping[str, Any], proposal: Mapping[str, Any] | None
) -> list[str]:
    """What the author's revision is shown of its refused proposal: why, and what it was.

    The structured check, rendered, and the proposal as it was refused --
    the author's own output, thresholds included, since it is the author.
    """

    check_record = dict(draft_record)
    campaign = dict(check_record.get("campaign") or {})
    lines = [
        (
            f"YOUR PREVIOUS PROPOSAL FOR THIS IDEA VERSION WAS REFUSED BEFORE FREEZING "
            f"by Research OS's execution-shape check against envelope "
            f"{check_record.get('envelope_digest')}. Nothing was frozen and nothing "
            f"ran; the proposal below is not a commitment."
        ),
        f"verdict: {check_record.get('verdict')}",
        f"stopping rule you proposed: {check_record.get('stopping_rule')}",
    ]
    for item in check_record.get("problems") or ():
        lines.append(f"  - {item}")
    lines.append(
        f"one execution of {check_record.get('capability')}: "
        + (
            "holds it"
            if (check_record.get("single_execution") or {}).get("fits")
            else "cannot hold it"
        )
    )
    if campaign.get("supported"):
        lines.append(
            f"a campaign here: up to {campaign.get('max_units')} units differing in "
            f"{', '.join(campaign.get('unit_varies') or ())}; "
            + (
                f"the smallest that holds it has {campaign.get('min_units')} units"
                if campaign.get("min_units")
                else "none of them holds it"
            )
        )
    else:
        lines.append("a campaign: not available here")
    for item in check_record.get("demands") or ():
        lines.append(
            f"  need {item.get('needed')} "
            + (
                "records"
                if item.get("bound") == "records"
                else (
                    f"distinct {item.get('field')}"
                    if item.get("field")
                    else f"{item.get('bound')} (jointly)"
                )
            )
            + f" of {item.get('observable')}: one execution holds "
            f"{item.get('one_execution')}"
            + (
                f", a campaign of {campaign.get('max_units')} holds "
                f"{item.get('campaign')}"
                if item.get("campaign") is not None
                else ""
            )
        )
    if check_record.get("suggestion"):
        lines.append(str(check_record["suggestion"]))
    lines.append(
        "Revise HOW it is executed -- the stopping rule and execution_shape -- and "
        "keep what the data must show, which is the question's and not the "
        "envelope's. If you change anything else, say why in "
        "execution_shape.rationale. If you conclude the question cannot be "
        "answered within this envelope, say so: that is recorded, and a person "
        "decides. This is your one revision under this envelope."
    )
    if proposal is not None:
        lines.append("the proposal, as it was refused:")
        lines.extend(json.dumps(dict(proposal), indent=1, sort_keys=True).splitlines())
    return lines


def changed_fields(before: Mapping[str, Any], after: Mapping[str, Any]) -> list[str]:
    """Which top-level fields of an analysis a revision changed, by name."""

    return sorted(
        name
        for name in set(before) | set(after)
        if _key(before.get(name)) != _key(after.get(name))
    )


# ---------------------------------------------------------- the design --
def realisation_problems(
    shape_check: ShapeCheck,
    *,
    unit_differences: Sequence[PairDifference],
    unit_count: int,
    analysis: AnalysisSpec,
    envelope: capabilities.ExecutionEnvelope,
    binding_observables: Sequence[tuple[str, str]],
) -> list[str]:
    """Why a campaign design does not realise the shape its analysis was frozen with.

    ``unit_differences`` are, for every pair of units, *everything* the two
    differ in (``campaign.pair_differences``) -- not only what the
    capability attests, since a difference nothing attests is exactly the
    one that must not pass unseen. A stated number of units is exact; stated
    unit differences are the only ones, and every pair differing in anything
    else is reported, however much else it also differs in; and the campaign
    the design actually specifies -- this many units, differing in what they
    differ in -- must be one the envelope says could hold the frozen
    support. Nothing here relaxes what compiling the campaign checks.
    """

    if shape_check.verdict is not ExecutionShapeVerdict.VALID_CAMPAIGN:
        # Frozen before the check existed, or with nothing to check: the
        # compiler's own refusals are all there is, as before.
        return []
    problems: list[str] = []
    if shape_check.shape_units is not None and unit_count != shape_check.shape_units:
        problems.append(
            f"the frozen analysis fixes a campaign of {shape_check.shape_units} units "
            f"and the design has {unit_count}"
        )
    allowed = set(shape_check.shape_varies)
    cap = (
        envelope.capability(shape_check.capability) if shape_check.capability else None
    )
    attested = set(cap.unit_varies) if cap is not None else set()
    used: set[str] = set()
    for pair in unit_differences:
        differing = set(pair.tokens)
        used |= differing
        if not differing & attested:
            # The same execution, or one padded by what nothing attests: the
            # compiler refuses it, as CAPABILITY_LIMITED, whatever this says.
            continue
        if differing - allowed:
            problems.append(
                f"units {pair.first} and {pair.second} differ in "
                f"{pair.rendered(differing - allowed)}; the frozen analysis lets "
                f"them differ only in {', '.join(sorted(allowed))}"
            )
    if cap is not None and not problems:
        short = _fits(
            demands(analysis, envelope=cap, observables=binding_observables),
            units=unit_count,
            varies=sorted(used & allowed),
            analysis=analysis,
            envelope=cap,
            observables=binding_observables,
        )
        problems.extend(
            f"this campaign cannot hold the frozen support: {item}" for item in short
        )
    return problems


__all__ = [
    "MAX_REFUSED_DRAFTS",
    "Demand",
    "Planning",
    "ShapeCheck",
    "changed_fields",
    "check",
    "demands",
    "human_bounds",
    "planning_for",
    "realisation_problems",
    "refused_lines",
]
