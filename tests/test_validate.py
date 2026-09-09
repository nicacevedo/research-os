"""Cross-object validation tests for Research Capsule scientific state."""

from __future__ import annotations

import pytest

from research_os.digests import subject_digest
from research_os.errors import (
    E_ACCEPTED_WITHOUT_EVIDENCE,
    E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
    E_CREATED_FROM_CYCLE,
    E_DANGLING_REF,
    E_DUP_ID,
    E_EVIDENCE_DIGEST_UNLINKED,
    E_EVIDENCE_DIGESTS_INCOMPLETE,
    E_EXPERIMENT_DIGEST_UNLINKED,
    E_EXPERIMENT_DIGESTS_INCOMPLETE,
    E_NONQUALIFYING_EVIDENCE,
    E_STALE_REVIEW_DIGEST,
    E_SUPERSEDED_WITHOUT_SUCCESSOR,
    E_SUPERSEDES_NON_SUPERSEDED,
    E_SUPERSESSION_CYCLE,
    E_WITHDRAWN_SUPERSEDED,
    E_WRONG_REF_TYPE,
    W_PROMOTED_WITHOUT_HYPOTHESIS,
    W_STALE_EVIDENCE_DIGEST,
    W_STALE_EXPERIMENT_DIGEST,
    W_STALE_SUBJECT_DIGEST,
    InvalidIdError,
    ValidationReport,
)
from research_os.models import Experiment, ScientificObject
from research_os.validate import referenced_experiments, validate_objects
from tests.helpers import (
    OTHER_PROJECT_ID,
    TEST_PROJECT_ID,
    make_claim,
    make_evidence,
    make_experiment,
    make_hypothesis,
    make_idea,
    make_question,
    make_review,
)


def _report(
    objects: list[ScientificObject],
    *,
    project_id: str = TEST_PROJECT_ID,
) -> ValidationReport:
    return validate_objects(objects, project_id=project_id)


def _codes(objects: list[ScientificObject]) -> set[str]:
    return set(_report(objects).codes())


def _accepted_bundle(
    *,
    claim_status: str = "accepted",
    reviewer_kind: str = "human",
    verdict: str = "approve",
    review_status: str = "concluded",
    evidence_status: str = "active",
    statement: str = "X is supported.",
    digest: str | None = None,
    evidence_digests: dict[str, str] | None = None,
    experiment_digests: dict[str, str] | None = None,
    project_id: str = TEST_PROJECT_ID,
) -> list[ScientificObject]:
    evidence = make_evidence(status=evidence_status)
    claim = make_claim(
        status=claim_status,
        supporting_evidence=["EVI-0001"],
        statement=statement,
    )
    subject_digest_value = (
        digest if digest is not None else subject_digest(claim, project_id=project_id)
    )
    if evidence_digests is None:
        evidence_digests = {
            evidence.id: subject_digest(evidence, project_id=project_id)
        }
    review = make_review(
        status=review_status,
        subject=claim.id,
        reviewer_kind=reviewer_kind,
        verdict=verdict,
        findings="Reviewed.",
        subject_digest=subject_digest_value,
        evidence_digests=evidence_digests,
        experiment_digests=experiment_digests,
    )
    return [evidence, claim, review]


def test_duplicate_ids_are_errors() -> None:
    report = _report([make_question(), make_question(title="Other")])
    assert E_DUP_ID in report.codes()
    assert not report.ok


def test_duplicate_ids_do_not_use_first_object_wins() -> None:
    unique = make_question(id="Q-0002")
    dup_a = make_question(id="Q-0001", title="First payload", created_from=["Q-0002"])
    dup_b = make_question(id="Q-0001", title="Second payload", created_from=["Q-9999"])
    first = _report([dup_a, dup_b, unique])
    second = _report([dup_b, dup_a, unique])
    third = _report([unique, dup_b, dup_a])
    assert first.findings == second.findings == third.findings
    assert first.codes() == (E_DUP_ID,)
    assert E_DANGLING_REF not in first.codes()


def test_dangling_and_wrong_type_refs() -> None:
    dangling = _report([make_claim(status="draft", supporting_evidence=["EVI-9999"])])
    assert E_DANGLING_REF in dangling.codes()
    wrong = _report(
        [
            make_hypothesis(addresses=["EVI-0001"]),
            make_evidence(),
        ]
    )
    assert E_WRONG_REF_TYPE in wrong.codes()
    assert E_DANGLING_REF not in wrong.codes()


