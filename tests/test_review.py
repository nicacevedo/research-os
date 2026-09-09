"""Tests for review-packet assembly and canonical Review authoring."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
import yaml

from research_os.capsule import resolve_object, validate_project
from research_os.digests import subject_digest
from research_os.errors import (
    E_EVIDENCE_DIGESTS_INCOMPLETE,
    CapsuleError,
    ReviewBlockedError,
)
from research_os.models import Claim, Review, Verdict, parse_object
from research_os.review import (
    build_review,
    build_review_packet,
    write_review,
)
from tests.fs_helpers import (
    make_git_repo,
    question_data,
    snapshot_files,
    write_reviewable_capsule,
    write_yaml,
)

FINDINGS = "Supporting evidence checked against the cited figure."


def _packet(repo: Path, claim_id: str = "CLAIM-0001"):
    return build_review_packet(validate_project(repo), claim_id)


def _repo(tmp_path: Path, **kwargs) -> Path:
    repo = make_git_repo(tmp_path / "sample-project")
    write_reviewable_capsule(repo, **kwargs)
    return repo


# --- packet contents ------------------------------------------------------


def test_packet_lists_supporting_and_contrary_evidence(tmp_path: Path) -> None:
    packet = _packet(_repo(tmp_path))
    assert [entry.id for entry in packet.supporting] == ["EVI-0001", "EVI-0003"]
    assert [entry.id for entry in packet.contrary] == ["EVI-0002"]
    assert packet.claim.contrary_evidence_addressed is not None
    assert packet.hypotheses == (("HYP-0001", "X holds above 200 K"),)
    assert packet.project_id == "sample-project"


def test_packet_shows_every_non_null_evidence_pointer(tmp_path: Path) -> None:
    """Evaluating evidence means following it to its source.

    All four pointers are surfaced when present, and absent ones are omitted
    rather than rendered as placeholders.
    """

    packet = _packet(_repo(tmp_path))
    entries = {entry.id: entry for entry in packet.entries()}

    literature = dict(entries["EVI-0001"].pointers())
    assert literature == {
        "citation": "Doe et al., J. Example Phys. 12, 345 (2024)",
        "global_ref": "doi:10.1000/example.12.345",
        "locator": "Fig. 3",
    }

    derived = dict(entries["EVI-0003"].pointers())
    assert derived == {"experiment": "EXP-0001  (status: completed)"}
    assert "citation" not in derived
    assert "locator" not in derived

    for entry in packet.entries():
        assert entry.title
        assert entry.statement
        assert entry.kind
        assert entry.status


def test_packet_experiment_pointer_shows_qualifying_status(tmp_path: Path) -> None:
    """A referenced Experiment's status decides whether evidence qualifies."""

    repo = _repo(tmp_path)
    manifest = repo / ".research" / "experiments" / "EXP-0001" / "manifest.yaml"
    data = yaml.safe_load(manifest.read_text(encoding="utf-8"))
    data["status"] = "running"
    del data["provenance"]
    write_yaml(manifest, data)
    packet = _packet(repo)
    entry = next(item for item in packet.entries() if item.id == "EVI-0003")
    assert dict(entry.pointers())["experiment"] == "EXP-0001  (status: running)"


def test_packet_claim_digest_is_canonical(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    report = validate_project(repo)
    claim = resolve_object(report, "CLAIM-0001")
    assert isinstance(claim, Claim)
    packet = build_review_packet(report, "CLAIM-0001")
    assert packet.claim_digest == subject_digest(claim, project_id="sample-project")


def test_packet_evidence_digests_are_canonical_and_complete(tmp_path: Path) -> None:
    """The bound map is exactly the Claim's linked set, in both polarities.

    This is the coverage the WP-A gate requires: an approval that did not
    examine every linked Evidence object must not gate acceptance.
    """

    repo = _repo(tmp_path)
    report = validate_project(repo)
    packet = build_review_packet(report, "CLAIM-0001")
    claim = resolve_object(report, "CLAIM-0001")
    assert isinstance(claim, Claim)

    linked = set(claim.supporting_evidence or []) | set(claim.contrary_evidence or [])
    assert set(packet.evidence_digests) == linked
    assert set(packet.evidence_digests) == {"EVI-0001", "EVI-0002", "EVI-0003"}
    for evidence_id, digest in packet.evidence_digests.items():
        target = resolve_object(report, evidence_id)
        assert digest == subject_digest(target, project_id="sample-project")


def test_evidence_count_is_derived_from_the_digest_map(tmp_path: Path) -> None:
    """The number a reviewer confirms is the number actually written."""

    packet = _packet(_repo(tmp_path))
    assert packet.evidence_count == len(packet.evidence_digests) == 3
    assert packet.evidence_count == len(packet.supporting) + len(packet.contrary)


# --- refusals -------------------------------------------------------------


def test_packet_refuses_project_with_errors(tmp_path: Path) -> None:
    """A capsule holding errors cannot present a trustworthy packet."""

    repo = _repo(tmp_path)
    write_yaml(
        repo / ".research" / "questions" / "Q-0002.yaml",
        question_data(id="Q-0002", unknown_field="nope"),
    )
    with pytest.raises(ReviewBlockedError) as exc:
        _packet(repo)
    assert exc.value.findings
    assert "fix these errors" in str(exc.value)


def test_packet_refuses_non_claim_subject(tmp_path: Path) -> None:
    with pytest.raises(CapsuleError, match="reviews of claims only"):
        _packet(_repo(tmp_path), "Q-0001")


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        ("draft", "is draft"),
        ("withdrawn", "not open for review"),
    ],
)
def test_review_requires_evidence_linked_claim(
    tmp_path: Path,
    status: str,
    expected: str,
) -> None:
    repo = _repo(tmp_path, claim_status=status)
    with pytest.raises(CapsuleError, match=expected):
        _packet(repo)


