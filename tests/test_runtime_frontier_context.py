"""What the role that ranks the next action is allowed to see, and what it is not.

The defect, in the frontier's own words. A real cycle on the thesis pilot asked
the ``FRONTIER`` role whether another autonomous cycle was warranted. It ranked
five candidates, put an audit of whether the outstanding proposals already
covered the open questions at the top, and wrote this into the finding it
recorded:

    Absent HYP-0006's pre-specified test, the correct answer would have been
    WAIT_HUMAN. This judgement rests on the quoted frontier alone, as all
    file-inspection tools were disabled and the underlying artifacts could not
    be read.

So the highest-ranked action was one the role structurally could not perform,
and the recommendation it did give was qualified on material it could not open.
``frontier@1`` received the deterministic capsule frontier and the current
cycle's previous result. That is all there was.

**The fix is not file access.** It is that the material was structured
scientific state the runtime already held and simply did not pass. A proposal
reached the prompt as five operational fields -- an id, a run, a timestamp and
two counts -- so "is Q-0004 already in front of the researcher" was not
answerable from it. Proposals now arrive with their items, and each item with
the capsule objects it addresses, which makes coverage a set intersection.

**The boundary this file also holds.** The frontier gained no path, no
repository handle and no tool. Everything below asserts both halves: the
scientific context is there, and nothing else is.
"""

from __future__ import annotations

import inspect
import json
from pathlib import Path
from typing import Any

import pytest

from research_os.proposal.models import ProposalGrounding, ResearchProposal
from research_os.proposal.store import ProposalStore
from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.cycles import start_cycle
from research_os.runtime.db import Database
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.policy import ActionKind
from research_os.runtime.prompts import FRONTIER, PLANNER
from research_os.runtime.sciencecontext import (
    MAX_PROPOSAL_ITEMS,
    MAX_PROPOSAL_TITLE_CHARS,
    noncanonical_science,
    proposal_view,
)
from research_os.runtime.store import RuntimeStore
from tests.proposal_helpers import item as proposed_item
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)

PROJECT = "frontier-project"

FRONTIER_ANSWER: dict[str, Any] = {
    "recommendation": "WAIT_HUMAN",
    "recommendation_rationale": "every open question is already in a proposal.",
    "ranked_actions": [
        {
            "action": "propose_capsule_change",
            "addresses": ["Q-0001"],
            "importance": "low",
            "information_gain": "low",
            "feasibility": "high",
            "cost": "medium",
            "rationale": "a third ask would restate a question.",
        }
    ],
}


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project", project_id=PROJECT)
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT, repo_path=str(repo))
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "config": make_config(pg_dsn, tmp_path / "artifacts"),
        "artifacts_store": FilesystemArtifactStore(tmp_path / "artifacts", store=store),
        "tmp": tmp_path,
    }


def run_frontier(env: dict[str, Any], **answers: Any) -> tuple[Any, ScriptedRouter]:
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
            "frontier": FRONTIER_ANSWER,
            **answers,
        }
    )
    result = start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=env["repo"],
        objective="decide whether another cycle is warranted",
        models=router,
    )
    return result, router


def frontier_prompt(router: ScriptedRouter) -> str:
    requests = router.requests_for("frontier")
    assert requests, "the frontier role was never asked"
    return requests[-1].prompt


def put_a_real_proposal_on_disk(
    env: dict[str, Any],
    *,
    proposal_id: str,
    items: list[dict[str, Any]],
    finding_ids: tuple[str, ...] = (),
    project_path: Path | None = None,
) -> ResearchProposal:
    """A genuine proposal document plus the runtime's record of it.

    Both halves, because both are read: the reservation row is how the runtime
    knows a proposal is outstanding, and the document is where the items live.
    Writing a real document rather than stubbing the store is the point -- the
    defect being fixed was precisely that the items existed on disk and nothing
    read them.
    """

    built = [proposed_item(**entry) for entry in items]  # plain dicts
    proposal = ResearchProposal(
        proposal_id=proposal_id,
        project_id=PROJECT,
        project_path=str(project_path or env["repo"]),
        goal="carry the open questions to the researcher",
        summary="What is waiting for a decision.",
        # A proposal may only rest on what the run had in front of it, and the
        # model enforces that. So the allowlist is built from the items rather
        # than hard-coded: a helper that drifted from its items would be
        # testing a proposal a researcher could never receive.
        grounding=ProposalGrounding(
            capsule_ids=sorted(
                {target for entry in built for target in entry["addresses"]}
            )
        ),
        items=built,  # type: ignore[arg-type]
        provider="scripted",
    )
    ProposalStore.create(proposal)
    env["store"].reserve_proposal(
        reservation_key=f"propose_capsule_change:{proposal_id}",
        proposal_id=proposal_id,
        project_id=PROJECT,
    )
    env["store"].settle_proposal_reservation(
        f"propose_capsule_change:{proposal_id}", status="CREATED"
    )
    if finding_ids:
        env["store"].link_proposal_findings(
            proposal_id=proposal_id, finding_ids=finding_ids, cited_ids=finding_ids
        )
    return proposal