def test_created_from_cycles() -> None:
    first = make_question(id="Q-0001", created_from=["Q-0002"])
    second = make_question(id="Q-0002", created_from=["Q-0001"])
    assert E_CREATED_FROM_CYCLE in _codes([first, second])
    self_cycle = make_question(created_from=["Q-0001"])
    assert E_CREATED_FROM_CYCLE in _codes([self_cycle])


def test_withdrawn_evidence_does_not_qualify() -> None:
    objects = _accepted_bundle(
        claim_status="evidence_linked", evidence_status="withdrawn"
    )
    objects = [obj for obj in objects if obj.id != "REV-0001"]
    assert E_NONQUALIFYING_EVIDENCE in _codes(objects)
    superseded = _accepted_bundle(
        claim_status="evidence_linked", evidence_status="superseded"
    )
    superseded = [obj for obj in superseded if obj.id != "REV-0001"] + [
        make_evidence(id="EVI-0002", status="active")
    ]
    # claim still points only at superseded EVI-0001
    assert E_NONQUALIFYING_EVIDENCE in _codes(superseded)


def test_extra_withdrawn_evidence_is_ok_if_one_qualifies() -> None:
    evidence_active = make_evidence(id="EVI-0001", status="active")
    evidence_withdrawn = make_evidence(id="EVI-0002", status="withdrawn")
    claim = make_claim(
        status="evidence_linked",
        supporting_evidence=["EVI-0001", "EVI-0002"],
    )
    report = _report([evidence_active, evidence_withdrawn, claim])
    assert E_NONQUALIFYING_EVIDENCE not in report.codes()
    assert report.ok


def test_experiment_evidence_requires_completed_experiment() -> None:
    hyp = make_hypothesis()
    running = make_experiment(status="running")
    failed = make_experiment(id="EXP-0002", status="failed")
    evi_running = make_evidence(kind="experiment", experiment="EXP-0001")
    claim = make_claim(status="evidence_linked", supporting_evidence=["EVI-0001"])
    running_report = _report([hyp, running, evi_running, claim])
    assert E_NONQUALIFYING_EVIDENCE in running_report.codes()

    evi_failed = make_evidence(id="EVI-0002", kind="experiment", experiment="EXP-0002")
    claim_failed = make_claim(
        id="CLAIM-0002", status="evidence_linked", supporting_evidence=["EVI-0002"]
    )
    failed_report = _report([hyp, failed, evi_failed, claim_failed])
    assert E_NONQUALIFYING_EVIDENCE in failed_report.codes()

    completed = make_experiment(status="completed")
    evi_done = make_evidence(kind="experiment", experiment="EXP-0001")
    claim_ok = make_claim(status="evidence_linked", supporting_evidence=["EVI-0001"])
    ok = _report([hyp, completed, evi_done, claim_ok])
    assert E_NONQUALIFYING_EVIDENCE not in ok.codes()
    assert ok.ok


def test_hypothesis_terminal_evidence_gates() -> None:
    active = make_evidence(id="EVI-0001", status="active")
    withdrawn = make_evidence(id="EVI-0002", status="withdrawn")
    supported_bad = make_hypothesis(
        status="supported",
        supporting_evidence=["EVI-0002"],
    )
    assert E_NONQUALIFYING_EVIDENCE in _codes([active, withdrawn, supported_bad])

    supported_ok = make_hypothesis(
        id="HYP-0002",
        status="supported",
        supporting_evidence=["EVI-0001"],
    )
    assert E_NONQUALIFYING_EVIDENCE not in _codes([active, withdrawn, supported_ok])

    rejected_bad = make_hypothesis(
        id="HYP-0003",
        status="rejected",
        contrary_evidence=["EVI-0002"],
    )
    assert E_NONQUALIFYING_EVIDENCE in _codes([active, withdrawn, rejected_bad])

    rejected_ok = make_hypothesis(
        id="HYP-0004",
        status="rejected",
        contrary_evidence=["EVI-0001"],
    )
    assert E_NONQUALIFYING_EVIDENCE not in _codes([active, withdrawn, rejected_ok])

    inconclusive_bad = make_hypothesis(
        id="HYP-0005",
        status="inconclusive",
        supporting_evidence=["EVI-0002"],
        contrary_evidence=["EVI-0002"],
    )
    assert E_NONQUALIFYING_EVIDENCE in _codes([active, withdrawn, inconclusive_bad])

    inconclusive_ok = make_hypothesis(
        id="HYP-0006",
        status="inconclusive",
        contrary_evidence=["EVI-0001"],
    )
    assert E_NONQUALIFYING_EVIDENCE not in _codes([active, withdrawn, inconclusive_ok])


