"""Writing and refereeing, with the author's authority deliberately constrained.

The rule this module exists to enforce, stated as narrowly as it can be:

> The author may transform supported scientific state into prose. It may not
> invent unsupported science.

"Supported" is not the author's judgement and not the runtime's. It is
:func:`research_os.validate.claim_approval`, reached through
:meth:`ScientificKernelAdapter.quotable_claims`, which requires both that the
file says ``accepted`` *and* that a qualifying human Review still stands against
the current digests. A claim whose evidence changed after its review was
recorded is not quotable, and the author is not told it is.

The provenance chain the drafting step is built to preserve:

```text
manuscript statement -> claim id -> evidence object -> experiment or literature
source -> immutable artifact or stable citation
```

Each arrow is checkable. :func:`audit_citations` walks it deterministically --
no model -- and reports every statement whose cited claim is not quotable and
every quotable claim the draft asserts without citing. A model is not asked
whether a citation is valid; that is a lookup.

The referee is a separate model, in a separate independence group, given the
draft and the evidence and nothing else. It cannot approve anything: acceptance
of the underlying claims already happened, or did not, and refereeing prose does
not change it.
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Mapping
from typing import Any

from research_os.errors import ResearchOSError
from research_os.runtime.actions.base import ActionOutcome, latest_artifact
from research_os.runtime.budgets import BudgetExhaustedError
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ModelRequest
from research_os.runtime.prompts import AUTHOR, REFEREE

LOG = logging.getLogger("research_os.runtime.actions.authoring")

#: Claim ids as the kernel writes them. Used to find citations in prose.
CLAIM_REF = re.compile(r"\bCLAIM-[0-9]{4,}\b")


def _evidence_lines(context: CycleContext, claim_ids: tuple[str, ...]) -> list[str]:
    """Render each quotable claim and the evidence behind it, for the prompt.

    Read through the kernel adapter. The author sees the statements and the
    evidence ids; it does not see the capsule files and cannot write them.
    """

    lines: list[str] = []
    for claim_id in claim_ids:
        try:
            claim = context.kernel.object(claim_id)
        except ResearchOSError as exc:
            LOG.debug("could not read %s: %s", claim_id, exc)
            continue
        statement = getattr(claim, "statement", "") or getattr(claim, "title", "")
        supporting = ", ".join(
            str(item) for item in getattr(claim, "supporting_evidence", ())
        )
        contrary = ", ".join(
            str(item) for item in getattr(claim, "contrary_evidence", ())
        )
        lines.append(f"{claim_id}: {statement}")
        lines.append(f"  supported by: {supporting or '(none)'}")
        if contrary:
            lines.append(f"  contrary evidence: {contrary}")
    return lines


def draft_manuscript(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Draft one section from claims the kernel says are quotable.

    Refuses to draft at all when nothing is quotable. That is not an obstacle to
    work around: a section written from unaccepted claims is a section whose
    statements are not supported, and producing one so that there is something
    to show is precisely the failure this whole apparatus exists to prevent.
    """

    section = str(plan.get("parameters", {}).get("section") or "results").strip()
    quotable = context.kernel.quotable_claims()
    if not quotable:
        return ActionOutcome.succeeded(
            "nothing is quotable yet: no claim has a qualifying human review "
            "standing against its current digests, so there is nothing to write up",
            data={"section": section, "quotable_claims": [], "drafted": False},
        )

    prompt = AUTHOR.render(
        fields={"section": section, "quotable_claim_ids": ", ".join(quotable)},
        blocks={"evidence": _evidence_lines(context, quotable)},
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=AUTHOR.role,
                capability=AUTHOR.capability,
                prompt=prompt,
                prompt_version=AUTHOR.identity,
                criticality=AUTHOR.criticality,
                independence=AUTHOR.independence,
                independence_group=f"write:{state['run_id']}:{section}",
                json_schema=AUTHOR.output_schema,
            )
        )
    # Budget only; a `RoutingError` propagates. See the other eight sites: an
    # `ActionOutcome` cannot carry the breaker's deadline or the fact that no
    # invocation happened, so an outage caught here is rescheduled by the
    # linear backoff alone and charged an attempt it never spent.
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            f"the author did not run: {exc}",
            failure_class=FailureClass.BUDGET_EXHAUSTED,
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.failed(
            f"the author returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    draft = dict(response.structured)
    markdown = str(draft.get("markdown", ""))
    ref = context.artifacts.put_text(
        markdown,
        media_type="text/markdown",
        role=f"draft:{section}",
        producer=f"{response.provider}:{AUTHOR.identity}",
    )
    audit = _audit(
        markdown, cited=tuple(draft.get("cited_claims", ())), quotable=quotable
    )
    return ActionOutcome.succeeded(
        f"drafted {section} from {len(quotable)} quotable claim(s)"
        + (
            f"; {len(audit['unsupported'])} unsupported citation(s)"
            if audit["unsupported"]
            else ""
        ),
        data={
            "section": section,
            "drafted": True,
            "quotable_claims": list(quotable),
            "cited_claims": list(draft.get("cited_claims", ())),
            "gaps": list(draft.get("gaps", ())),
            "draft_artifact": ref.artifact_id,
            "audit": audit,
        },
        artifacts=(ref,),
    )


def _audit(
    markdown: str, *, cited: tuple[str, ...], quotable: tuple[str, ...]
) -> dict[str, Any]:
    """Check the citations in a draft against what is quotable. No model.

    Three distinct findings, because they call for different responses:

    - ``unsupported``: a claim the prose cites that is not quotable. The
      statement has to go or the claim has to be accepted by a person.
    - ``undeclared``: a claim cited in the prose but absent from the model's own
      ``cited_claims`` list. Its bookkeeping and its text disagree.
    - ``unused``: quotable claims the draft never mentions. Not an error -- a
      section need not cite everything -- but worth reporting.
    """

    in_prose = set(CLAIM_REF.findall(markdown))
    declared = {str(item) for item in cited}
    allowed = set(quotable)
    return {
        "in_prose": sorted(in_prose),
        "declared": sorted(declared),
        "unsupported": sorted((in_prose | declared) - allowed),
        "undeclared": sorted(in_prose - declared),
        "unused": sorted(allowed - in_prose),
    }


def audit_citations(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Walk the provenance chain for every claim a draft cites. Deterministic.

    Asking a model whether a citation is valid would be asking it to do a
    lookup, and it would sometimes get the lookup wrong in the direction that
    makes the draft look better.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    artifact_id = str(
        plan.get("parameters", {}).get("draft_artifact")
        or previous.get("draft_artifact")
        # The durable record, because graph state does not cross a cycle
        # boundary and the draft was almost certainly written in an earlier one.
        or latest_artifact(
            context, role_prefix="draft:", project_id=state["project_id"]
        )
        or ""
    )
    if not artifact_id:
        return ActionOutcome.succeeded("no draft to audit", data={"audited": False})

    try:
        markdown = context.artifacts.get_text(artifact_id)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not read the draft: {exc}",
            failure_class=FailureClass.ARTIFACT_MISSING,
        )

    quotable = context.kernel.quotable_claims()
    audit = _audit(
        markdown, cited=tuple(previous.get("cited_claims", ())), quotable=quotable
    )

    # Walk the chain one more step for the claims that survived: a quotable
    # claim must reference qualifying evidence, and the kernel's own approval
    # check is what says so.
    chain: dict[str, Any] = {}
    for claim_id in audit["in_prose"]:
        if claim_id not in quotable:
            continue
        approval = context.kernel.approval(claim_id)
        claim = context.kernel.object(claim_id)
        chain[claim_id] = {
            "approval_stands": approval.qualifies,
            "review": str(getattr(approval.review, "id", ""))
            if approval.review
            else None,
            "supporting_evidence": [
                str(item) for item in getattr(claim, "supporting_evidence", ())
            ],
        }

    clean = not audit["unsupported"] and not audit["undeclared"]
    return ActionOutcome.succeeded(
        "every citation resolves to a standing approval"
        if clean
        else f"{len(audit['unsupported'])} unsupported and "
        f"{len(audit['undeclared'])} undeclared citation(s)",
        data={"audited": True, "audit": audit, "chain": chain, "clean": clean},
    )


def referee_manuscript(
    state: Mapping[str, Any], context: CycleContext, plan: Mapping[str, Any]
) -> ActionOutcome:
    """Referee the draft independently: a different family where one exists.

    Given the draft and the evidence, and nothing else -- not the author's
    reasoning, not the planner's rationale. Its verdict is advice recorded as
    provenance; it approves nothing.
    """

    previous = dict(state.get("action_result", {}).get("data") or {})
    artifact_id = str(
        plan.get("parameters", {}).get("draft_artifact")
        or previous.get("draft_artifact")
        or latest_artifact(
            context, role_prefix="draft:", project_id=state["project_id"]
        )
        or ""
    )
    if not artifact_id:
        return ActionOutcome.succeeded("no draft to referee", data={"refereed": False})
    try:
        markdown = context.artifacts.get_text(artifact_id)
    except ResearchOSError as exc:
        return ActionOutcome.failed(
            f"could not read the draft: {exc}",
            failure_class=FailureClass.ARTIFACT_MISSING,
        )

    section = str(previous.get("section") or "the manuscript")
    prompt = REFEREE.render(
        fields={"section": section},
        blocks={
            "manuscript": [markdown],
            "evidence": _evidence_lines(context, context.kernel.quotable_claims()),
        },
    )
    try:
        response = context.models.complete(
            ModelRequest(
                role=REFEREE.role,
                capability=REFEREE.capability,
                prompt=prompt,
                prompt_version=REFEREE.identity,
                criticality=REFEREE.criticality,
                independence=REFEREE.independence,
                independence_group=f"write:{state['run_id']}:{section}",
                json_schema=REFEREE.output_schema,
            )
        )
    # Budget only; a `RoutingError` propagates. See the other eight sites: an
    # `ActionOutcome` cannot carry the breaker's deadline or the fact that no
    # invocation happened, so an outage caught here is rescheduled by the
    # linear backoff alone and charged an attempt it never spent.
    except BudgetExhaustedError as exc:
        return ActionOutcome.failed(
            f"the referee did not run: {exc}",
            failure_class=FailureClass.BUDGET_EXHAUSTED,
        )
    if not response.ok or response.structured is None:
        return ActionOutcome.failed(
            f"the referee returned nothing usable: {response.error}",
            failure_class=FailureClass.MODEL_OUTPUT_INVALID,
        )

    verdict = dict(response.structured)
    ref = context.artifacts.put_text(
        json.dumps(verdict, indent=2, sort_keys=True),
        media_type="application/json",
        role="referee_report",
        producer=f"{response.provider}:{REFEREE.identity}",
    )
    return ActionOutcome.succeeded(
        f"referee: {verdict.get('verdict')} ({response.independence})",
        data={
            "refereed": True,
            "verdict": verdict.get("verdict"),
            "claim_support": verdict.get("claim_support"),
            "overclaiming": verdict.get("overclaiming", []),
            "required_changes": verdict.get("required_changes", []),
            "independence": str(response.independence),
            "independence_note": response.independence_note,
        },
        artifacts=(ref,),
    )