# --- the template ----------------------------------------------------------
def test_the_frontier_template_declares_the_context_it_needs() -> None:
    """A version bump, because a prompt is code and its identity is provenance."""

    assert FRONTIER.identity == "frontier@2"
    blocks = {name for name, _fence in FRONTIER.blocks}
    assert {"completed_findings", "outstanding_proposals", "preregistered_designs"} <= (
        blocks
    )
    assert "noncanonical_census" in FRONTIER.fields


def test_the_frontier_is_told_it_has_no_files_and_will_not_get_any() -> None:
    """The specific correction.

    The live assessment qualified its recommendation on artifacts it could not
    read, as though reading them were a thing that might happen on a later
    turn. It is not, and a role that believes otherwise defers instead of
    deciding.
    """

    instruction = FRONTIER.instruction
    assert "no filesystem" in instruction
    assert "no turn in which" in instruction
    assert "do not rank an action whose premise is that you" in instruction


def test_the_frontier_is_told_that_waiting_is_a_real_answer() -> None:
    """``frontier@1`` named only DONE_FOR_NOW, while the schema accepted
    WAIT_HUMAN -- so the one recommendation that ends the loop for the right
    reason was the one the instruction never mentioned."""

    assert "WAIT_HUMAN" in FRONTIER.instruction
    assert "Recommend WAIT_HUMAN when" in FRONTIER.instruction
    assert "WAIT_HUMAN" in json.dumps(FRONTIER.output_schema)


def test_the_frontier_and_the_planner_are_told_the_same_thing_about_authority(
    env: dict[str, Any],
) -> None:
    """Both must know the runtime cannot write canonical state.

    A role that thinks more autonomous work could resolve the frontier will
    always find something worth doing next.
    """

    for template in (FRONTIER, PLANNER):
        assert "cannot write" in template.instruction, template.identity
        assert "NOT canonical" in template.instruction, template.identity
        assert "only when a person promotes" in template.instruction, template.identity


# --- what it can now see ---------------------------------------------------
def test_the_frontier_sees_the_items_of_an_outstanding_proposal(
    env: dict[str, Any],
) -> None:
    """The acceptance test, in its smallest form.

    Two proposals, five items between them, addressing three of the project's
    identifiers. The prompt must carry each item, each item's targets, and
    whether a person has acted on it -- because those three are what coverage
    is computed from.
    """

    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-aaaa1111",
        items=[
            proposed_item_entry("PR-001", ("Q-0001",)),
            proposed_item_entry("PR-002", ("HYP-0001",)),
        ],
    )
    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000001Z-bbbb2222",
        items=[proposed_item_entry("PR-003", ("Q-0001", "EVI-0001"))],
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)

    assert "OUTSTANDING PROPOSALS" in prompt
    for identifier in ("PROP-20260917T000000Z-aaaa1111", "PR-001", "PR-002", "PR-003"):
        assert identifier in prompt, f"{identifier} never reached the frontier"
    # The field coverage is actually computed from.
    assert '"addresses"' in prompt
    assert '"decided_by_human"' in prompt
    assert "2 proposal(s) already awaiting a human decision" in prompt
    assert "3 proposed item(s)" in prompt


