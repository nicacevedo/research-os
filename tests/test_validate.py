"""Cross-object validation tests for Research Capsule scientific state."""

from __future__ import annotations

from research_os.digests import subject_digest
from research_os.errors import (
    E_ACCEPTED_WITHOUT_EVIDENCE,
    E_ACCEPTED_WITHOUT_HUMAN_REVIEW,
    E_CREATED_FROM_CYCLE,
    E_DANGLING_REF,
    E_DUP_ID,
    E_NONQUALIFYING_EVIDENCE,
    E_STALE_REVIEW_DIGEST,
    E_SUPERSEDED_WITHOUT_SUCCESSOR,
    E_SUPERSEDES_NON_SUPERSEDED,
    E_SUPERSESSION_CYCLE,
    E_WITHDRAWN_SUPERSEDED,
    E_WRONG_REF_TYPE,
    W_PROMOTED_WITHOUT_HYPOTHESIS,
    W_STALE_SUBJECT_DIGEST,
)
from research_os.models import ScientificObject
from research_os.validate import validate_objects
from tests.helpers import (
    make_claim,
    make_evidence,
    make_experiment,
    make_hypothesis,
    make_idea,
    make_question,
    make_review,
)


def _codes(objects: list[ScientificObject]) -> set[str]:
    return set(validate_objects(objects).codes())


def _accepted_bundle(
    *,
    claim_status: str = "accepted",
    reviewer_kind: str = "human",
    verdict: str = "approve",
    review_status: str = "concluded",
    evidence_status: str = "active",
    statement: str = "X is supported.",
    digest: str | None = None,
) -> list[ScientificObject]:
    evidence = make_evidence(status=evidence_status)
    claim = make_claim(
        status=claim_status,
        evidence=["EVI-0001"],
        statement=statement,
    )
    subject_digest_value = digest if digest is not None else subject_digest(claim)
    review = make_review(
        status=review_status,
        subject=claim.id,
        reviewer_kind=reviewer_kind,
        verdict=verdict,
        findings="Reviewed.",
        subject_digest=subject_digest_value,
    )
    return [evidence, claim, review]


def test_duplicate_ids_are_errors() -> None:
    report = validate_objects([make_question(), make_question(title="Other")])
    assert E_DUP_ID in report.codes()
    assert not report.ok


def test_duplicate_ids_do_not_use_first_object_wins() -> None:
    unique = make_question(id="Q-0002")
    dup_a = make_question(id="Q-0001", title="First payload", created_from=["Q-0002"])
    dup_b = make_question(id="Q-0001", title="Second payload", created_from=["Q-9999"])
    first = validate_objects([dup_a, dup_b, unique])
    second = validate_objects([dup_b, dup_a, unique])
    third = validate_objects([unique, dup_b, dup_a])
    assert first.findings == second.findings == third.findings
    assert first.codes() == (E_DUP_ID,)
    assert E_DANGLING_REF not in first.codes()


def test_dangling_and_wrong_type_refs() -> None:
    dangling = validate_objects(
        [make_claim(status="draft", evidence=["EVI-9999"])]
    )
    assert E_DANGLING_REF in dangling.codes()
    wrong = validate_objects(
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
    objects = _accepted_bundle(claim_status="evidence_linked", evidence_status="withdrawn")
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
        evidence=["EVI-0001", "EVI-0002"],
    )
    report = validate_objects([evidence_active, evidence_withdrawn, claim])
    assert E_NONQUALIFYING_EVIDENCE not in report.codes()
    assert report.ok


def test_experiment_evidence_requires_completed_experiment() -> None:
    hyp = make_hypothesis()
    running = make_experiment(status="running")
    failed = make_experiment(id="EXP-0002", status="failed")
    evi_running = make_evidence(kind="experiment", experiment="EXP-0001")
    claim = make_claim(status="evidence_linked", evidence=["EVI-0001"])
    running_report = validate_objects([hyp, running, evi_running, claim])
    assert E_NONQUALIFYING_EVIDENCE in running_report.codes()

    evi_failed = make_evidence(
        id="EVI-0002", kind="experiment", experiment="EXP-0002"
    )
    claim_failed = make_claim(
        id="CLAIM-0002", status="evidence_linked", evidence=["EVI-0002"]
    )
    failed_report = validate_objects([hyp, failed, evi_failed, claim_failed])
    assert E_NONQUALIFYING_EVIDENCE in failed_report.codes()

    completed = make_experiment(status="completed")
    evi_done = make_evidence(kind="experiment", experiment="EXP-0001")
    claim_ok = make_claim(status="evidence_linked", evidence=["EVI-0001"])
    ok = validate_objects([hyp, completed, evi_done, claim_ok])
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
        subject_digest=subject_digest(claim),
    )
    report = validate_objects([claim, review])
    assert E_ACCEPTED_WITHOUT_EVIDENCE in report.codes()


