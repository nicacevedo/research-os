"""Nominating one project's finding as candidate knowledge for others.

The second of the two actions that had a policy and no handler. Like
``propose_capsule_change`` it is thin, and for the same reason: the v1 insight
subsystem already holds the whole authority-preserving path, and it has one
property that makes it worth reusing rather than reimplementing.

**Nomination and promotion are different types in different stores.** An
:class:`~research_os.insights.models.InsightNomination` is runtime state a
researcher can delete without consequence. A
:class:`~research_os.insights.models.PromotedInsight` is what other projects'
workers will read. The gap between them cannot be closed by setting a field,
because they are not the same object -- which is exactly the shape invariant 15
needs, and the reason this handler needs no guard of its own to avoid crossing
it.

**What the runtime deliberately cannot supply.** A promoted insight requires
``scope``, ``assumptions`` and ``applicability``, all non-empty. This handler
leaves all three blank. That is not an omission to fill in later: those three
fields *are* the judgement that a finding transfers, and
:attr:`InsightNomination.missing_for_promotion` therefore tells the researcher
exactly what they must write before the nomination can become knowledge. A
runtime that filled them in would have decided that a true-in-one-place finding
is general, which is the entire risk of cross-project transfer.

**Saying no is the common answer.** The nominator prompt is asked whether the
finding is worth a person's attention at all, and ``worth_nominating: false``
ends the action successfully with nothing written. Most findings are about the
project that produced them. A nomination nobody should have made costs a
researcher's attention, which is the scarcest thing in the system.

**Replay safety.** ``make_nomination_id`` is deterministic in title, project and
*second*, and a retry does not reproduce the second, so a crash between writing
the nomination and recording it would leave two nominations for one judgement.
The id is reserved from the cycle's own identity instead, and the reconciler
asks the nomination store whether that file exists.
"""

from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.actions.proposals import packet_for
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.findings import FindingPacket
from research_os.runtime.idempotency import idempotency_key
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.prompts import NOMINATOR
from research_os.runtime.routing import RoutingError

LOG = logging.getLogger("research_os.runtime.actions.insights")


def reservation_key_for(state: Mapping[str, Any]) -> str:
    """This cycle's one nomination, named by the cycle and nothing per-attempt."""

    return f"nominate_insight:{state['run_id']}:{state['cycle_index']}"


def reserved_nomination_id(*, reservation_key: str) -> str:
    """The nomination id one reservation key always produces.

    A fixed, obviously-not-a-real-time stamp, because the id has to match
    ``NOMINATION_ID_RE`` and has to be recomputable by a retry that happens
    later. ``created_at`` on the nomination records the real time.
    """

    digest = hashlib.sha256(
        f"reserved-nomination-v1\n{reservation_key}".encode()
    ).hexdigest()
    return f"NOM-19700101T000000Z-{digest[:8]}"