def test_a_question_covered_by_a_proposal_is_determinable_from_the_prompt(
    env: dict[str, Any],
) -> None:
    """Coverage as a set intersection, checked the way the role would do it.

    This is the property the live assessment asked for file access to get. The
    test computes it from the prompt's own blocks: the frontier block's open
    questions against the union of the items' addresses. One question is
    covered, one is not, and both facts are readable without opening anything.
    """

    _write_a_second_question(env["repo"])
    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-cccc3333",
        items=[proposed_item_entry("PR-001", ("Q-0001",))],
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)

    open_questions = set(_frontier_block(prompt)["open_questions"])
    assert {"Q-0001", "Q-0002"} <= open_questions
    addressed = {
        target
        for entry in _proposal_entries(prompt)
        for proposed in entry.get("items", ())
        for target in proposed["addresses"]
    }
    assert "Q-0001" in addressed, "a covered question must be visible as covered"
    assert "Q-0002" not in addressed, "an uncovered question must be visible as open"


def test_an_item_a_person_has_already_decided_is_marked(
    env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """Promoted or declined, either way it is no longer a live decision.

    "A person has read this and said no" and "nobody has looked" were the same
    state to anything reading the reservation row.

    The terminal check is monkeypatched because ``record_decline`` refuses a
    process with no interactive terminal -- a decline is a human scientific
    decision, and ``tests/test_runtime_authority.py`` asserts structurally that
    nothing under ``research_os.runtime`` can reach this writer. What is being
    tested here is the *reader*, so the decline has to exist somehow, and
    standing in for the terminal is the narrowest way to put one there.
    """

    from research_os import cli

    monkeypatch.setattr(cli, "_is_interactive", lambda: True)

    proposal = put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-dddd4444",
        items=[
            proposed_item_entry("PR-001", ("Q-0001",)),
            proposed_item_entry("PR-002", ("HYP-0001",)),
        ],
    )
    store = ProposalStore.open(proposal.proposal_id)
    from research_os.proposal.models import DeclineRecord

    store.record_decline(
        DeclineRecord(
            proposal_id=proposal.proposal_id,
            item_id="PR-002",
            reason="not worth the rig time",
        )
    )

    science = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    items = {entry["item_id"]: entry for entry in science.proposals[0]["items"]}
    assert items["PR-001"]["decided_by_human"] is False
    assert items["PR-002"]["decided_by_human"] is True


def test_a_proposal_whose_basis_has_moved_is_reported_stale(
    env: dict[str, Any],
) -> None:
    """ "Current" and "obsolete" are different answers and must look different.

    A proposal resting on objects that have since changed is not a live
    decision, so counting it as covering a question would make the frontier
    wait for a decision nobody can take.
    """

    from research_os.proposal.basis import scientific_basis

    proposal = ResearchProposal(
        proposal_id="PROP-20260917T000000Z-eeee5555",
        project_id=PROJECT,
        project_path=str(env["repo"]),
        goal="a proposal with a recorded basis",
        summary="It rests on HYP-0001.",
        grounding=ProposalGrounding(capsule_ids=["HYP-0001"]),
        items=[proposed_item(**proposed_item_entry("PR-001", ("HYP-0001",)))],
        provider="scripted",
        scientific_basis=scientific_basis(
            ("HYP-0001",), root=env["repo"], project_id=PROJECT
        ),
    )
    ProposalStore.create(proposal)
    env["store"].reserve_proposal(
        reservation_key="propose_capsule_change:stale",
        proposal_id=proposal.proposal_id,
        project_id=PROJECT,
    )
    env["store"].settle_proposal_reservation(
        "propose_capsule_change:stale", status="CREATED"
    )

    fresh = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    assert str(fresh.proposals[0]["basis"]).startswith("current:")

    # Now move the science the proposal rests on.
    _retire_the_hypothesis(env["repo"])
    moved = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    assert str(moved.proposals[0]["basis"]).startswith("STALE:")
    assert "HYP-0001" in str(moved.proposals[0]["basis"])


def test_a_caller_with_no_repository_reports_the_basis_unchecked(
    env: dict[str, Any],
) -> None:
    """Three answers, not two. "We did not check" must not read as "it is fine"."""

    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-ffff6666",
        items=[proposed_item_entry("PR-001", ("Q-0001",))],
    )
    science = noncanonical_science(env["store"], project_id=PROJECT)
    assert str(science.proposals[0]["basis"]).startswith("unchecked:")


