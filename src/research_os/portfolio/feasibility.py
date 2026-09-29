"""Whether this laboratory could answer an idea's empirical question: planning metadata.

The first final qualification spent the portfolio's scarce advancement work on
two directions its only capability could not answer -- one needed a past
study's own records, one a sample larger than a single execution could hold --
and learned it only at the evidence stage, from the analysis designer. This is
the cheap signal that lets the allocator tell, between otherwise equally
strong directions, which one the declared capabilities can actually test.

    CURRENTLY_EXECUTABLE              one execution of a declared capability
    LIKELY_EXECUTABLE_WITH_CAMPAIGN   a declared capability, several executions
    CAPABILITY_LIMITED                something it needs no capability produces
    UNKNOWN                           not yet classified, or not a measurement

**Derived, never declared.** The trusted side is the science repository's
*committed* capability manifest (``research_os.capability``); the other side
is the idea's structured requirement (``contracts.EvidenceNeeds``), which the
sharpening stage states against the catalogue it is shown -- or, once one is
frozen, the capability-limited state of the idea's own contract. The match is
ordinary code. A model's words cannot make a field exist, and nothing here
changes what an idea says, freezes anything, or rejects anything: an idea that
is capability-limited today stays in the bank, keeps exploring, and is
advanced when nothing executable of comparable value is waiting
(``allocation.utility``).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from research_os.capability import LoadedManifest, check_against_command
from research_os.portfolio.models import AdjudicationType, Feasibility


@dataclass(frozen=True, slots=True)
class Assessment:
    signal: Feasibility
    basis: str
    capability: str | None = None


def _declared_names(capability: Any) -> set[str]:
    names: set[str] = set()
    for observable in capability.observables:
        if observable.kind == "scalar":
            names.add(observable.name)
        for entry in observable.fields:
            names.add(entry.name)
    return names


def assess(
    needs: Mapping[str, Any] | None,
    *,
    loaded: LoadedManifest | None,
    commands: Mapping[str, Any],
    adjudication: Sequence[AdjudicationType | str] = (),
    blocked_on_capability: bool = False,
) -> Assessment:
    """The signal for one idea version. Deterministic; reads nothing but its arguments.

    ``blocked_on_capability`` is the idea version's own contract refused by
    capability resolution -- the definitive answer, once there is one.
    """

    if blocked_on_capability:
        return Assessment(
            Feasibility.CAPABILITY_LIMITED,
            "its frozen analysis was refused by capability resolution: no declared "
            "capability produces what it reads",
        )
    kinds = {str(item) for item in adjudication}
    if kinds and str(AdjudicationType.EMPIRICAL) not in kinds:
        return Assessment(Feasibility.UNKNOWN, "it is not settled by a measurement")
    if loaded is None:
        return Assessment(
            Feasibility.UNKNOWN,
            "the repository declares no capability manifest to match it against",
        )
    if not needs:
        return Assessment(
            Feasibility.UNKNOWN, "it states no structured evidence requirement yet"
        )
    data = str(needs.get("data") or "new_execution")
    if data == "none":
        return Assessment(Feasibility.UNKNOWN, "it says it needs no measurement")
    if data == "existing_records":
        return Assessment(
            Feasibility.CAPABILITY_LIMITED,
            "it needs records that already exist; every declared capability "
            "produces new executions, and none declares those records",
        )
    wanted = [str(item) for item in needs.get("fields") or ()]
    draws = max(1, int(needs.get("independent_draws") or 1))
    usable = [
        item
        for item in sorted(loaded.manifest.capabilities, key=lambda cap: cap.id)
        if not check_against_command(item, commands)
    ]
    if not usable:
        return Assessment(
            Feasibility.CAPABILITY_LIMITED,
            "no declared capability is runnable with the host's declared commands",
        )
    best_missing: list[str] | None = None
    complete: list[Any] = []
    for capability in usable:
        missing = [item for item in wanted if item not in _declared_names(capability)]
        if missing:
            if best_missing is None or len(missing) < len(best_missing):
                best_missing = missing
            continue
        complete.append(capability)
    if complete and draws <= 1:
        return Assessment(
            Feasibility.CURRENTLY_EXECUTABLE,
            f"{complete[0].ref} declares every field it needs",
            complete[0].ref,
        )
    for capability in complete:
        if capability.campaign is not None:
            return Assessment(
                Feasibility.LIKELY_EXECUTABLE_WITH_CAMPAIGN,
                f"{capability.ref} declares every field it needs, and a campaign of "
                f"up to {capability.campaign.max_units} executions can hold "
                f"{draws} independent draws",
                capability.ref,
            )
    if complete:
        return Assessment(
            Feasibility.UNKNOWN,
            f"{complete[0].ref} declares every field it needs; whether one execution "
            f"holds {draws} independent draws is for the design to show",
            complete[0].ref,
        )
    return Assessment(
        Feasibility.CAPABILITY_LIMITED,
        "no declared capability reports "
        + ", ".join(best_missing or wanted)
        + "; the catalogue names every observable that exists",
    )


__all__ = ["Assessment", "assess"]
