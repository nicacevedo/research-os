"""A researcher can say no, and saying no has to mean something.

`ProposalStore` recorded promotions and nothing else, so the lifecycle had one
half of a decision in it. "I read this and I do not want it" had no
representation, which made it indistinguishable from "nobody has opened this
yet" -- and the runtime's cross-cycle deduplication asks exactly that question
before it decides whether to propose again.

The consequence was concrete and got worse the better the deduplication worked:
a declined proposal stayed pending forever, so every later cycle over the same
findings was answered with "an equivalent pending proposal exists" and the
runtime never proposed about those findings again. The researcher's rejection
turned into permanent silence on the subject they had rejected.

These tests cover the three things that had to be true at once: the decline is
durable, it is human-only, and the runtime reads it.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

import pytest

from research_os.errors import PromotionRefusedError, ProposalNotFoundError
from research_os.proposal import commands as propose_commands
from research_os.proposal.models import (
    DeclineRecord,
    PromotionRecord,
    ProposalGrounding,
    ResearchProposal,
    ScientificBasisSnapshot,
)
from research_os.proposal.store import ProposalStore
from tests.proposal_helpers import item, proposal_payload

PROJECT_ID = "decline-project"


def make_proposal(
    *,
    proposal_id: str,
    project_path: Path,
    finding_digest: str | None = "fp-digest",
    items: list[dict[str, Any]] | None = None,
) -> ResearchProposal:
    payload = proposal_payload(
        items=items
        or [
            item(item_id="PR-001"),
            item(item_id="PR-002", title="A second thing", kind="question"),
        ]
    )
    return ResearchProposal.model_validate(
        {
            "proposal_id": proposal_id,
            "project_path": str(project_path),
            "project_id": PROJECT_ID,
            "goal": "Settle Q-0001.",
            "grounding": ProposalGrounding(
                capsule_ids=["Q-0001", "HYP-0001"]
            ).model_dump(),
            "provider": "fake",
            "model": "fake-proposer",
            **payload,
            "scientific_basis": ScientificBasisSnapshot(
                project_id=PROJECT_ID, finding_packet_digest=finding_digest
            ).model_dump(mode="json"),
        }
    )


@pytest.fixture
def stored(automation_home: Path, tmp_path: Path) -> ProposalStore:
    proposal = make_proposal(
        proposal_id="PROP-20260101T000000Z-aaaaaaaa", project_path=tmp_path / "project"
    )
    return ProposalStore.create(proposal)


# -- the record ---------------------------------------------------------------


def test_a_decline_is_durable_and_carries_its_reason(stored: ProposalStore) -> None:
    """Six months later the reason is the part that is still worth having."""

    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id,
            item_id="PR-001",
            reason="Celer already answers this, so measuring it again buys nothing.",
        )
    )

    reopened = ProposalStore.open(stored.proposal_id)
    [record] = reopened.declines()
    assert record.item_id == "PR-001"
    assert record.declined_by == "human"
    assert "Celer" in record.reason
    assert record.declined_at


def test_a_decline_is_appended_never_rewritten(stored: ProposalStore) -> None:
    """A reconsidered item gets a promotion *after* the decline. That is the history."""

    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id, item_id="PR-001", reason="not now"
        )
    )
    stored.record_promotion(
        PromotionRecord(
            proposal_id=stored.proposal_id,
            item_id="PR-001",
            object_id="HYP-0007",
            object_type="hypothesis",
            object_status="draft",
            project_path="/tmp/project",
            written_path=".research/hypotheses/HYP-0007.yaml",
        )
    )

    reopened = ProposalStore.open(stored.proposal_id)
    assert [record.item_id for record in reopened.declines()] == ["PR-001"]
    assert [record.item_id for record in reopened.promotions()] == ["PR-001"]


def test_declines_live_beside_promotions_not_inside_them(
    stored: ProposalStore,
) -> None:
    """Two files, because a reader that has to check a `kind` field can get it wrong."""

    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id, item_id="PR-001", reason="not now"
        )
    )
    assert stored.declines_file.name == "declines.jsonl"
    assert stored.declines_file.is_file()
    assert not stored.promotions_file.exists()
    line = json.loads(stored.declines_file.read_text(encoding="utf-8").strip())
    assert line["item_id"] == "PR-001"


def test_a_decline_needs_a_reason() -> None:
    """A decline with no reason is indistinguishable from neglect."""

    with pytest.raises(ValueError):
        DeclineRecord(proposal_id="PROP-1", item_id="PR-001", reason="   ")


def test_decided_items_are_promotions_and_declines_together(
    stored: ProposalStore,
) -> None:
    """The question downstream asks is "has a person acted", not "which way"."""

    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id, item_id="PR-001", reason="not now"
        )
    )
    stored.record_promotion(
        PromotionRecord(
            proposal_id=stored.proposal_id,
            item_id="PR-002",
            object_id="Q-0009",
            object_type="question",
            object_status="open",
            project_path="/tmp/project",
            written_path=".research/questions/Q-0009.yaml",
        )
    )
    assert ProposalStore.open(stored.proposal_id).decided_item_ids() == {
        "PR-001",
        "PR-002",
    }


def test_the_event_ledger_records_the_decline(stored: ProposalStore) -> None:
    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id, item_id="PR-001", reason="superseded"
        )
    )
    events = [
        event for event in stored.iter_events() if event["event"] == "item_declined"
    ]
    assert len(events) == 1
    assert events[0]["item_id"] == "PR-001"
    assert events[0]["reason"] == "superseded"


# -- who may write it ---------------------------------------------------------


def test_the_command_refuses_a_non_interactive_terminal(
    stored: ProposalStore,
) -> None:
    """The guard `promote` has, for an authority that is different but not lesser.

    A decline writes nothing into the capsule, so it cannot corrupt a
    scientific record. What it does is *close* a question the runtime would
    otherwise keep asking, and automation able to close its own unanswered
    proposals could report an empty queue it produced by dismissing everything
    in it.
    """

    with pytest.raises(PromotionRefusedError, match="interactive terminal"):
        propose_commands._decline(
            argparse.Namespace(
                proposal_id=stored.proposal_id, item="PR-001", reason="no"
            )
        )
    assert ProposalStore.open(stored.proposal_id).declines() == []


def test_the_command_writes_nothing_without_the_confirmation(
    stored: ProposalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os import cli

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda _question: False)

    code = propose_commands._decline(
        argparse.Namespace(
            proposal_id=stored.proposal_id, item="PR-001", reason="not worth it"
        )
    )
    assert code != 0
    assert ProposalStore.open(stored.proposal_id).declines() == []


def test_confirming_records_exactly_the_named_item(
    stored: ProposalStore, monkeypatch: pytest.MonkeyPatch, capsys: Any
) -> None:
    from research_os import cli

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda _question: True)

    code = propose_commands._decline(
        argparse.Namespace(
            proposal_id=stored.proposal_id, item="PR-001", reason="not worth it"
        )
    )
    assert code == 0
    assert [r.item_id for r in ProposalStore.open(stored.proposal_id).declines()] == [
        "PR-001"
    ]
    out = capsys.readouterr().out
    assert "PR-001" in out
    assert "writes nothing into your project" in out


def test_omitting_the_item_declines_everything_undecided(
    stored: ProposalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The common case is "I read all nine and want none of them"."""

    from research_os import cli

    stored.record_promotion(
        PromotionRecord(
            proposal_id=stored.proposal_id,
            item_id="PR-001",
            object_id="Q-0009",
            object_type="question",
            object_status="open",
            project_path="/tmp/project",
            written_path=".research/questions/Q-0009.yaml",
        )
    )
    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda _question: True)

    propose_commands._decline(
        argparse.Namespace(proposal_id=stored.proposal_id, item=None, reason="enough")
    )
    declined = [r.item_id for r in ProposalStore.open(stored.proposal_id).declines()]
    assert declined == ["PR-002"], "a promoted item must not be declined as well"