def test_a_proposal_that_cannot_be_read_says_so_rather_than_looking_empty(
    env: dict[str, Any],
) -> None:
    """Unknown coverage is not zero coverage.

    A reader that cannot tell "no item addresses this" from "I could not read
    the items" will treat the second as the first, conclude a direction is
    uncovered, and spend a cycle on a question already in front of a person.
    """

    env["store"].reserve_proposal(
        reservation_key="propose_capsule_change:missing",
        proposal_id="PROP-20260917T000000Z-9999aaaa",
        project_id=PROJECT,
    )
    env["store"].settle_proposal_reservation(
        "propose_capsule_change:missing", status="CREATED"
    )

    science = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    entry = science.proposals[0]
    assert entry["items"] == []
    assert "items_unavailable" in entry
    assert "ProposalNotFoundError" in str(entry["items_unavailable"])
    assert "coverage cannot be established" in science.census()

    # And the cycle still completes: this is a decision aid, not a dependency.
    result, router = run_frontier(env)
    assert result.recommendation == "WAIT_HUMAN"
    assert "items_unavailable" in frontier_prompt(router)


def test_the_items_shown_are_bounded(env: dict[str, Any]) -> None:
    """A bound, and the total reported beside it, so nothing is silently hidden."""

    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-7777bbbb",
        items=[
            proposed_item_entry(f"PR-{index:03d}", ("Q-0001",))
            for index in range(MAX_PROPOSAL_ITEMS + 4)
        ],
    )
    science = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    entry = science.proposals[0]
    assert len(entry["items"]) == MAX_PROPOSAL_ITEMS
    assert entry["items_total"] == MAX_PROPOSAL_ITEMS + 4
    assert entry["items_shown"] == MAX_PROPOSAL_ITEMS
    # And the census says it is partial. A bound that is hit silently is the
    # defect one layer down that a security review found: the reader was told
    # every item was present, so the ones it could not see read as absent
    # rather than as unseen.
    assert f"{MAX_PROPOSAL_ITEMS} of {MAX_PROPOSAL_ITEMS + 4} proposed item(s)" in (
        science.census()
    )


def test_every_item_of_a_full_proposal_reaches_the_rendered_prompt(
    env: dict[str, Any],
) -> None:
    """The defect the bounds test above could not see, because it looked one
    layer too high.

    ``test_the_items_shown_are_bounded`` asserted on the *view object*. A
    security review looked at the layer below it and found that a proposal view
    is one block *entry*, that ``prompt_safe_block`` clips each entry at
    ``DEFAULT_FIELD_CHARS``, and that a twelve-item proposal at ``indent=2``
    serialised to 6 550 characters -- so **three** of twelve items reached the
    model, the JSON was cut mid-object, and the controller-authored census
    reported all twelve. Both live thesis proposals hold eleven and twelve
    items, so the set intersection this whole change exists to enable was being
    computed over a quarter of the data.

    This asserts at the layer that matters: every item id, and every capsule
    object any item addresses, present in the text the role actually receives.
    Worst-case shaped -- the item bound, the title bound, and eight targets an
    item -- so a future field that pushes the entry over
    ``PROPOSAL_BLOCK_CHARS`` fails here instead of quietly losing items.
    """

    targets = (
        "HYP-0002",
        "HYP-0003",
        "Q-0002",
        "Q-0003",
        "Q-0004",
        "ASM-0001",
        "EVI-0001",
        "Q-0001",
    )
    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-3333eeee",
        items=[
            {
                "item_id": f"PR-{index:03d}",
                "addresses": list(targets),
                "title": "D" * (MAX_PROPOSAL_TITLE_CHARS * 2),
            }
            for index in range(1, MAX_PROPOSAL_ITEMS + 1)
        ],
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)

    entries = _proposal_entries(prompt)
    assert len(entries) == 1, (
        "the proposal entry must still be decodable JSON, not clipped mid-object"
    )
    assert len(entries[0]["items"]) == MAX_PROPOSAL_ITEMS
    for index in range(1, MAX_PROPOSAL_ITEMS + 1):
        assert f"PR-{index:03d}" in prompt, f"PR-{index:03d} was clipped out"
    addressed = {target for item in entries[0]["items"] for target in item["addresses"]}
    assert set(targets) == addressed, "an item's targets were clipped out"
    # Truncation is visible where it does happen: the title is bounded, and the
    # marker says so rather than reading as a short title.
    assert all(
        len(item["title"]) <= MAX_PROPOSAL_TITLE_CHARS + 3
        for item in entries[0]["items"]
    )