def test_accepted_claim_without_evidence() -> None:
    claim = make_claim(status="accepted")
    review = make_review(
        status="concluded",
        subject=claim.id,
        subject_digest=subject_digest(claim, project_id=TEST_PROJECT_ID),
    )
    report = _report([claim, review])
    assert E_ACCEPTED_WITHOUT_EVIDENCE in report.codes()


def test_accepted_claim_without_review() -> None:
    objects = [
        make_evidence(),
        make_claim(status="accepted", supporting_evidence=["EVI-0001"]),
    ]
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in _codes(objects)


def test_independent_agent_review_cannot_accept_claim() -> None:
    objects = _accepted_bundle(reviewer_kind="independent_agent")
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in _codes(objects)


def test_human_reject_review_cannot_accept_claim() -> None:
    objects = _accepted_bundle(verdict="reject")
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in _codes(objects)


def test_non_concluded_review_cannot_accept_claim() -> None:
    objects = _accepted_bundle(review_status="submitted")
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW in _codes(objects)


def test_stale_digest_does_not_accept_changed_claim() -> None:
    evidence = make_evidence()
    original = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        statement="Old",
    )
    original_digest = subject_digest(original, project_id=TEST_PROJECT_ID)
    changed = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        statement="New scientific content",
    )
    review = make_review(
        status="concluded",
        subject=changed.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Approved the old statement.",
        subject_digest=original_digest,
        evidence_digests={
            "EVI-0001": subject_digest(evidence, project_id=TEST_PROJECT_ID)
        },
    )
    report = _report([evidence, changed, review])
    assert E_STALE_REVIEW_DIGEST in report.codes()
    assert W_STALE_SUBJECT_DIGEST in report.codes()
    assert subject_digest(changed, project_id=TEST_PROJECT_ID) != original_digest


def test_matching_human_approval_accepts_claim() -> None:
    objects = _accepted_bundle()
    report = _report(objects)
    assert report.ok
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW not in report.codes()
    assert E_STALE_REVIEW_DIGEST not in report.codes()


def test_lifecycle_only_status_change_keeps_digest_and_acceptance() -> None:
    linked_claim = make_claim(
        status="evidence_linked",
        supporting_evidence=["EVI-0001"],
        statement="Stable statement.",
    )
    digest = subject_digest(linked_claim, project_id=TEST_PROJECT_ID)
    accepted_claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        statement="Stable statement.",
    )
    assert subject_digest(accepted_claim, project_id=TEST_PROJECT_ID) == digest
    evidence = make_evidence()
    review = make_review(
        status="concluded",
        subject=accepted_claim.id,
        subject_digest=digest,
        evidence_digests={
            "EVI-0001": subject_digest(evidence, project_id=TEST_PROJECT_ID)
        },
    )
    report = _report([evidence, accepted_claim, review])
    assert report.ok


def test_review_subject_must_resolve() -> None:
    review = make_review(subject="CLAIM-0099")
    report = _report([review, make_question()])
    assert E_DANGLING_REF in report.codes()
    assert any(item.field == "subject" for item in report.errors)


def test_split_supersession() -> None:
    predecessor = make_question(id="Q-0001", status="superseded")
    first = make_question(id="Q-0002", supersedes=["Q-0001"])
    second = make_question(id="Q-0003", supersedes=["Q-0001"])
    report = _report([predecessor, first, second])
    assert report.ok
    assert E_SUPERSESSION_CYCLE not in report.codes()


def test_merge_supersession() -> None:
    first = make_question(id="Q-0001", status="superseded")
    second = make_question(id="Q-0002", status="superseded")
    merged = make_question(id="Q-0003", supersedes=["Q-0001", "Q-0002"])
    report = _report([first, second, merged])
    assert report.ok