def test_an_unknown_item_is_refused(
    stored: ProposalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os import cli

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda _question: True)

    with pytest.raises(ProposalNotFoundError, match="PR-404"):
        propose_commands._decline(
            argparse.Namespace(
                proposal_id=stored.proposal_id, item="PR-404", reason="no"
            )
        )


def test_an_already_decided_item_is_not_decided_twice(
    stored: ProposalStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    from research_os import cli

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)
    monkeypatch.setattr(cli, "_confirm", lambda _question: True)

    propose_commands._decline(
        argparse.Namespace(proposal_id=stored.proposal_id, item="PR-001", reason="no")
    )
    code = propose_commands._decline(
        argparse.Namespace(
            proposal_id=stored.proposal_id, item="PR-001", reason="still no"
        )
    )
    assert code != 0
    assert len(ProposalStore.open(stored.proposal_id).declines()) == 1


def test_the_rendering_distinguishes_declined_from_untouched(
    stored: ProposalStore,
) -> None:
    from research_os.proposal.report import render_proposal

    stored.record_decline(
        DeclineRecord(
            proposal_id=stored.proposal_id, item_id="PR-001", reason="subsumed by Celer"
        )
    )
    text = render_proposal(
        stored.load(),
        promotions=stored.promotions(),
        declines=stored.declines(),
    )
    assert "DECLINED: subsumed by Celer" in text
    # And the item nobody touched carries no marker at all.
    second = [line for line in text.splitlines() if line.startswith("PR-002")]
    assert second and "DECLINED" not in second[0] and "PROMOTED" not in second[0]


