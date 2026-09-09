"""End-to-end workflow a researcher actually performs.

Everything here goes through supported ``researchctl`` commands, with two
deliberate exceptions written by hand: promoting a Claim to ``accepted`` and
resetting it to ``evidence_linked``. WP-B has no ``set-status``, and those edits
staying manual is the point -- the kernel never promotes science on its own.

The purpose is to prove a researcher can drive the review gate without
hand-computing a single digest, and can recover after evidence changes
underneath an approval.
"""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from research_os import cli as cli_module
from research_os.cli import main
from research_os.errors import (
    E_STALE_REVIEW_DIGEST,
    EXIT_ERROR,
    W_STALE_EVIDENCE_DIGEST,
)
from research_os.models import DIGEST_RE
from tests.fs_helpers import make_git_repo, write_reviewable_capsule


def _run(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def _interactive_review(monkeypatch: pytest.MonkeyPatch, *replies: str) -> None:
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    pending = list(replies)

    def fake_input(prompt: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def _set_claim_status(repo: Path, old: str, new: str) -> None:
    """Edit Claim status by hand, as a researcher must."""

    path = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    text = path.read_text(encoding="utf-8")
    assert f"status: {old}" in text
    path.write_text(text.replace(f"status: {old}", f"status: {new}"), encoding="utf-8")


def test_human_review_workflow_end_to_end(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo = make_git_repo(tmp_path / "smoke")

    # init-project, then research-shaped canonical YAML.
    assert _run(monkeypatch, "init-project", str(repo), "--id", "smoke") == 0
    (repo / ".research").rename(repo / ".research.generated")
    write_reviewable_capsule(repo, project_id="smoke")
    capsys.readouterr()

    # validate-project
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    assert capsys.readouterr().out.strip() == "OK"

    # digest -- no hand-computed hashes anywhere in this workflow.
    assert _run(monkeypatch, "digest", "CLAIM-0001", str(repo)) == 0
    claim_digest = capsys.readouterr().out.strip()
    assert DIGEST_RE.fullmatch(claim_digest) is not None

    # interactive human review
    _interactive_review(monkeypatch, "approve", "Evidence checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    out = capsys.readouterr().out
    assert claim_digest in out
    assert "all 3 evidence" in out
    assert "Wrote .research/reviews/REV-0001.yaml" in out

    review = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert review["subject_digest"] == claim_digest
    assert set(review["evidence_digests"]) == {"EVI-0001", "EVI-0002", "EVI-0003"}

    # manual promotion, then the gate is satisfied
    _set_claim_status(repo, "evidence_linked", "accepted")
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    assert capsys.readouterr().out.strip() == "OK"

    # mutate evidence -- the approval must go stale
    evidence = repo / ".research" / "evidence" / "EVI-0002.yaml"
    evidence.write_text(
        evidence.read_text(encoding="utf-8").replace(
            "The source reports weak not-X near 210 K.",
            "The source in fact reports strong not-X near 210 K.",
        ),
        encoding="utf-8",
    )
    assert _run(monkeypatch, "validate-project", str(repo)) == EXIT_ERROR
    stale = capsys.readouterr().out
    assert E_STALE_REVIEW_DIGEST in stale
    assert W_STALE_EVIDENCE_DIGEST in stale

    # recovery: re-review is refused while the capsule holds errors, and the
    # blocking finding is shown rather than restated -- here the stale approval
    # itself is the error, which is the most useful thing to print.
    _interactive_review(monkeypatch, "approve", "Re-checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == EXIT_ERROR
    refused = capsys.readouterr()
    assert E_STALE_REVIEW_DIGEST in refused.out
    assert "fix these errors before reviewing CLAIM-0001" in refused.err

    _set_claim_status(repo, "accepted", "evidence_linked")
    _interactive_review(
        monkeypatch, "approve", "Re-checked the revised source.", "", "y"
    )
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    second_out = capsys.readouterr().out
    assert "Wrote .research/reviews/REV-0002.yaml" in second_out

    # the fresh review binds the NEW evidence digest
    second = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0002.yaml").read_text(encoding="utf-8")
    )
    assert second["subject_digest"] == claim_digest  # Claim science never changed
    assert (
        second["evidence_digests"]["EVI-0002"] != review["evidence_digests"]["EVI-0002"]
    )
    assert (
        second["evidence_digests"]["EVI-0001"] == review["evidence_digests"]["EVI-0001"]
    )

    # valid state restored: the historical review still warns, which is correct
    _set_claim_status(repo, "evidence_linked", "accepted")
    assert _run(monkeypatch, "validate-project", str(repo)) == 0
    final = capsys.readouterr().out
    assert W_STALE_EVIDENCE_DIGEST in final
    assert E_STALE_REVIEW_DIGEST not in final


def test_workflow_never_required_a_hand_computed_digest(
    tmp_path: Path,
    data_home: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The digest the CLI prints is the digest the review binds.

    This is the whole point of WP-B: before it, a researcher had to import
    research_os.digests to obtain these values.
    """

    repo = make_git_repo(tmp_path / "smoke")
    write_reviewable_capsule(repo, project_id="smoke")

    printed: dict[str, str] = {}
    for object_id in ("CLAIM-0001", "EVI-0001", "EVI-0002", "EVI-0003"):
        assert _run(monkeypatch, "digest", object_id, str(repo)) == 0
        printed[object_id] = capsys.readouterr().out.strip()

    _interactive_review(monkeypatch, "approve", "Checked.", "", "y")
    assert _run(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    capsys.readouterr()

    review = yaml.safe_load(
        (repo / ".research" / "reviews" / "REV-0001.yaml").read_text(encoding="utf-8")
    )
    assert review["subject_digest"] == printed["CLAIM-0001"]
    assert review["evidence_digests"] == {
        "EVI-0001": printed["EVI-0001"],
        "EVI-0002": printed["EVI-0002"],
        "EVI-0003": printed["EVI-0003"],
    }
