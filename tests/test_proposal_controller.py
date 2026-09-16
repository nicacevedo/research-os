"""The read-only path from a goal to an assessed proposal.

A proposal run is the one controller that touches nothing. It reads a project's
capsule, spends at most three model calls on context-only workers, and writes
only into runtime state. These tests hold it to that: the project is byte-for-
byte unchanged afterwards, the workers really are tool-free, and a dirty working
tree is fine -- because a researcher wanting a proposal about work in progress
should get one rather than an error about uncommitted changes.

They also check the honest-reporting half. An assessment is labelled as advisory
and never as a Review, a literature pass that finds nothing says so rather than
producing an ungrounded proposal that looks grounded, and everything a worker
said is archived.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from research_os.automation.models import Access, Role
from research_os.errors import (
    AutomationError,
    ProposalValidationError,
    ProviderInvocationError,
)
from research_os.literature.models import AuthorRecord
from research_os.literature.store import LiteratureStore
from research_os.proposal.models import ProposalVerdict
from research_os.proposal.report import render_proposal
from research_os.proposal.store import ProposalStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.proposal_helpers import (
    assessment_payload,
    fake_config,
    init_capsule_project,
    item,
    make_controller,
    proposal_payload,
)


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


def provider(
    *,
    proposal: dict | None = None,
    assessment: dict | None = None,
    literature: dict | None = None,
) -> FakeProvider:
    responses = {
        str(Role.PLANNER): [
            ScriptedResponse(structured=proposal or proposal_payload())
        ],
        str(Role.REVIEWER): [
            ScriptedResponse(structured=assessment or assessment_payload())
        ],
    }
    if literature is not None:
        responses[str(Role.LITERATURE)] = [ScriptedResponse(structured=literature)]
    return FakeProvider(responses=responses)


def snapshot(project: Path) -> dict[str, bytes]:
    return {
        str(path.relative_to(project)): path.read_bytes()
        for path in sorted(project.rglob("*"))
        if path.is_file() and ".git/" not in str(path.relative_to(project))
    }


# -- what a proposal run does and does not touch ------------------------------


def test_a_proposal_run_leaves_the_project_byte_for_byte_unchanged(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    before = snapshot(project)
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    assert snapshot(project) == before
    assert outcome.proposal.project_id == "widget-study"


def test_a_proposal_run_creates_no_worktree_and_no_branch(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller({"fake": provider()})

    controller.propose(project_path=project, goal="Settle Q-0001")

    worktrees = subprocess.run(
        ["git", "worktree", "list"],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert worktrees.count("\n") == 1, "only the project's own checkout"
    branches = subprocess.run(
        ["git", "branch", "--list"],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "automation/" not in branches


def test_a_dirty_working_tree_is_fine(research_home: Path, tmp_path: Path) -> None:
    """A researcher wanting advice about work in progress should get advice."""

    project = init_capsule_project(tmp_path / "project")
    (project / "analysis.py").write_text("# half-finished\n", encoding="utf-8")
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    assert outcome.proposal.items
    status = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(project),
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    assert "analysis.py" in status, "the run neither committed nor reverted it"


def test_every_worker_is_context_only_and_toolless(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    fake = provider()
    controller = make_controller({"fake": fake})

    controller.propose(project_path=project, goal="Settle Q-0001")

    assert fake.calls
    for call in fake.calls:
        assert call.read_only is True
        assert call.access is Access.CONTEXT_ONLY
        assert call.tools == ()
        assert project not in Path(call.cwd).parents
        assert Path(call.cwd) != project


def test_a_role_with_tools_is_refused(research_home: Path, tmp_path: Path) -> None:
    """A future configuration change must not silently arm a proposal worker."""

    from research_os.automation.models import RoleSetting

    project = init_capsule_project(tmp_path / "project")
    config = fake_config()
    config.roles["planner"] = RoleSetting(
        provider="fake",
        model="fake-planner",
        read_only=True,
        access=Access.SNAPSHOT_READ,
        tools=["Read", "Glob", "Grep"],
    )
    controller = make_controller({"fake": provider()}, config=config)

    with pytest.raises(AutomationError, match="context-only"):
        controller.propose(project_path=project, goal="Settle Q-0001")


# -- what it produces ---------------------------------------------------------


def test_the_proposal_is_grounded_in_this_project_s_objects(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    assert set(outcome.proposal.grounding.capsule_ids) == {"Q-0001", "HYP-0001"}
    assert outcome.proposal.items[0].addresses == ["Q-0001"]


def test_a_proposal_citing_something_this_project_lacks_fails_the_run(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller(
        {
            "fake": provider(
                proposal=proposal_payload(items=[item(addresses=["CLAIM-9"])])
            )
        }
    )

    with pytest.raises(ProposalValidationError, match="not supplied"):
        controller.propose(project_path=project, goal="Settle Q-0001")


def test_the_assessment_is_recorded_and_labelled_advisory(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller(
        {
            "fake": provider(
                assessment=assessment_payload(
                    "PASS_WITH_REPAIR",
                    findings=[
                        {
                            "item_id": "PR-001",
                            "severity": "major",
                            "message": "the falsification is not reachable with this rig",
                        }
                    ],
                )
            )
        }
    )

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    assert outcome.assessment is not None
    assert outcome.assessment.verdict is ProposalVerdict.PASS_WITH_REPAIR
    assert outcome.assessment.blocking
    rendered = render_proposal(outcome.proposal, assessment=outcome.assessment)
    assert "NOT a scientific Review" in rendered
    assert "Nothing above is part of this project's scientific record" in rendered


def test_the_report_says_no_claim_was_accepted(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    rendered = render_proposal(outcome.proposal, assessment=outcome.assessment)
    assert "No Claim was" in rendered
    assert "no Review was written" in rendered
    assert "researchctl propose promote" in rendered


def test_everything_the_workers_said_is_archived(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(project_path=project, goal="Settle Q-0001")

    store = ProposalStore.open(outcome.proposal.proposal_id)
    assert store.proposal_file.is_file()
    assert store.assessment_file.is_file()
    assert list(store.path("prompts").glob("INV-*.txt"))
    events = [entry["event"] for entry in store.iter_events()]
    assert "proposal_created" in events
    assert "proposal_validated" in events
    assert "proposal_assessed" in events


def test_an_assessment_can_be_skipped_to_save_a_call(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller({"fake": provider()})

    outcome = controller.propose(
        project_path=project, goal="Settle Q-0001", assess=False
    )

    assert outcome.assessment is None
    assert outcome.model_calls == 1
    assert "(none was run)" in render_proposal(outcome.proposal)


def test_a_failed_worker_stops_the_run_rather_than_producing_half_a_proposal(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    fake = FakeProvider(
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=None, error="provider exploded", exit_code=1
                )
            ]
        }
    )
    controller = make_controller({"fake": fake})

    with pytest.raises(ProviderInvocationError, match="proposal worker failed"):
        controller.propose(project_path=project, goal="Settle Q-0001")

    assert ProposalStore.list_proposal_ids() == ()


def test_a_project_with_no_capsule_still_gets_a_proposal(
    research_home: Path, tmp_path: Path
) -> None:
    """It just has nothing to cite, and the context says so."""

    bare = tmp_path / "bare"
    bare.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=str(bare), check=True)
    subprocess.run(
        ["git", "config", "user.email", "t@example.invalid"], cwd=str(bare), check=True
    )
    subprocess.run(["git", "config", "user.name", "tests"], cwd=str(bare), check=True)
    (bare / "README.md").write_text("x\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=str(bare), check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=str(bare), check=True)

    controller = make_controller(
        {
            "fake": provider(
                proposal=proposal_payload(
                    items=[item(kind="question", addresses=[], falsification=None)],
                    next_actions=[],
                    uncertainties=[],
                )
            )
        }
    )

    outcome = controller.propose(project_path=bare, goal="What should we ask?")

    assert outcome.proposal.grounding.capsule_ids == []
    assert outcome.proposal.items[0].kind.value == "question"


# -- the literature pass ------------------------------------------------------


def literature_payload(keys: list[str]) -> dict:
    return {
        "summary": "One work bears on widget deformation.",
        "assessments": [
            {
                "work_key": keys[0],
                "relevance": "high",
                "contribution": "Measures deformation under load.",
                "methods": [],
                "assumptions": [],
                "datasets": [],
                "findings": [],
                "limitations": [],
            }
        ],
        "findings": [
            {
                "id": "L-001",
                "statement": "Deformation is reported linear below 10N.",
                "importance": "high",
                "confidence": "medium",
                "work_keys": [keys[0]],
            }
        ],
        "disagreements": [],
        "uncertainties": [],
        "recommended_followup": [],
    }


def seeded_literature() -> LiteratureStore:
    store = LiteratureStore.open_memory()
    store.ingest(
        provider="test",
        payload={},
        fields={
            "title": "Widget deformation under load",
            "abstract": "We measure how widgets deform when loaded.",
            "publication_year": 2021,
        },
        identifiers={"doi": "10.1000/widget"},
        authors=[AuthorRecord(position=0, name="Jane Roe")],
    )
    return store


def test_a_literature_pass_grounds_the_proposal_in_retrieved_works(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    store = seeded_literature()
    controller = make_controller(
        {
            "fake": provider(
                literature=literature_payload(["doi:10.1000/widget"]),
                proposal=proposal_payload(
                    items=[item(grounded_in_literature=["doi:10.1000/widget"])]
                ),
            )
        },
        literature_store=store,
    )

    outcome = controller.propose(
        project_path=project, goal="widget deformation load", with_literature=True
    )

    assert outcome.literature_keys == ("doi:10.1000/widget",)
    assert outcome.proposal.grounding.literature_keys == ["doi:10.1000/widget"]
    assert outcome.model_calls == 3


def test_an_empty_index_produces_an_ungrounded_proposal_that_says_so(
    research_home: Path, tmp_path: Path
) -> None:
    """Pretending otherwise would make an ungrounded proposal look grounded."""

    project = init_capsule_project(tmp_path / "project")
    controller = make_controller(
        {"fake": provider()}, literature_store=LiteratureStore.open_memory()
    )

    outcome = controller.propose(
        project_path=project, goal="Settle Q-0001", with_literature=True
    )

    assert outcome.literature_keys == ()
    assert outcome.proposal.grounding.literature_keys == []
    assert outcome.model_calls == 2, "no literature worker was invoked"


def test_a_proposal_cannot_cite_a_work_the_literature_pass_did_not_supply(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller(
        {
            "fake": provider(
                literature=literature_payload(["doi:10.1000/widget"]),
                proposal=proposal_payload(
                    items=[item(grounded_in_literature=["doi:10.1038/famous"])]
                ),
            )
        },
        literature_store=seeded_literature(),
    )

    with pytest.raises(ProposalValidationError, match="retrieved work"):
        controller.propose(
            project_path=project,
            goal="widget deformation load",
            with_literature=True,
        )


def test_the_literature_report_is_archived_with_the_proposal(
    research_home: Path, tmp_path: Path
) -> None:
    project = init_capsule_project(tmp_path / "project")
    controller = make_controller(
        {
            "fake": provider(
                literature=literature_payload(["doi:10.1000/widget"]),
                proposal=proposal_payload(
                    items=[item(grounded_in_literature=["doi:10.1000/widget"])]
                ),
            )
        },
        literature_store=seeded_literature(),
    )

    outcome = controller.propose(
        project_path=project, goal="widget deformation load", with_literature=True
    )

    store = ProposalStore.open(outcome.proposal.proposal_id)
    assert store.path("literature", "report.json").is_file()


# -- the decision queue is in the order the proposals were made -------------
def test_proposals_are_listed_by_when_they_were_made(research_home: Path) -> None:
    """A reserved id's timestamp is derived, not observed, so it cannot sort.

    `reserved_proposal_id` derives the whole id from the reservation key so a
    retry recomputes it, which means the timestamp in the id is a placeholder --
    `19700101T000000Z`, deliberately implausible rather than a plausible lie.
    Listing by id therefore put every runtime proposal at the front of the
    researcher's queue, before everything they proposed themselves, in digest
    order among themselves.
    """

    from research_os.proposal.store import proposals_root

    root = proposals_root()
    root.mkdir(parents=True, exist_ok=True)
    made = {
        "PROP-19700101T000000Z-aaaaaaaa": "2026-09-14T12:00:00Z",
        "PROP-20260910T090000Z-bbbbbbbb": "2026-09-10T09:00:00Z",
        "PROP-19700101T000000Z-cccccccc": "2026-09-12T18:30:00Z",
    }
    for proposal_id, created_at in made.items():
        directory = root / proposal_id
        directory.mkdir()
        (directory / "proposal.json").write_text(
            json.dumps({"proposal_id": proposal_id, "created_at": created_at}),
            encoding="utf-8",
        )

    assert ProposalStore.list_proposal_ids() == (
        "PROP-20260910T090000Z-bbbbbbbb",
        "PROP-19700101T000000Z-cccccccc",
        "PROP-19700101T000000Z-aaaaaaaa",
    )


def test_a_proposal_that_cannot_be_read_stays_in_the_listing(
    research_home: Path,
) -> None:
    """Dropping it would hide a proposal a person may be waiting on."""

    from research_os.proposal.store import proposals_root

    root = proposals_root()
    root.mkdir(parents=True, exist_ok=True)
    broken = root / "PROP-20260910T090000Z-dddddddd"
    broken.mkdir()
    (broken / "proposal.json").write_text("{ not json", encoding="utf-8")

    assert broken.name in ProposalStore.list_proposal_ids()