def test_the_frontier_sees_completed_findings_and_preregistered_designs(
    env: dict[str, Any],
) -> None:
    """The other two categories. A role ranking what to do next must know what
    has been done, or it ranks it again."""

    env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.LITERATURE,
            summary="8 works on maximum-violation working-set methods",
            excerpt="the family is established; the 2019 survey covers it",
            source_action=str(ActionKind.SEARCH_LITERATURE),
        )
    )
    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)

    assert "COMPLETED FINDINGS" in prompt
    assert "working-set methods" in prompt
    assert "1 completed finding(s)" in prompt
    assert "PREREGISTERED DESIGNS" in prompt


# --- the boundary ----------------------------------------------------------
def test_the_frontier_is_given_no_repository_path(env: dict[str, Any]) -> None:
    """The planner gets a ``repository`` block; the frontier does not, and must not.

    Ranking what to do next needs to know what is outstanding and what has been
    asked. It does not need a path, and a path in a prompt is an invitation to
    reason about a filesystem the role cannot reach.
    """

    blocks = {name for name, _fence in FRONTIER.blocks}
    assert "repository" not in blocks
    assert "repository" in {name for name, _fence in PLANNER.blocks}

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)
    assert str(env["repo"]) not in prompt


def test_the_frontier_prompt_carries_no_path_from_the_proposal_document(
    env: dict[str, Any],
) -> None:
    """A proposal records the project path it was written against. That string
    is not passed on, and it is not dereferenced.

    A reader that opened a path it read out of a stored document would be
    letting the document choose what gets opened -- and a proposal is written
    by a model. The repository comes from the runtime's own context instead.
    """

    elsewhere = env["tmp"] / "somewhere-else"
    elsewhere.mkdir()
    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-8888cccc",
        items=[proposed_item_entry("PR-001", ("Q-0001",))],
        project_path=elsewhere,
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)
    assert "PR-001" in prompt, "the items must still be read"
    assert str(elsewhere) not in prompt
    assert "somewhere-else" not in prompt


def test_the_proposal_reader_dereferences_only_the_path_it_is_given(
    env: dict[str, Any],
) -> None:
    """The basis check runs against the caller's repository, never the document's.

    **The first version of this test did not test its own mechanism**, and an
    audit caught it: the fixture recorded no ``scientific_basis``, and
    ``basis_status`` returns ``checkable=False`` before it touches
    ``project_path`` at all. So neither path was dereferenced and a reader that
    passed the document's path straight through would have passed.

    This records a real basis, points the *document* at a second capsule where
    the cited object has not moved, and then retires that object in the
    **caller's** repository. A reader using the document's path would report
    ``current``; one using the caller's reports ``STALE``. The two answers are
    now distinguishable, which is what makes the assertion mean something.
    """

    from research_os.proposal.basis import scientific_basis

    # A second, independent capsule, whose HYP-0001 will not move.
    elsewhere = make_capsule(env["tmp"] / "elsewhere", project_id=PROJECT)

    proposal = ResearchProposal(
        proposal_id="PROP-20260917T000000Z-6666dddd",
        project_id=PROJECT,
        # The document points at the other capsule. This string is the one a
        # model wrote, and nothing may open it.
        project_path=str(elsewhere),
        goal="a proposal whose recorded path is not the caller's",
        summary="It rests on HYP-0001.",
        grounding=ProposalGrounding(capsule_ids=["HYP-0001"]),
        items=[proposed_item(**proposed_item_entry("PR-001", ("HYP-0001",)))],  # type: ignore[list-item]
        provider="scripted",
        scientific_basis=scientific_basis(
            ("HYP-0001",), root=env["repo"], project_id=PROJECT
        ),
    )
    ProposalStore.create(proposal)
    row = {"proposal_id": proposal.proposal_id, "run_id": "", "created_at": "now"}

    # Before anything moves, both repositories agree, so this says nothing yet.
    assert str(proposal_view(row, repo_path=str(env["repo"]))["basis"]).startswith(
        "current:"
    )

    # Now move it in the caller's repository only.
    _retire_the_hypothesis(env["repo"])
    view = proposal_view(row, repo_path=str(env["repo"]))
    assert str(view["basis"]).startswith("STALE:"), (
        "the basis must be checked against the repository the caller supplied, "
        "not the one the proposal document names"
    )
    # The control: handed the document's own path, the same reader would have
    # said `current`. So the assertion above is a real discrimination.
    assert str(proposal_view(row, repo_path=str(elsewhere))["basis"]).startswith(
        "current:"
    )
    assert [entry["item_id"] for entry in view["items"]] == ["PR-001"]
    assert str(elsewhere) not in json.dumps(view, default=str)