def test_supersedes_requires_predecessor_superseded() -> None:
    open_pred = make_question(id="Q-0001", status="open")
    successor = make_question(id="Q-0002", supersedes=["Q-0001"])
    open_report = _report([open_pred, successor])
    assert E_SUPERSEDES_NON_SUPERSEDED in open_report.codes()
    assert not open_report.ok

    active_pred = make_idea(id="IDEA-0001", status="active")
    active_succ = make_idea(id="IDEA-0002", supersedes=["IDEA-0001"])
    active_report = _report([active_pred, active_succ])
    assert E_SUPERSEDES_NON_SUPERSEDED in active_report.codes()

    superseded_pred = make_question(id="Q-0001", status="superseded")
    ok_succ = make_question(id="Q-0002", supersedes=["Q-0001"])
    ok_report = _report([superseded_pred, ok_succ])
    assert ok_report.ok
    assert E_SUPERSEDES_NON_SUPERSEDED not in ok_report.codes()


def test_supersession_cycle() -> None:
    first = make_question(id="Q-0001", status="superseded", supersedes=["Q-0002"])
    second = make_question(id="Q-0002", status="superseded", supersedes=["Q-0001"])
    assert E_SUPERSESSION_CYCLE in _codes([first, second])


def test_superseded_without_successor() -> None:
    orphan = make_question(status="superseded")
    assert E_SUPERSEDED_WITHOUT_SUCCESSOR in _codes([orphan])


def test_withdrawn_cannot_be_superseded() -> None:
    withdrawn = make_question(id="Q-0001", status="withdrawn")
    replacement = make_question(id="Q-0002", supersedes=["Q-0001"])
    assert E_WITHDRAWN_SUPERSEDED in _codes([withdrawn, replacement])


def test_promoted_idea_without_hypothesis_is_warning() -> None:
    idea = make_idea(status="promoted")
    report = _report([idea])
    assert report.ok
    assert W_PROMOTED_WITHOUT_HYPOTHESIS in report.codes()
    hyp = make_hypothesis(created_from=["IDEA-0001"])
    assert W_PROMOTED_WITHOUT_HYPOTHESIS not in _codes([idea, hyp])


def test_findings_are_deterministic_across_input_order() -> None:
    objects = [
        make_question(id="Q-0002", created_from=["Q-9999"]),
        make_question(id="Q-0001", created_from=["Q-9999"]),
    ]
    first = _report(objects)
    second = _report(list(reversed(objects)))
    assert first.findings == second.findings


def test_validate_does_not_require_filesystem_or_registry() -> None:
    report = _report(_accepted_bundle())
    assert report.ok
    assert report.errors == ()


def test_stale_evidence_digest_invalidates_accepted_claim() -> None:
    """The demonstrated approval hole: Evidence content changes after review.

    The Claim itself is untouched, so its own subject digest still matches.
    Only the explicit evidence-digest map catches the change.
    """

    evidence = make_evidence(statement="The source supports X.")
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        statement="X is supported.",
    )
    claim_digest = subject_digest(claim, project_id=TEST_PROJECT_ID)
    review = make_review(
        status="concluded",
        subject=claim.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Read the cited source directly.",
        subject_digest=claim_digest,
        evidence_digests={
            "EVI-0001": subject_digest(evidence, project_id=TEST_PROJECT_ID)
        },
    )
    before = _report([evidence, claim, review])
    assert before.ok

    mutated = make_evidence(statement="The source in fact contradicts X above 200 K.")
    after = _report([mutated, claim, review])
    assert not after.ok
    assert E_STALE_REVIEW_DIGEST in after.codes()
    assert W_STALE_EVIDENCE_DIGEST in after.codes()
    assert subject_digest(claim, project_id=TEST_PROJECT_ID) == claim_digest
    assert W_STALE_SUBJECT_DIGEST not in after.codes()