def test_review_refuses_already_accepted_claim(tmp_path: Path) -> None:
    """R0 acceptance is existential, so a later verdict cannot override it.

    A new reject would be recorded while the earlier approval kept satisfying
    the gate, which reads as a rejection that changed nothing.
    """

    repo = _repo(tmp_path)
    packet = _packet(repo)
    write_review(
        packet, build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    )
    claim_file = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    claim_file.write_text(
        claim_file.read_text(encoding="utf-8").replace(
            "status: evidence_linked", "status: accepted"
        ),
        encoding="utf-8",
    )
    assert validate_project(repo).ok
    with pytest.raises(CapsuleError, match="already accepted"):
        _packet(repo)


def test_review_requires_nonempty_findings(tmp_path: Path) -> None:
    packet = _packet(_repo(tmp_path))
    with pytest.raises(CapsuleError, match="non-empty findings"):
        build_review(packet, verdict=Verdict.APPROVE, findings="   \n  ")


# --- authoring ------------------------------------------------------------


def test_review_id_follows_next_id(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    assert _packet(repo).review_id == "REV-0001"
    packet = _packet(repo)
    write_review(
        packet, build_review(packet, verdict=Verdict.REVISE, findings=FINDINGS)
    )
    assert _packet(repo).review_id == "REV-0002"


@pytest.mark.parametrize("verdict", list(Verdict))
def test_written_review_validates_under_canonical_schema(
    tmp_path: Path,
    verdict: Verdict,
) -> None:
    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=verdict, findings=FINDINGS)
    target = write_review(packet, review)

    assert target == repo / ".research" / "reviews" / "REV-0001.yaml"
    reloaded = parse_object(yaml.safe_load(target.read_text(encoding="utf-8")))
    assert isinstance(reloaded, Review)
    assert reloaded.status == "concluded"
    assert reloaded.reviewer_kind == "human"
    assert reloaded.verdict == verdict
    assert reloaded.subject == "CLAIM-0001"
    assert reloaded.subject_digest == packet.claim_digest
    assert reloaded.evidence_digests == packet.evidence_digests
    assert validate_project(repo).ok


def test_review_does_not_mutate_the_claim(tmp_path: Path) -> None:
    """Promoting a Claim to accepted stays a deliberate human edit."""

    repo = _repo(tmp_path)
    claim_file = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    before = claim_file.read_bytes()
    packet = _packet(repo)
    write_review(
        packet, build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    )
    assert claim_file.read_bytes() == before


def test_written_review_satisfies_the_acceptance_gate(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    packet = _packet(repo)
    write_review(
        packet, build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    )
    claim_file = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    claim_file.write_text(
        claim_file.read_text(encoding="utf-8").replace(
            "status: evidence_linked", "status: accepted"
        ),
        encoding="utf-8",
    )
    report = validate_project(repo)
    assert report.ok, [item.message for item in report.errors]


def test_partial_evidence_coverage_would_not_gate_acceptance(tmp_path: Path) -> None:
    """Why the packet binds the complete map rather than the supporting side.

    Written by hand here, because the command has no way to produce it.
    """

    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    partial = review.model_dump(mode="json", exclude_none=True)
    partial["evidence_digests"] = {
        key: value
        for key, value in partial["evidence_digests"].items()
        if key != "EVI-0002"
    }
    write_yaml(repo / ".research" / "reviews" / "REV-0001.yaml", partial)
    claim_file = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    claim_file.write_text(
        claim_file.read_text(encoding="utf-8").replace(
            "status: evidence_linked", "status: accepted"
        ),
        encoding="utf-8",
    )
    report = validate_project(repo)
    assert not report.ok
    assert E_EVIDENCE_DIGESTS_INCOMPLETE in {item.code for item in report.findings}


# --- atomicity ------------------------------------------------------------


def test_write_refuses_existing_review_path(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    write_review(packet, review)
    with pytest.raises(CapsuleError, match="refusing to overwrite"):
        write_review(packet, review)


def test_write_failure_leaves_no_partial_file(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    before = snapshot_files(repo)

    def boom(src: object, dst: object) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(CapsuleError, match="cannot write"):
        write_review(packet, review)

    reviews = repo / ".research" / "reviews"
    assert not (reviews / "REV-0001.yaml").exists()
    assert list(reviews.glob("*.tmp")) == []
    assert list(reviews.glob(".*")) == []
    assert snapshot_files(repo) == before


def test_invalid_constructed_review_is_not_written(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Validation runs before anything reaches the filesystem."""

    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    stale = review.model_copy(
        update={"evidence_digests": {"EVI-9999": packet.claim_digest}}
    )
    before = snapshot_files(repo)
    with pytest.raises(ReviewBlockedError, match="refusing to write an invalid review"):
        write_review(packet, stale)
    assert snapshot_files(repo) == before
    assert not (repo / ".research" / "reviews" / "REV-0001.yaml").exists()


def test_review_file_is_canonical_yaml(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    packet = _packet(repo)
    review = build_review(packet, verdict=Verdict.APPROVE, findings=FINDINGS)
    text = write_review(packet, review).read_text(encoding="utf-8")
    assert text.endswith("\n")
    assert "!!python" not in text
    assert "id: REV-0001" in text
    for evidence_id in packet.evidence_digests:
        assert evidence_id in text