def test_an_unreadable_proposal_leaks_no_filesystem_path(
    env: dict[str, Any],
) -> None:
    """The boundary this file claims, on the path where it was false.

    A security review found it. ``ProposalStore.open`` raises ``no proposal
    <id> under <proposals_root()>``, the first version of ``_why_unreadable``
    formatted ``str(exc)`` into ``items_unavailable``, and the earlier version
    of this suite *asserted that the string arrived in the prompt* -- so a role
    told in the same prompt that it has no filesystem was handed an absolute
    path under the researcher's home, and a test held it in place.

    The reader needs to know the items could not be read. It does not need to
    know which directory.
    """

    from research_os.proposal.store import proposals_root

    env["store"].reserve_proposal(
        reservation_key="propose_capsule_change:leak",
        proposal_id="PROP-20260917T000000Z-1111dddd",
        project_id=PROJECT,
    )
    env["store"].settle_proposal_reservation(
        "propose_capsule_change:leak", status="CREATED"
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)

    # The fact survives.
    assert "items_unavailable" in prompt
    assert "ProposalNotFoundError" in prompt
    # The path does not. Asserted on the real roots rather than on a literal,
    # so the test cannot pass by the roots being redirected somewhere the
    # literal does not match.
    assert str(proposals_root()) not in prompt
    assert str(Path.home()) not in prompt
    assert str(env["repo"]) not in prompt


def test_a_failed_basis_check_leaks_no_repository_path(env: dict[str, Any]) -> None:
    """The second half of the same boundary.

    ``_basis_line``'s failure branch is reachable with the *caller's*
    repository inside the exception -- a capsule mid-checkout, a directory that
    has gone. That path must not enter a prompt either, and it is a separate
    branch from the one above.
    """

    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-2222eeee",
        items=[proposed_item_entry("PR-001", ("Q-0001",))],
    )
    row = {
        "proposal_id": "PROP-20260917T000000Z-2222eeee",
        "run_id": "",
        "created_at": "now",
    }
    # A path that is not a capsule at all, so the check fails rather than
    # returning a verdict.
    gone = env["tmp"] / "went-away"
    view = proposal_view(row, repo_path=str(gone), project_id=PROJECT)
    basis = str(view["basis"])
    assert basis.startswith("unchecked:"), basis
    assert str(gone) not in basis
    assert "went-away" not in basis


def test_a_promoted_item_is_also_marked_decided(
    env: dict[str, Any],
) -> None:
    """``decided_item_ids`` unions promotions and declines; both halves count.

    The decline half is covered above. A promoted item is equally not a live
    decision, and it is the half a researcher will actually produce.
    """

    from research_os.proposal.models import PromotionRecord

    proposal = put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-aaaa9999",
        items=[
            proposed_item_entry("PR-001", ("Q-0001",)),
            proposed_item_entry("PR-002", ("HYP-0001",)),
        ],
    )
    ProposalStore.open(proposal.proposal_id).record_promotion(
        PromotionRecord(
            proposal_id=proposal.proposal_id,
            item_id="PR-001",
            object_id="Q-0011",
            object_type="question",
            object_status="open",
            project_path=str(env["repo"]),
            written_path=".research/questions/Q-0011.yaml",
        )
    )

    science = noncanonical_science(
        env["store"], project_id=PROJECT, repo_path=str(env["repo"])
    )
    items = {entry["item_id"]: entry for entry in science.proposals[0]["items"]}
    assert items["PR-001"]["decided_by_human"] is True
    assert items["PR-002"]["decided_by_human"] is False