def test_evidence_digests_must_cover_all_linked_evidence() -> None:
    supporting = make_evidence(id="EVI-0001")
    contrary = make_evidence(id="EVI-0002", citation="Jones 2021")
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        contrary_evidence=["EVI-0002"],
        contrary_evidence_addressed="The contrary result applies below 200 K.",
    )
    claim_digest = subject_digest(claim, project_id=TEST_PROJECT_ID)
    supporting_digest = subject_digest(supporting, project_id=TEST_PROJECT_ID)
    contrary_digest = subject_digest(contrary, project_id=TEST_PROJECT_ID)

    partial = make_review(
        status="concluded",
        subject=claim.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Reviewed only the supporting source.",
        subject_digest=claim_digest,
        evidence_digests={"EVI-0001": supporting_digest},
    )
    partial_report = _report([supporting, contrary, claim, partial])
    assert E_EVIDENCE_DIGESTS_INCOMPLETE in partial_report.codes()
    assert not partial_report.ok

    bare = make_review(
        status="concluded",
        subject=claim.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Recorded no evidence digests at all.",
        subject_digest=claim_digest,
    )
    bare_report = _report([supporting, contrary, claim, bare])
    assert E_EVIDENCE_DIGESTS_INCOMPLETE in bare_report.codes()

    complete = make_review(
        status="concluded",
        subject=claim.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Reviewed both sources.",
        subject_digest=claim_digest,
        evidence_digests={
            "EVI-0001": supporting_digest,
            "EVI-0002": contrary_digest,
        },
    )
    complete_report = _report([supporting, contrary, claim, complete])
    assert complete_report.ok


def test_evidence_digest_key_must_be_linked_to_claim() -> None:
    linked = make_evidence(id="EVI-0001")
    unlinked = make_evidence(id="EVI-0002", citation="Jones 2021")
    claim = make_claim(status="evidence_linked", supporting_evidence=["EVI-0001"])
    review = make_review(
        status="concluded",
        subject=claim.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Reviewed.",
        subject_digest=subject_digest(claim, project_id=TEST_PROJECT_ID),
        evidence_digests={
            "EVI-0001": subject_digest(linked, project_id=TEST_PROJECT_ID),
            "EVI-0002": subject_digest(unlinked, project_id=TEST_PROJECT_ID),
        },
    )
    report = _report([linked, unlinked, claim, review])
    assert E_EVIDENCE_DIGEST_UNLINKED in report.codes()
    assert not report.ok


def test_review_from_another_project_does_not_validate() -> None:
    objects = _accepted_bundle(project_id=TEST_PROJECT_ID)
    assert _report(objects, project_id=TEST_PROJECT_ID).ok
    other = _report(objects, project_id=OTHER_PROJECT_ID)
    assert not other.ok
    assert E_STALE_REVIEW_DIGEST in other.codes()
    assert W_STALE_SUBJECT_DIGEST in other.codes()


def test_project_identity_is_required_for_validation() -> None:
    """No supported call validates claims with project scope disabled."""

    objects = _accepted_bundle()
    with pytest.raises(TypeError):
        validate_objects(objects)  # type: ignore[call-arg]
    for invalid in (None, "", "Alpha", "alpha_project"):
        with pytest.raises(InvalidIdError):
            validate_objects(objects, project_id=invalid)  # type: ignore[arg-type]


def test_retirement_metadata_does_not_break_a_concluded_review() -> None:
    active = make_hypothesis(status="testing")
    review = make_review(
        status="concluded",
        subject=active.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Reviewed the hypothesis as stated.",
        subject_digest=subject_digest(active, project_id=TEST_PROJECT_ID),
    )
    assert _report([active, review]).ok

    retired = make_hypothesis(
        status="withdrawn",
        retire_reason="The instrument was decommissioned mid-campaign.",
        revisit_if="A calibrated replacement sensor is installed.",
    )
    report = _report([retired, review])
    assert W_STALE_SUBJECT_DIGEST not in report.codes()
    assert report.ok


# --- experiment review binding ------------------------------------------------


def _digest(obj: ScientificObject, project_id: str = TEST_PROJECT_ID) -> str:
    return subject_digest(obj, project_id=project_id)


def _experiment_capsule(
    *,
    claim_status: str = "accepted",
    experiment_updates: dict | None = None,
    evidence_status: str = "active",
) -> tuple[list[ScientificObject], object, object]:
    """Build the smallest claim that rests on experiment-derived evidence.

    Returns ``(objects, claim, experiment)`` without a Review, so each test can
    bind whatever map it means to exercise.
    """

    hypothesis = make_hypothesis(status="active")
    experiment = make_experiment(status="completed", **(experiment_updates or {}))
    evidence = make_evidence(
        id="EVI-0001",
        kind="experiment",
        experiment="EXP-0001",
        status=evidence_status,
    )
    claim = make_claim(status=claim_status, supporting_evidence=["EVI-0001"])
    return [hypothesis, experiment, evidence, claim], claim, experiment


