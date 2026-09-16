"""What a proposal must contain, and what it may never rest on.

Two rules carry the weight of this whole subsystem.

**Grounding.** A proposal may cite only what was actually supplied to the worker
that wrote it: this project's capsule objects, this run's retrieved works, this
run's analyst findings. A Claim the project does not hold and a paper the run did
not retrieve are both refused, because a proposal that reaches outside its own
evidence is not a proposal about this project.

**Basis.** Every item says whether it was written before the answer was known.
That single field is what stops a description of finished work being promoted as
a preregistered prediction, which is the most effective way a pipeline can
manufacture confidence nobody earned.

The rest is completeness: a hypothesis nothing could contradict, an experiment
whose every outcome reads as confirmation, and a claim that answers nothing are
all refused while the worker can still produce something better.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from research_os.errors import ProposalValidationError
from research_os.proposal.models import (
    PROMOTION_TARGETS,
    ActionKind,
    EvidenceBasis,
    NextAction,
    ProposalGrounding,
    ProposalKind,
    ProposedItem,
    ResearchProposal,
    SuppliedFinding,
)
from research_os.proposal.planner import parse_proposal, validate_proposal
from tests.proposal_helpers import item, proposal_payload

GROUNDING = ProposalGrounding(
    capsule_ids=["Q-0001", "HYP-0001"],
    literature_keys=["doi:10.1000/widget"],
    finding_ids=["F-001"],
)

#: The text behind every id in ``GROUNDING.finding_ids``.
#:
#: The allowlist and the quoted findings have to name the same set -- see
#: ``ResearchProposal._everything_cited_was_supplied`` -- so a fixture that
#: allows ``F-001`` has to carry ``F-001``'s statement too. That is what a real
#: proposal looks like: ``ProposalController`` builds both from one tuple.
SUPPLIED = (
    SuppliedFinding(
        finding_id="F-001",
        kind="analysis",
        statement="The residuals grow with load, not with time.",
    ),
)


def parse(
    payload: dict,
    grounding: ProposalGrounding = GROUNDING,
    supplied: tuple[SuppliedFinding, ...] = SUPPLIED,
) -> ResearchProposal:
    return parse_proposal(
        structured=payload,
        text=None,
        proposal_id="PROP-20260912T101500Z-0a1b2c3d",
        project_path="/tmp/widget-study",
        project_id="widget-study",
        base_commit="a" * 40,
        goal="Settle whether deformation is linear",
        grounding=grounding,
        supplied_findings=supplied,
        provider="fake",
        model="fake-planner",
        invocation_id="INV-0001",
    )


# -- grounding ----------------------------------------------------------------


def test_a_well_grounded_proposal_validates() -> None:
    proposal = parse(proposal_payload())

    assert proposal.items[0].addresses == ["Q-0001"]
    assert proposal.human_checkpoints


def test_a_capsule_object_this_project_does_not_hold_cannot_be_addressed() -> None:
    payload = proposal_payload(items=[item(addresses=["CLAIM-9999"])])

    with pytest.raises(ProposalValidationError, match="not supplied"):
        parse(payload)


def test_a_paper_this_run_did_not_retrieve_cannot_ground_an_item() -> None:
    """A work recalled from training must not enter the record as retrieved."""

    payload = proposal_payload(
        items=[item(grounded_in_literature=["doi:10.1038/famous-paper"])]
    )

    with pytest.raises(ProposalValidationError, match="retrieved work"):
        parse(payload)


def test_an_analyst_finding_that_was_not_supplied_cannot_ground_an_item() -> None:
    payload = proposal_payload(items=[item(grounded_in_findings=["F-999"])])

    with pytest.raises(ProposalValidationError, match="analyst finding"):
        parse(payload)


def test_what_was_supplied_may_be_cited() -> None:
    payload = proposal_payload(
        items=[
            item(
                grounded_in_literature=["doi:10.1000/widget"],
                grounded_in_findings=["F-001"],
            )
        ]
    )

    proposal = parse(payload)

    assert proposal.items[0].grounded_in_literature == ["doi:10.1000/widget"]


def test_an_action_cannot_refer_to_an_item_that_is_not_in_the_proposal() -> None:
    payload = proposal_payload(
        next_actions=[
            {
                "action": "Do the thing",
                "kind": "experiment",
                "rationale": "because",
                "addresses_items": ["PR-404"],
                "requires_human": False,
            }
        ]
    )

    with pytest.raises(ProposalValidationError, match="not in this proposal"):
        parse(payload)


def test_an_uncertainty_cannot_block_an_item_that_does_not_exist() -> None:
    payload = proposal_payload(
        uncertainties=[
            {
                "statement": "unknown",
                "what_would_settle_it": "measurement",
                "blocks": ["PR-404"],
            }
        ]
    )

    with pytest.raises(ProposalValidationError, match="not in this proposal"):
        parse(payload)


# -- completeness -------------------------------------------------------------


def test_a_hypothesis_nothing_could_contradict_is_refused() -> None:
    payload = proposal_payload(items=[item(kind="hypothesis", falsification="")])

    with pytest.raises(ProposalValidationError, match="not a hypothesis"):
        parse(payload)


def test_an_experiment_with_no_decision_rule_is_refused() -> None:
    """Without one, any outcome can be read as confirmation."""

    payload = proposal_payload(
        items=[item(kind="experiment", decision_rule="", addresses=["HYP-0001"])]
    )

    with pytest.raises(ProposalValidationError, match="read as confirmation"):
        parse(payload)


def test_an_experiment_with_no_primary_metric_is_refused() -> None:
    payload = proposal_payload(
        items=[item(kind="experiment", primary_metrics=[], addresses=["HYP-0001"])]
    )

    with pytest.raises(ProposalValidationError, match="no primary metric"):
        parse(payload)


def test_a_prospective_experiment_must_state_its_expected_direction() -> None:
    payload = proposal_payload(
        items=[
            item(
                kind="experiment",
                basis="prospective",
                expected_direction="",
                addresses=["HYP-0001"],
            )
        ]
    )

    with pytest.raises(ProposalValidationError, match="not a prediction"):
        parse(payload)


def test_a_historical_experiment_needs_no_expected_direction() -> None:
    """It describes finished work; there was nothing to predict."""

    payload = proposal_payload(
        items=[
            item(
                kind="experiment",
                basis="historical",
                expected_direction="",
                addresses=["HYP-0001"],
            )
        ]
    )

    proposal = parse(payload)

    assert proposal.items[0].basis is EvidenceBasis.HISTORICAL


def test_a_claim_that_answers_nothing_is_refused() -> None:
    payload = proposal_payload(items=[item(kind="claim", addresses=[])])

    with pytest.raises(ProposalValidationError, match="addresses nothing"):
        parse(payload)


def test_an_experiment_that_discriminates_nothing_is_refused() -> None:
    """An experiment that could not change what is believed is not worth running.

    Checked in ``validate_proposal`` rather than on the item, because it is a
    property of the whole proposal: an experiment may name a hypothesis the
    project already holds, or one proposed alongside it.
    """

    payload = proposal_payload(items=[item(kind="experiment", addresses=[])])

    proposal = parse(payload)
    with pytest.raises(ProposalValidationError, match="addresses nothing"):
        validate_proposal(proposal)


def test_item_ids_must_be_sequential() -> None:
    payload = proposal_payload(items=[item(item_id="PR-001"), item(item_id="PR-007")])

    proposal = parse(payload)
    with pytest.raises(ProposalValidationError, match="sequential"):
        validate_proposal(proposal)


def test_repeated_item_ids_are_refused() -> None:
    payload = proposal_payload(items=[item(item_id="PR-001"), item(item_id="PR-001")])

    with pytest.raises(ProposalValidationError, match="must not repeat"):
        parse(payload)


def test_a_proposal_larger_than_a_human_will_read_is_refused() -> None:
    payload = proposal_payload(
        items=[item(item_id=f"PR-{index:03d}") for index in range(1, 21)]
    )

    proposal = parse(payload)
    with pytest.raises(ProposalValidationError, match="at most"):
        validate_proposal(proposal)


def test_output_that_is_not_json_is_refused_rather_than_interpreted() -> None:
    with pytest.raises(ProposalValidationError, match="no JSON object"):
        parse_proposal(
            structured=None,
            text="I think you should measure more widgets.",
            proposal_id="PROP-20260912T101500Z-0a1b2c3d",
            project_path="/tmp/x",
            project_id=None,
            base_commit=None,
            goal="a goal",
            grounding=GROUNDING,
            provider="fake",
            model=None,
            invocation_id=None,
        )


# -- the science boundary -----------------------------------------------------


def test_a_proposal_can_never_propose_a_review() -> None:
    """A Review is a human act, so it is not a kind anything here can produce."""

    assert "review" not in {item.value for item in ProposalKind}


def test_every_promotable_kind_targets_its_weakest_status() -> None:
    """Promotion produces a starting point, never a finished or accepted object."""

    assert PROMOTION_TARGETS[ProposalKind.QUESTION] == ("question", "open")
    assert PROMOTION_TARGETS[ProposalKind.HYPOTHESIS] == ("hypothesis", "draft")
    assert PROMOTION_TARGETS[ProposalKind.EXPERIMENT] == ("experiment", "draft")
    assert PROMOTION_TARGETS[ProposalKind.CLAIM] == ("claim", "draft")
    assert "accepted" not in {status for _, status in PROMOTION_TARGETS.values()}
    assert "review" not in {name for name, _ in PROMOTION_TARGETS.values()}


def test_an_evidence_interpretation_is_not_promotable() -> None:
    parsed = ProposedItem.model_validate(
        item(kind="evidence_interpretation", addresses=[])
    )

    assert parsed.promotable is False


def test_a_human_decision_must_say_it_needs_a_human() -> None:
    with pytest.raises(ValidationError, match="requires_human"):
        NextAction(
            action="Accept the claim",
            kind=ActionKind.HUMAN_DECISION,
            rationale="it is a scientific judgement",
            requires_human=False,
        )


# -- the allowlist and the quoted text name the same findings ----------------
def test_a_citable_finding_whose_text_is_absent_is_refused() -> None:
    """One direction of this was missing, justified by a caller that never was.

    The comment said a caller might supply ids whose text travels in a separate
    `analysis_data` block. `ProposalController` builds both from one tuple and
    nothing passes `analysis_data` to `build_proposal_prompt` at all. A
    proposal that permits a citation to an id whose statement it does not carry
    is one a person cannot audit: the statement lives in the operational
    database, which is rebuildable, and the proposal is the durable artifact.
    """

    with pytest.raises(ProposalValidationError, match="citable but not quoted"):
        parse(proposal_payload(), supplied=())


def test_quoting_a_finding_that_is_not_citable_is_refused() -> None:
    """The other direction: the prompt showed more than the validator accepts."""

    extra = SuppliedFinding(
        finding_id="F-999",
        kind="analysis",
        statement="something the allowlist never permitted",
    )
    with pytest.raises(ProposalValidationError, match="quoted but not citable"):
        parse(proposal_payload(), supplied=(*SUPPLIED, extra))


def test_a_proposal_with_no_findings_needs_none_quoted() -> None:
    """Most proposals ground in the capsule alone, and that stays legal."""

    grounding = ProposalGrounding(
        capsule_ids=["Q-0001", "HYP-0001"],
        literature_keys=["doi:10.1000/widget"],
        finding_ids=[],
    )
    proposal = parse(
        proposal_payload(items=[item(grounded_in_findings=[])]),
        grounding=grounding,
        supplied=(),
    )
    assert proposal.supplied_findings == []