def test_a_preregistered_design_reaches_the_frontier_prompt(
    env: dict[str, Any],
) -> None:
    """Asserted on the design's own content, not on the block header.

    An audit found the earlier assertion vacuous: ``render`` skips an empty
    block, and ``"PREREGISTERED DESIGNS"`` also appears in the *instruction*
    text -- so the substring was satisfied whether or not any design was
    passed, and nothing would have failed if the block stopped being supplied.
    """

    from research_os.runtime.actions.experiments import preregistration_role
    from research_os.runtime.interfaces import ArtifactRef

    digest = "c0ffee" + "0" * 58
    role = preregistration_role(digest)
    ref = env["artifacts_store"].put_text(
        json.dumps({"hypothesis": "HYP-0001", "command": "adjudicate-pricing"}),
        role=role,
    )
    # Linked through a run, which is how the real path records it: `link`
    # derives the project from the run so the two cannot disagree.
    run = env["store"].create_run(project_id=PROJECT, objective="test HYP-0001")
    env["artifacts_store"].link(
        ArtifactRef(artifact_id=ref.artifact_id, media_type="application/json"),
        role=role,
        run_id=run.run_id,
    )

    science = noncanonical_science(
        env["store"],
        project_id=PROJECT,
        artifacts=env["artifacts_store"],
        repo_path=str(env["repo"]),
    )
    assert science.preregistrations_total == 1, (
        "the preregistration lookup found nothing, so this test would be vacuous"
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)
    assert digest[:16] in prompt, "the design's digest never reached the role"
    assert "HYP-0001" in prompt
    assert "1 experiment design(s) already preregistered" in prompt


def test_the_frontier_is_not_shown_its_own_previous_assessments(
    env: dict[str, Any],
) -> None:
    """A self-confirmation loop, closed.

    Raised by a security review. ``FindingKind.FRONTIER`` findings were not
    excluded, so cycle N+1's frontier read cycle N's own recommendation and
    rationale inside a block the instruction calls "work this runtime has
    already finished". With the identity fix keeping the first occurrence's
    wording, one early answer would be reinforced indefinitely -- on the one
    decision that governs whether money is spent and whether the system stops
    for a person.

    The planner still sees them: it is choosing a verb, not re-deciding whether
    to continue.
    """

    first, _router = run_frontier(env)
    assert first.recommendation == "WAIT_HUMAN"
    recorded = [
        item
        for item in env["store"].list_findings(project_id=PROJECT, limit=20)
        if item.source_action == str(ActionKind.ASSESS_FRONTIER)
    ]
    assert recorded, "the first assessment recorded no finding, so this is vacuous"

    _second, router = run_frontier(env)
    prompt = frontier_prompt(router)
    assert recorded[0].finding_id not in prompt, (
        "the frontier was shown its own previous assessment as completed work"
    )
    assert "0 completed finding(s)" in prompt

    # And the planner is not deprived of it.
    planner_requests = router.requests_for("planner")
    assert planner_requests
    assert recorded[0].finding_id in planner_requests[-1].prompt


def test_an_unrelated_project_is_not_visible(env: dict[str, Any]) -> None:
    """Scoping, asserted rather than assumed.

    Every read is by ``project_id``. A second project's findings and proposals
    exist in the same database and must not appear.
    """

    other = make_capsule(env["tmp"] / "other", project_id="other-project")
    env["store"].upsert_project(project_id="other-project", repo_path=str(other))
    env["store"].record_finding(
        RuntimeFinding(
            project_id="other-project",
            kind=FindingKind.LITERATURE,
            summary="a finding belonging to a different project entirely",
            source_action=str(ActionKind.SEARCH_LITERATURE),
        )
    )
    env["store"].reserve_proposal(
        reservation_key="propose_capsule_change:other",
        proposal_id="PROP-20260917T000000Z-0000eeee",
        project_id="other-project",
    )
    env["store"].settle_proposal_reservation(
        "propose_capsule_change:other", status="CREATED"
    )

    _result, router = run_frontier(env)
    prompt = frontier_prompt(router)
    assert "different project entirely" not in prompt
    assert "PROP-20260917T000000Z-0000eeee" not in prompt
    assert "0 completed finding(s)" in prompt


def test_the_frontier_context_writes_nothing(env: dict[str, Any]) -> None:
    """Reads only. The capsule is byte-identical after an assessment."""

    before = _capsule_bytes(env["repo"])
    put_a_real_proposal_on_disk(
        env,
        proposal_id="PROP-20260917T000000Z-5555ffff",
        items=[proposed_item_entry("PR-001", ("Q-0001",))],
    )
    run_frontier(env)
    assert _capsule_bytes(env["repo"]) == before