def _approval(claim, **updates) -> object:
    base = {
        "status": "concluded",
        "subject": claim.id,
        "reviewer_kind": "human",
        "verdict": "approve",
        "findings": "Reviewed.",
        "subject_digest": _digest(claim),
    }
    base.update(updates)
    return make_review(**base)


def test_experiment_backed_claim_accepts_when_the_experiment_is_bound() -> None:
    objects, claim, experiment = _experiment_capsule()
    evidence = objects[2]
    review = _approval(
        claim,
        evidence_digests={"EVI-0001": _digest(evidence)},
        experiment_digests={"EXP-0001": _digest(experiment)},
    )
    report = _report([*objects, review])
    assert report.ok
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE not in report.codes()


def test_changed_experiment_invalidates_an_accepted_claim() -> None:
    """The finding this patch exists to close.

    The Claim and the Evidence are untouched, so only the Experiment binding can
    notice that the science behind the approval moved.
    """

    objects, claim, experiment = _experiment_capsule()
    evidence = objects[2]
    review = _approval(
        claim,
        evidence_digests={"EVI-0001": _digest(evidence)},
        experiment_digests={"EXP-0001": _digest(experiment)},
    )
    before = _report([*objects, review])
    assert before.ok

    changed = make_experiment(
        status="completed",
        primary_metrics=["heldout_rmse", "auroc"],
    )
    after = _report([objects[0], changed, evidence, claim, review])
    assert not after.ok
    assert E_STALE_REVIEW_DIGEST in after.codes()
    assert W_STALE_EXPERIMENT_DIGEST in after.codes()
    assert W_STALE_SUBJECT_DIGEST not in after.codes()
    assert W_STALE_EVIDENCE_DIGEST not in after.codes()


def test_experiment_digests_must_cover_every_referenced_experiment() -> None:
    objects, claim, experiment = _experiment_capsule()
    evidence = objects[2]
    evidence_digests = {"EVI-0001": _digest(evidence)}

    bare = _approval(claim, evidence_digests=evidence_digests)
    bare_report = _report([*objects, bare])
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in bare_report.codes()
    assert not bare_report.ok

    empty = _approval(claim, evidence_digests=evidence_digests, experiment_digests={})
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in _report([*objects, empty]).codes()

    complete = _approval(
        claim,
        evidence_digests=evidence_digests,
        experiment_digests={"EXP-0001": _digest(experiment)},
    )
    assert _report([*objects, complete]).ok


def test_experiment_digest_key_must_be_referenced_by_the_claim() -> None:
    objects, claim, experiment = _experiment_capsule()
    evidence = objects[2]
    other = make_experiment(id="EXP-0002", status="completed")
    review = _approval(
        claim,
        evidence_digests={"EVI-0001": _digest(evidence)},
        experiment_digests={
            "EXP-0001": _digest(experiment),
            "EXP-0002": _digest(other),
        },
    )
    report = _report([*objects, other, review])
    assert E_EXPERIMENT_DIGEST_UNLINKED in report.codes()
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in report.codes()
    assert not report.ok


@pytest.mark.parametrize("review_status", ["draft", "submitted", "concluded"])
def test_unlinked_experiment_digest_is_reported_at_any_review_status(
    review_status: str,
) -> None:
    objects, claim, _experiment = _experiment_capsule(claim_status="evidence_linked")
    other = make_experiment(id="EXP-0002", status="completed")
    review = _approval(
        claim,
        status=review_status,
        experiment_digests={"EXP-0002": _digest(other)},
    )
    assert E_EXPERIMENT_DIGEST_UNLINKED in _codes([*objects, other, review])


def test_stale_experiment_digest_warns_without_blocking_an_unaccepted_claim() -> None:
    objects, claim, _experiment = _experiment_capsule(claim_status="evidence_linked")
    stale = f"{'0' * 63}1"
    review = _approval(claim, experiment_digests={"EXP-0001": f"1:{stale}"})
    report = _report([*objects, review])
    assert W_STALE_EXPERIMENT_DIGEST in report.codes()
    assert report.ok