def test_accepted_claim_without_review() -> None:
    objects = [
        make_evidence(),
        make_claim(status="accepted", evidence=["EVI-0001"]),
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
    original = make_claim(status="accepted", evidence=["EVI-0001"], statement="Old")
    original_digest = subject_digest(original)
    changed = make_claim(
        status="accepted",
        evidence=["EVI-0001"],
        statement="New scientific content",
    )
    review = make_review(
        status="concluded",
        subject=changed.id,
        reviewer_kind="human",
        verdict="approve",
        findings="Approved the old statement.",
        subject_digest=original_digest,
    )
    report = validate_objects([evidence, changed, review])
    assert E_STALE_REVIEW_DIGEST in report.codes()
    assert W_STALE_SUBJECT_DIGEST in report.codes()
    assert subject_digest(changed) != original_digest


def test_matching_human_approval_accepts_claim() -> None:
    objects = _accepted_bundle()
    report = validate_objects(objects)
    assert report.ok
    assert E_ACCEPTED_WITHOUT_HUMAN_REVIEW not in report.codes()
    assert E_STALE_REVIEW_DIGEST not in report.codes()


def test_lifecycle_only_status_change_keeps_digest_and_acceptance() -> None:
    linked_claim = make_claim(
        status="evidence_linked",
        evidence=["EVI-0001"],
        statement="Stable statement.",
    )
    digest = subject_digest(linked_claim)
    accepted_claim = make_claim(
        status="accepted",
        evidence=["EVI-0001"],
        statement="Stable statement.",
    )
    assert subject_digest(accepted_claim) == digest
    review = make_review(
        status="concluded",
        subject=accepted_claim.id,
        subject_digest=digest,
    )
    report = validate_objects([make_evidence(), accepted_claim, review])
    assert report.ok


def test_review_subject_must_resolve() -> None:
    review = make_review(subject="CLAIM-0099")
    report = validate_objects([review, make_question()])
    assert E_DANGLING_REF in report.codes()
    assert any(item.field == "subject" for item in report.errors)


def test_split_supersession() -> None:
    predecessor = make_question(id="Q-0001", status="superseded")
    first = make_question(id="Q-0002", supersedes=["Q-0001"])
    second = make_question(id="Q-0003", supersedes=["Q-0001"])
    report = validate_objects([predecessor, first, second])
    assert report.ok
    assert E_SUPERSESSION_CYCLE not in report.codes()


def test_merge_supersession() -> None:
    first = make_question(id="Q-0001", status="superseded")
    second = make_question(id="Q-0002", status="superseded")
    merged = make_question(id="Q-0003", supersedes=["Q-0001", "Q-0002"])
    report = validate_objects([first, second, merged])
    assert report.ok


def test_supersedes_requires_predecessor_superseded() -> None:
    open_pred = make_question(id="Q-0001", status="open")
    successor = make_question(id="Q-0002", supersedes=["Q-0001"])
    open_report = validate_objects([open_pred, successor])
    assert E_SUPERSEDES_NON_SUPERSEDED in open_report.codes()
    assert not open_report.ok

    active_pred = make_idea(id="IDEA-0001", status="active")
    active_succ = make_idea(id="IDEA-0002", supersedes=["IDEA-0001"])
    active_report = validate_objects([active_pred, active_succ])
    assert E_SUPERSEDES_NON_SUPERSEDED in active_report.codes()

    superseded_pred = make_question(id="Q-0001", status="superseded")
    ok_succ = make_question(id="Q-0002", supersedes=["Q-0001"])
    ok_report = validate_objects([superseded_pred, ok_succ])
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
    report = validate_objects([idea])
    assert report.ok
    assert W_PROMOTED_WITHOUT_HYPOTHESIS in report.codes()
    hyp = make_hypothesis(created_from=["IDEA-0001"])
    assert W_PROMOTED_WITHOUT_HYPOTHESIS not in _codes([idea, hyp])


def test_findings_are_deterministic_across_input_order() -> None:
    objects = [
        make_question(id="Q-0002", created_from=["Q-9999"]),
        make_question(id="Q-0001", created_from=["Q-9999"]),
    ]
    first = validate_objects(objects)
    second = validate_objects(list(reversed(objects)))
    assert first.findings == second.findings


def test_validate_does_not_require_filesystem_or_registry() -> None:
    report = validate_objects(_accepted_bundle())
    assert report.ok
    assert report.errors == ()