def test_assembling_the_context_cannot_make_a_model_call() -> None:
    """It is derived state, asserted structurally.

    The first version of this test built a ``ScriptedRouter``, never wired it
    to anything, called ``noncanonical_science`` -- which takes no provider --
    and asserted the router had no requests. A fresh router never does. An
    audit called it fully tautological, correctly: it was true of every
    possible implementation.

    So it is asserted the way ``tests/test_runtime_authority.py`` asserts its
    boundaries: by reading the module. A summariser between the proposals and
    the ranking would be one more place for a conclusion to drift from its
    basis, and the way to make that impossible is for the module to hold no
    path to a model at all.
    """

    import ast

    source = (
        Path(__file__).resolve().parents[1]
        / "src"
        / "research_os"
        / "runtime"
        / "sciencecontext.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)

    imported = {
        node.module or "" for node in ast.walk(tree) if isinstance(node, ast.ImportFrom)
    }
    assert not any("interfaces" in name or "routing" in name for name in imported), (
        f"sciencecontext must not reach a model provider; it imports {imported}"
    )
    called = {
        getattr(node.func, "attr", None) or getattr(node.func, "id", None)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    assert "complete" not in called
    assert "ModelRequest" not in called
    # And it takes no provider, so a caller could not hand it one by mistake.
    signature = inspect.signature(noncanonical_science)
    assert "models" not in signature.parameters
    assert "provider" not in signature.parameters


# --- helpers ---------------------------------------------------------------
def proposed_item_entry(item_id: str, addresses: tuple[str, ...]) -> dict[str, Any]:
    return {"item_id": item_id, "addresses": list(addresses)}


def _objects_in(prompt: str) -> list[dict[str, Any]]:
    """Every top-level JSON object the prompt carries, decoded.

    Read back out of the rendered prompt rather than from the context object,
    deliberately: what the role can compute is a property of the text it
    receives, and asserting against the object it was built from would not
    notice a serialiser that dropped a field.

    "Top-level" is a brace at column zero, which is what
    ``json.dumps(indent=2)`` produces for the outer object and for nothing
    inside it. Decoding from there with ``raw_decode`` finds the end of the
    object rather than counting braces, so a brace inside a string cannot
    mislead it.
    """

    decoder = json.JSONDecoder()
    found: list[dict[str, Any]] = []
    for offset, line in _line_offsets(prompt):
        if not line.startswith("{"):
            continue
        try:
            value, _end = decoder.raw_decode(prompt, offset)
        except ValueError:
            continue
        if isinstance(value, dict):
            found.append(value)
    return found


def _line_offsets(text: str) -> list[tuple[int, str]]:
    offsets: list[tuple[int, str]] = []
    position = 0
    for line in text.splitlines(keepends=True):
        offsets.append((position, line))
        position += len(line)
    return offsets


def _frontier_block(prompt: str) -> dict[str, Any]:
    """The deterministic frontier, as the role receives it."""

    for value in _objects_in(prompt):
        if "open_questions" in value and "actionable_hypotheses" in value:
            return value
    raise AssertionError("no frontier block in the prompt")


def _proposal_entries(prompt: str) -> list[dict[str, Any]]:
    """Every outstanding-proposal object in the prompt."""

    return [value for value in _objects_in(prompt) if "proposal_id" in value]


def _write_a_second_question(repo: Path) -> None:
    from tests.automation_helpers import commit_all
    from tests.fs_helpers import question_data, write_yaml

    write_yaml(
        repo / ".research" / "questions" / "Q-0002.yaml",
        {**question_data(), "id": "Q-0002"},
    )
    commit_all(repo, "a second question")


def _retire_the_hypothesis(repo: Path) -> None:
    import yaml

    from tests.automation_helpers import commit_all

    path = repo / ".research" / "hypotheses" / "HYP-0001.yaml"
    document = yaml.safe_load(path.read_text(encoding="utf-8"))
    document["status"] = "retired"
    path.write_text(yaml.safe_dump(document, sort_keys=True), encoding="utf-8")
    commit_all(repo, "retire the hypothesis")


def _capsule_bytes(repo: Path) -> dict[Path, bytes]:
    return {
        path: path.read_bytes()
        for path in sorted((repo / ".research").rglob("*"))
        if path.is_file()
    }