def test_experiment_status_only_change_does_not_change_its_digest() -> None:
    """Status is outside every projection, so it cannot stale a binding.

    Recorded deliberately: a withdrawn Experiment stops *qualifying*, which is a
    separate rule, but it does not invalidate the digest a review bound.
    """

    completed = make_experiment(status="completed")
    withdrawn = Experiment.model_validate(
        {**completed.model_dump(mode="json"), "status": "withdrawn"}
    )
    assert _digest(completed) == _digest(withdrawn)


def test_two_evidence_objects_on_one_experiment_bind_it_once() -> None:
    hypothesis = make_hypothesis(status="active")
    experiment = make_experiment(status="completed")
    first = make_evidence(id="EVI-0001", kind="experiment", experiment="EXP-0001")
    second = make_evidence(id="EVI-0002", kind="experiment", experiment="EXP-0001")
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001", "EVI-0002"],
    )
    objects = [hypothesis, experiment, first, second, claim]
    by_id = {item.id: item for item in objects}
    assert referenced_experiments(claim, by_id) == frozenset({"EXP-0001"})

    review = _approval(
        claim,
        evidence_digests={
            "EVI-0001": _digest(first),
            "EVI-0002": _digest(second),
        },
        experiment_digests={"EXP-0001": _digest(experiment)},
    )
    assert _report([*objects, review]).ok


def test_supporting_and_contrary_evidence_on_one_experiment_bind_it_once() -> None:
    hypothesis = make_hypothesis(status="active")
    experiment = make_experiment(status="completed")
    supporting = make_evidence(id="EVI-0001", kind="experiment", experiment="EXP-0001")
    contrary = make_evidence(id="EVI-0002", kind="experiment", experiment="EXP-0001")
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        contrary_evidence=["EVI-0002"],
        contrary_evidence_addressed="The contrary reading uses a different window.",
    )
    objects = [hypothesis, experiment, supporting, contrary, claim]
    by_id = {item.id: item for item in objects}
    assert referenced_experiments(claim, by_id) == frozenset({"EXP-0001"})

    review = _approval(
        claim,
        evidence_digests={
            "EVI-0001": _digest(supporting),
            "EVI-0002": _digest(contrary),
        },
        experiment_digests={"EXP-0001": _digest(experiment)},
    )
    assert _report([*objects, review]).ok


def test_withdrawn_experiment_evidence_still_contributes_its_experiment() -> None:
    """Coverage follows what the review examined, not what currently qualifies."""

    hypothesis = make_hypothesis(status="active")
    experiment = make_experiment(status="completed")
    qualifying = make_evidence(id="EVI-0001")
    retired = make_evidence(
        id="EVI-0002",
        kind="experiment",
        experiment="EXP-0001",
        status="withdrawn",
    )
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001", "EVI-0002"],
    )
    objects = [hypothesis, experiment, qualifying, retired, claim]
    by_id = {item.id: item for item in objects}
    assert referenced_experiments(claim, by_id) == frozenset({"EXP-0001"})

    without = _approval(
        claim,
        evidence_digests={
            "EVI-0001": _digest(qualifying),
            "EVI-0002": _digest(retired),
        },
    )
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in _codes([*objects, without])


def test_literature_only_claim_needs_no_experiment_binding() -> None:
    evidence = make_evidence(id="EVI-0001")
    claim = make_claim(status="accepted", supporting_evidence=["EVI-0001"])
    by_id = {evidence.id: evidence, claim.id: claim}
    assert referenced_experiments(claim, by_id) == frozenset()

    digests = {"EVI-0001": _digest(evidence)}
    omitted = _approval(claim, evidence_digests=digests)
    assert _report([evidence, claim, omitted]).ok

    empty = _approval(claim, evidence_digests=digests, experiment_digests={})
    assert _report([evidence, claim, empty]).ok

    spurious = _approval(
        claim,
        evidence_digests=digests,
        experiment_digests={"EXP-0001": f"1:{'a' * 64}"},
    )
    spurious_report = _report([evidence, claim, spurious])
    assert E_EXPERIMENT_DIGEST_UNLINKED in spurious_report.codes()
    assert not spurious_report.ok


