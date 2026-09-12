"""The one door between proposed work and a project's scientific record.

These tests run against a real Git repository with a real Research Capsule and
real R0 validation, because what is being checked is exactly the thing that
would be worthless if the capsule were faked: that the object promotion produces
is a valid, weak, traceable draft, and that nothing stronger can come out of
this path.

Four properties:

* only a draft is ever written -- never an accepted Claim, never a Review;
* a historical experiment never acquires preregistration fields;
* only an interactive human may run it;
* the draft records which proposal and item it came from.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from research_os.capsule import validate_project
from research_os.errors import (
    EXIT_ERROR,
    EXIT_OK,
    CapsuleError,
    PromotionRefusedError,
    ProposalValidationError,
)
from research_os.proposal import commands as propose_commands
from research_os.proposal.models import (
    ProposalGrounding,
    ResearchProposal,
)
from research_os.proposal.promote import prepare_promotion, write_promotion
from research_os.proposal.store import ProposalStore
from tests.proposal_helpers import init_capsule_project, item, proposal_payload


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    for name, subdirectory in (
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
        ("RESEARCH_OS_STATE_HOME", "state"),
    ):
        path = root / subdirectory
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return root / "state"


def build_proposal(
    project: Path,
    *,
    items: list[dict] | None = None,
    proposal_id: str = "PROP-20260912T101500Z-0a1b2c3d",
) -> ResearchProposal:
    payload = proposal_payload(items=items)
    return ResearchProposal.model_validate(
        {
            **payload,
            "proposal_id": proposal_id,
            "project_path": str(project),
            "project_id": "widget-study",
            "base_commit": "a" * 40,
            "goal": "Settle whether deformation is linear",
            "grounding": ProposalGrounding(
                capsule_ids=["Q-0001", "HYP-0001"]
            ).model_dump(),
            "provider": "fake",
            "model": "fake-planner",
        }
    )


def loaded(path: Path) -> dict:
    return yaml.safe_load(path.read_text(encoding="utf-8"))


# -- what promotion produces --------------------------------------------------


def test_a_hypothesis_is_promoted_as_a_draft(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)

    prepared = prepare_promotion(proposal, "PR-001")
    record = write_promotion(prepared)

    written = loaded(project / record.written_path)
    assert record.object_type == "hypothesis"
    assert record.object_status == "draft"
    assert written["status"] == "draft"
    assert written["falsification"]
    assert written["addresses"] == ["Q-0001"]
    assert validate_project(project).ok


def test_a_promoted_claim_is_a_draft_and_never_accepted(
    research_home: Path, tmp_path: Path
) -> None:
    """The single most important thing this module refuses."""

    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(
        project,
        items=[item(kind="claim", addresses=["HYP-0001"], falsification=None)],
    )

    record = write_promotion(prepare_promotion(proposal, "PR-001"))

    written = loaded(project / record.written_path)
    assert written["status"] == "draft"
    assert written["status"] != "accepted"
    assert validate_project(project).ok


def test_nothing_here_can_write_a_review(research_home: Path, tmp_path: Path) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)

    write_promotion(prepare_promotion(proposal, "PR-001"))

    reviews = list((project / ".research" / "reviews").glob("*.yaml"))
    assert reviews == [], "a Review is a human act and has no promotion path"


def test_a_prospective_experiment_keeps_its_decision_rule(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(
        project,
        items=[item(kind="experiment", basis="prospective", addresses=["HYP-0001"])],
    )

    record = write_promotion(prepare_promotion(proposal, "PR-001"))

    written = loaded(project / record.written_path)
    assert written["status"] == "draft"
    assert written["decision_rule"]
    assert written["primary_metrics"] == ["deformation_mm"]


def test_a_historical_experiment_never_acquires_a_preregistration(
    research_home: Path, tmp_path: Path
) -> None:
    """A decision rule written after the outcome is a description, not a prediction."""

    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(
        project,
        items=[
            item(
                kind="experiment",
                basis="historical",
                expected_direction="",
                addresses=["HYP-0001"],
            )
        ],
    )

    record = write_promotion(prepare_promotion(proposal, "PR-001"))

    written = loaded(project / record.written_path)
    assert written["status"] == "draft"
    assert "decision_rule" not in written, (
        "a retrospective rule is not a preregistration"
    )
    assert "predictions" not in written
    assert "HISTORICAL" in written["notes"]
    assert "retrospective" in written["notes"]
    assert validate_project(project).ok


def test_a_promoted_object_records_where_it_came_from(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)

    record = write_promotion(prepare_promotion(proposal, "PR-001"))

    notes = loaded(project / record.written_path)["notes"]
    assert proposal.proposal_id in notes
    assert "PR-001" in notes
    assert "fake-planner" in notes
    assert "DRAFT" in notes
    assert "Nothing about it has been scientifically reviewed or accepted" in notes
    assert "accepted" not in notes.replace(
        "Nothing about it has been scientifically reviewed or accepted", ""
    ), "nothing else in the note may suggest this was accepted"


def test_an_evidence_interpretation_cannot_be_promoted(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(
        project, items=[item(kind="evidence_interpretation", addresses=[])]
    )

    with pytest.raises(ProposalValidationError, match="cannot be promoted"):
        prepare_promotion(proposal, "PR-001")


def test_an_unknown_item_is_refused(research_home: Path, tmp_path: Path) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)

    with pytest.raises(ProposalValidationError, match="no proposed item"):
        prepare_promotion(proposal, "PR-404")


def test_a_project_with_no_capsule_has_nothing_to_promote_into(
    research_home: Path, tmp_path: Path
) -> None:
    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(bare), check=True)
    (bare / "README.md").write_text("x\n", encoding="utf-8")
    proposal = build_proposal(bare)

    with pytest.raises(ProposalValidationError, match="no Research Capsule"):
        prepare_promotion(proposal, "PR-001")


def test_promotion_never_overwrites_a_scientific_file(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)
    prepared = prepare_promotion(proposal, "PR-001")
    write_promotion(prepared)

    with pytest.raises(CapsuleError, match="refusing to overwrite"):
        write_promotion(prepared)


def test_two_promotions_get_distinct_ids(research_home: Path, tmp_path: Path) -> None:
    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project, items=[item(), item(item_id="PR-002")])

    first = write_promotion(prepare_promotion(proposal, "PR-001"))
    second = write_promotion(prepare_promotion(proposal, "PR-002"))

    assert first.object_id != second.object_id
    assert validate_project(project).ok


def test_preparing_changes_nothing_on_disk(research_home: Path, tmp_path: Path) -> None:
    """A researcher reads the preview before anything is created."""

    project = init_capsule_project(tmp_path / "project")
    proposal = build_proposal(project)
    before = sorted(str(p) for p in (project / ".research").rglob("*"))

    prepared = prepare_promotion(proposal, "PR-001")

    assert prepared.document
    assert sorted(str(p) for p in (project / ".research").rglob("*")) == before


# -- who may open the door ----------------------------------------------------


def test_a_non_interactive_process_may_not_promote(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pipeline must not be able to put science into a project."""

    import research_os.cli as cli_module

    project = init_capsule_project(tmp_path / "project")
    store = ProposalStore.create(build_proposal(project))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: False)

    from research_os.cli import _build_parser

    args = _build_parser().parse_args(
        ["propose", "promote", store.proposal_id, "--item", "PR-001"]
    )
    with pytest.raises(PromotionRefusedError, match="interactive terminal"):
        propose_commands.dispatch(args)

    assert list((project / ".research" / "hypotheses").glob("HYP-0002*")) == []