def nomination_digest(*, project_id: str, statement: str, packet: FindingPacket) -> str:
    """A stable hash of what is being nominated and what it rests on.

    Deduplicates the deterministic repeat §10 asks about: a later cycle that
    reaches the same judgement about the same findings should not put a second
    identical nomination in front of a person. Over the statement and the
    grounding rather than the title, because a reworded title is the same
    nomination and a different grounding is not.
    """

    material = json.dumps(
        {
            "v": 1,
            "project_id": project_id,
            "statement": " ".join(statement.split()),
            "findings": sorted(packet.ids),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


def _existing_duplicate(
    *, project_id: str, digest: str, context: CycleContext
) -> Any | None:
    """A pending nomination of this project that says the same thing.

    Checked against the *store*, across cycles, because the repeat this guards
    against is a later cycle reaching the same conclusion -- not a replay, which
    the reserved id already handles.

    Only pending ones. A nomination the researcher declined should not be
    silently re-offered, and one they promoted is knowledge now; both are
    answered, and re-nominating either would be arguing with a decision.
    """

    from research_os.insights.store import NominationStore

    del context
    try:
        existing = NominationStore().all()
    except ResearchOSError as exc:
        LOG.debug("could not read the nomination store: %s", exc)
        return None
    for nomination in existing:
        if nomination.source.project_id != project_id:
            continue
        if (nomination.source.detail or "").endswith(digest):
            return nomination
    return None


def _reconcile_nomination(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """Did a previous attempt already write this cycle's nomination?

    Re-derives the reserved id from the cycle's identity and asks the store.
    ``None`` means the file is not there, so the effect did not take hold.
    """

    from research_os.insights.store import NominationStore

    del plan
    reserved = reserved_nomination_id(reservation_key=reservation_key_for(state))
    try:
        nomination = NominationStore().load(reserved)
    except ResearchOSError:
        return None
    return {
        "ok": True,
        "detail": f"recovered nomination {reserved} from an earlier attempt",
        "data": _payload(
            nomination,
            packet=FindingPacket(findings=context.store.nomination_findings(reserved)),
            recovered=True,
        ),
    }


def _payload(
    nomination: Any, *, packet: FindingPacket, recovered: bool = False
) -> dict[str, Any]:
    return {
        "nominated": True,
        "recovered": recovered,
        "nomination_id": nomination.nomination_id,
        "title": nomination.title,
        "promotion_type": str(nomination.promotion_type),
        "grounded_in_findings": list(packet.ids),
        "finding_packet_digest": packet.digest,
        # Said plainly in the data the run report reads. A nomination is not
        # knowledge, and the one thing a reader must not conclude from its
        # existence is that another project may now rely on it.
        "promoted": False,
        "requires_human_promotion": True,
        "missing_before_promotion": list(nomination.missing_for_promotion),
        "follow_up": (
            f"researchctl insight show-nomination {nomination.nomination_id}"
            "  # then `insight promote` if you decide it transfers"
        ),
    }


def nominate_insight(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Offer one finding to a person as candidate cross-project knowledge.

    Writes a nomination and nothing else. No insight is promoted, no other
    project is touched, and the three fields that would make it knowledge are
    deliberately left for the researcher.
    """

    from research_os.insights.models import (
        InsightNomination,
        PromotionType,
        SourceReference,
    )
    from research_os.insights.store import NominationStore

    project_id = str(state["project_id"])
    try:
        packet = packet_for(state, context, plan)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"the findings this nomination would rest on are not available: {exc}",
            failure_class=FailureClass.POLICY_REFUSED,
        )
    if not packet.findings:
        return ActionOutcome.succeeded(
            "this project has no runtime findings to nominate; nothing was offered",
            data={"nominated": False, "reason": "no findings"},
        )

    reservation_key = reservation_key_for(state)
    nomination_id = reserved_nomination_id(reservation_key=reservation_key)
    key = idempotency_key(
        "insight.nominate", state["run_id"], state["cycle_index"], reservation_key
    )

    def perform() -> dict[str, Any]:
        prompt = NOMINATOR.render(
            fields={"objective": str(state["objective"]), "project_id": project_id},
            blocks={
                "findings": [
                    json.dumps(entry, indent=2, sort_keys=True)
                    for entry in packet.rendered()
                ],
                "frontier": [
                    json.dumps(state.get("frontier", {}), indent=2, sort_keys=True)
                ],
            },
        )
        response = context.models.complete(
            ModelRequest(
                role=NOMINATOR.role,
                capability=NOMINATOR.capability,
                prompt=prompt,
                prompt_version=NOMINATOR.identity,
                criticality=NOMINATOR.criticality,
                independence=NOMINATOR.independence,
                independence_group=f"nominate:{state['run_id']}",
                json_schema=NOMINATOR.output_schema,
            )
        )
        if not response.ok or response.structured is None:
            raise ResearchOSError(
                f"the nominator returned no usable answer: {response.error}"
            )
        answer = dict(response.structured)
        if not bool(answer.get("worth_nominating")):
            return {
                "ok": True,
                "detail": (
                    "nothing here is worth another project's attention: "
                    + str(answer.get("why_it_might_not_transfer") or "no reason given")
                ),
                "data": {
                    "nominated": False,
                    "reason": "the nominator judged it does not transfer",
                    "why_it_might_not_transfer": str(
                        answer.get("why_it_might_not_transfer") or ""
                    ),
                    "grounded_in_findings": list(packet.ids),
                },
            }

        statement = str(answer.get("statement") or "").strip()
        digest = nomination_digest(
            project_id=project_id, statement=statement, packet=packet
        )
        duplicate = _existing_duplicate(
            project_id=project_id, digest=digest, context=context
        )
        if duplicate is not None:
            return {
                "ok": True,
                "detail": (
                    f"an equivalent nomination is already waiting for you: "
                    f"{duplicate.nomination_id}"
                ),
                "data": _payload(duplicate, packet=packet, recovered=True),
            }

        try:
            promotion_type = PromotionType(str(answer.get("promotion_type")))
        except ValueError as exc:
            raise ResearchOSError(
                f"the nominator named a kind of knowledge this build has no "
                f"category for: {exc}"
            ) from exc

        nomination = InsightNomination(
            nomination_id=nomination_id,
            title=str(answer.get("title") or "").strip() or statement[:60],
            statement=statement,
            promotion_type=promotion_type,
            source=SourceReference(
                project_id=project_id,
                project_path=str(state["repo_path"]),
                run_id=str(state["run_id"]),
                # The lineage §10 asks to bind, in the one free-text field the
                # v1 model has. The digest is last so `_existing_duplicate` can
                # match on a suffix, and every other part is there so a person
                # reading the nomination file alone can find what produced it.
                detail=(
                    f"runtime cycle {state['cycle_index']} of run "
                    f"{state['run_id']}; grounded in "
                    f"{', '.join(packet.ids)}; digest {digest}"
                ),
            ),
            # Deliberately empty. These three fields are the judgement that this
            # transfers, and `missing_for_promotion` will tell the researcher
            # that they are what is missing.
            scope="",
            assumptions=[],
            applicability="",
            rationale=str(answer.get("rationale") or "").strip()
            or "the nominator gave no rationale",
            nominated_by=f"runtime:{NOMINATOR.identity}",
        )
        NominationStore().write(nomination)
        context.store.link_nomination_findings(
            nomination_id=nomination.nomination_id,
            finding_ids=packet.ids,
            project_id=project_id,
            run_id=str(state["run_id"]),
        )
        return {
            "ok": True,
            "detail": (
                f"nomination {nomination.nomination_id} is waiting for your "
                f"decision about whether it transfers"
            ),
            "data": _payload(nomination, packet=packet),
        }

    try:
        outcome = context.ledger.run(
            key=key,
            kind="insight.nominate",
            run_id=str(state["run_id"]),
            request={"nomination_id": nomination_id, "finding_ids": list(packet.ids)},
            perform=perform,
            reconcile=lambda _invocation: _reconcile_nomination(state, context, plan),
        )
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.BUDGET_EXHAUSTED
        )
    except RoutingError as exc:
        return ActionOutcome.failed(
            str(exc), failure_class=FailureClass.PROVIDER_UNAVAILABLE
        )
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"the nomination could not be produced: {exc}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    result = outcome.result
    return ActionOutcome.succeeded(
        str(result.get("detail") or "nothing was nominated"),
        data=dict(result.get("data") or {}),
    )


def reconcile_reserved_nomination(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> dict[str, Any] | None:
    """The registry's reconciler. Re-derives the id; trusts no stashed state."""

    return _reconcile_nomination(state, context, plan)


__all__ = [
    "nominate_insight",
    "nomination_digest",
    "reconcile_reserved_nomination",
    "reservation_key_for",
    "reserved_nomination_id",
]