def test_dangling_experiment_pointer_blocks_acceptance() -> None:
    """A missing Experiment stays required, so the binding cannot shrink to fit."""

    evidence = make_evidence(id="EVI-0001", kind="experiment", experiment="EXP-0404")
    claim = make_claim(status="accepted", supporting_evidence=["EVI-0001"])
    by_id = {evidence.id: evidence, claim.id: claim}
    assert referenced_experiments(claim, by_id) == frozenset({"EXP-0404"})

    review = _approval(claim, evidence_digests={"EVI-0001": _digest(evidence)})
    report = _report([evidence, claim, review])
    assert not report.ok
    assert E_DANGLING_REF in report.codes()
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in report.codes()


def test_experiment_binding_from_another_project_does_not_transfer() -> None:
    objects, claim, experiment = _experiment_capsule()
    evidence = objects[2]
    review = _approval(
        claim,
        evidence_digests={"EVI-0001": _digest(evidence)},
        experiment_digests={"EXP-0001": _digest(experiment, OTHER_PROJECT_ID)},
    )
    report = _report([*objects, review])
    assert not report.ok
    assert E_STALE_REVIEW_DIGEST in report.codes()
    assert W_STALE_EXPERIMENT_DIGEST in report.codes()


def test_unresolvable_evidence_contributes_no_experiment() -> None:
    claim = make_claim(status="draft", supporting_evidence=["EVI-0404"])
    assert referenced_experiments(claim, {claim.id: claim}) == frozenset()


def test_two_experiments_are_both_bound() -> None:
    """A claim reaching two Experiments must bind both, and each must be current."""

    hypothesis = make_hypothesis(status="active")
    first = make_experiment(status="completed")
    second = make_experiment(id="EXP-0002", status="completed", title="Second sweep")
    evi_first = make_evidence(kind="experiment", experiment="EXP-0001")
    evi_second = make_evidence(
        id="EVI-0002",
        kind="experiment",
        experiment="EXP-0002",
    )
    claim = make_claim(
        status="accepted",
        supporting_evidence=["EVI-0001"],
        contrary_evidence=["EVI-0002"],
        contrary_evidence_addressed="The second sweep used a different probe.",
    )
    objects = [hypothesis, first, second, evi_first, evi_second, claim]
    evidence_digests = {
        "EVI-0001": _digest(evi_first),
        "EVI-0002": _digest(evi_second),
    }

    complete = _approval(
        claim,
        evidence_digests=evidence_digests,
        experiment_digests={
            "EXP-0001": _digest(first),
            "EXP-0002": _digest(second),
        },
    )
    assert _report([*objects, complete]).ok

    half = _approval(
        claim,
        evidence_digests=evidence_digests,
        experiment_digests={"EXP-0001": _digest(first)},
    )
    partial = _report([*objects, half])
    assert not partial.ok
    assert E_EXPERIMENT_DIGESTS_INCOMPLETE in partial.codes()

    moved = make_experiment(
        id="EXP-0002",
        status="completed",
        title="Second sweep",
        primary_metrics=["heldout_rmse", "auroc"],
    )
    after = _report([hypothesis, first, moved, evi_first, evi_second, claim, complete])
    assert not after.ok
    assert E_STALE_REVIEW_DIGEST in after.codes()
    assert W_STALE_EXPERIMENT_DIGEST in after.codes()


def test_experiment_content_is_not_hashed_into_evidence_or_claim() -> None:
    """The binding is explicit and flat, never a recursive digest.

    If Experiment content leaked into the Evidence or Claim projection, this
    patch would have changed the meaning of every existing digest instead of
    adding a separate, inspectable one.
    """

    experiment = make_experiment(status="completed")
    evidence = make_evidence(kind="experiment", experiment="EXP-0001")
    claim = make_claim(status="evidence_linked", supporting_evidence=["EVI-0001"])

    changed = make_experiment(
        status="completed",
        purpose="A materially different purpose.",
        primary_metrics=["heldout_rmse", "auroc"],
        decision_rule="Reject above 0.05.",
    )
    assert _digest(changed) != _digest(experiment)
    assert _digest(evidence) == _digest(
        make_evidence(kind="experiment", experiment="EXP-0001")
    )
    assert _digest(claim) == _digest(
        make_claim(status="accepted", supporting_evidence=["EVI-0001"])
    )