def test_declining_the_confirmation_writes_nothing(
    research_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import research_os.cli as cli_module

    project = init_capsule_project(tmp_path / "project")
    store = ProposalStore.create(build_proposal(project))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "_confirm", lambda _question: False)

    from research_os.cli import _build_parser

    args = _build_parser().parse_args(
        ["propose", "promote", store.proposal_id, "--item", "PR-001"]
    )
    code = propose_commands.dispatch(args)

    assert code == EXIT_ERROR
    assert "nothing was written" in capsys.readouterr().out
    assert store.promotions() == []
    assert validate_project(project).ok


def test_confirming_writes_the_draft_and_records_it(
    research_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import research_os.cli as cli_module

    project = init_capsule_project(tmp_path / "project")
    store = ProposalStore.create(build_proposal(project))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "_confirm", lambda _question: True)

    from research_os.cli import _build_parser

    args = _build_parser().parse_args(
        ["propose", "promote", store.proposal_id, "--item", "PR-001"]
    )
    code = propose_commands.dispatch(args)

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "DRAFT" in printed
    assert "has not been reviewed or accepted" in printed
    promotions = store.promotions()
    assert len(promotions) == 1
    assert promotions[0].object_status == "draft"
    assert any(entry["event"] == "item_promoted" for entry in store.iter_events())
    assert validate_project(project).ok


def test_the_preview_shows_exactly_what_would_be_written(
    research_home: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import research_os.cli as cli_module

    project = init_capsule_project(tmp_path / "project")
    store = ProposalStore.create(build_proposal(project))
    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli_module, "_confirm", lambda _question: True)

    from research_os.cli import _build_parser

    propose_commands.dispatch(
        _build_parser().parse_args(
            ["propose", "promote", store.proposal_id, "--item", "PR-001"]
        )
    )

    printed = capsys.readouterr().out
    record = store.promotions()[0]
    written = (project / record.written_path).read_text(encoding="utf-8")
    assert "status: draft" in printed
    assert written.splitlines()[0] in printed