# -- what the runtime does with it --------------------------------------------


def packet_and_digest() -> tuple[Any, str]:
    """One finding packet and the digest a proposal over it would record.

    Computed with the production functions rather than a literal, because the
    thing under test is that two different digest functions over different
    material are *not* being compared -- which is the defect the second pilot
    found, and a hard-coded digest would hide it again.
    """

    from research_os.proposal.planner import supplied_findings_digest
    from research_os.runtime.actions.proposals import _supplied
    from research_os.runtime.findings import FindingKind, FindingPacket, RuntimeFinding

    packet = FindingPacket(
        findings=(
            RuntimeFinding(
                finding_id="FIND-0001",
                project_id=PROJECT_ID,
                kind=FindingKind.EXPERIMENT,
                summary="The restricted master solved in 0.4s at p=10000.",
                artifact_ids=("sha256:abc",),
            ),
        )
    )
    digest = supplied_findings_digest(_supplied(packet.findings))
    assert digest is not None
    return packet, digest


def test_an_undecided_proposal_is_still_pending(
    automation_home: Path, tmp_path: Path
) -> None:
    """The control. Without it, the decline test below proves nothing."""

    from research_os.runtime.actions.proposals import _equivalent_pending_proposal

    packet, digest = packet_and_digest()
    ProposalStore.create(
        make_proposal(
            proposal_id="PROP-20260101T000000Z-bbbbbbbb",
            project_path=tmp_path / "project",
            finding_digest=digest,
        )
    )
    assert (
        _equivalent_pending_proposal(project_id=PROJECT_ID, packet=packet, context=None)
        == "PROP-20260101T000000Z-bbbbbbbb"
    )


def test_a_declined_proposal_stops_blocking_the_next_cycle(
    automation_home: Path, tmp_path: Path
) -> None:
    """The defect, stated as a test.

    Before declines existed the runtime had no way to tell a rejected proposal
    from an unopened one, so it went on answering every cycle over these
    findings with "an equivalent pending proposal exists" -- forever. The
    researcher's "no" became permanent silence on the subject they had said no
    to.
    """

    from research_os.runtime.actions.proposals import _equivalent_pending_proposal

    packet, digest = packet_and_digest()
    store = ProposalStore.create(
        make_proposal(
            proposal_id="PROP-20260101T000000Z-cccccccc",
            project_path=tmp_path / "project",
            finding_digest=digest,
        )
    )
    for item_id in ("PR-001", "PR-002"):
        store.record_decline(
            DeclineRecord(
                proposal_id=store.proposal_id,
                item_id=item_id,
                reason="modern solvers already answer this",
            )
        )

    assert (
        _equivalent_pending_proposal(project_id=PROJECT_ID, packet=packet, context=None)
        is None
    )


def test_a_partly_declined_proposal_is_still_pending(
    automation_home: Path, tmp_path: Path
) -> None:
    """A decline is not an answer to the items it did not touch.

    The same rule a promotion follows, and for the same reason: the second
    pilot showed a nine-item proposal with one item promoted being treated as
    fully answered, and eight questions silently dropped.
    """

    from research_os.runtime.actions.proposals import _equivalent_pending_proposal

    packet, digest = packet_and_digest()
    store = ProposalStore.create(
        make_proposal(
            proposal_id="PROP-20260101T000000Z-dddddddd",
            project_path=tmp_path / "project",
            finding_digest=digest,
        )
    )
    store.record_decline(
        DeclineRecord(
            proposal_id=store.proposal_id, item_id="PR-001", reason="not this one"
        )
    )
    assert (
        _equivalent_pending_proposal(project_id=PROJECT_ID, packet=packet, context=None)
        == "PROP-20260101T000000Z-dddddddd"
    )
