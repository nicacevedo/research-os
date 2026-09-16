"""Noticing a human scientific change, and continuing without being told.

The gap this closes is the last piece of routine human choreography in the
system. Before it:

```text
cycle -> findings -> proposal -> "a decision is waiting for you"
   ... researcher runs `researchctl propose promote` ...
-> nothing, because nothing told the runtime
-> researcher types a continue command
```

The properties, in descending order of how much damage their absence does:

1. a scientific change is noticed **and exactly one event is emitted**, however
   many times it is observed and however often the daemon restarts;
2. a successor cycle is opened **only when the unresolved work actually moved**,
   because a successor over an identical frontier is the seven-cycle pilot in
   ``docs/RUNTIME.md`` §16 again;
3. the parked cycle's LangGraph thread is **not revived** -- the successor is a
   new thread with recorded lineage;
4. a cycle waiting on an **approval** is not advanced by this, because a
   capsule change does not answer the question it asked.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.capsulewatch import (
    UNREADABLE_PREFIX,
    capsule_digest,
    observed_digests,
)
from research_os.runtime.clock import FrozenClock
from research_os.runtime.daemon import Daemon, WorkKind
from research_os.runtime.db import Database
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.models import RunStatus, TerminalState
from research_os.runtime.notify import CollectingNotifier
from research_os.runtime.policy import ActionKind
from research_os.runtime.store import RuntimeStore
from tests.fs_helpers import (
    hypothesis_data,
    make_git_repo,
    write_minimal_capsule,
    write_yaml,
)
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    plan_answer,
    review_answer,
)


@pytest.fixture
def plane(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    from research_os.runtime.checkpoints import ensure_tables

    ensure_tables(pg_dsn)
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "planner": plan_answer(str(ActionKind.ASSESS_FRONTIER)),
            "scientific_reviewer": review_answer(),
        }
    )
    clock = FrozenClock()
    daemon = Daemon(
        config=make_config(pg_dsn, tmp_path / "artifacts"),
        db=runtime_db,
        repo_for=lambda _project: repo,
        models=lambda _run, _project, _work: router,
        notifier=CollectingNotifier(),
        clock=clock,
        owner="test-worker",
    )
    return {
        "daemon": daemon,
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "store": store,
        "router": router,
        "clock": clock,
        "tmp_path": tmp_path,
    }


def _promote_a_hypothesis(repo: Path, *, object_id: str = "HYP-0002") -> Path:
    """Write a capsule object the way a person's promotion does.

    Through the capsule layout rather than through a runtime code path, because
    the point is that the runtime learns about a change *nothing told it about*.
    A fixture that went through the runtime would be testing a notification.
    """

    target = repo / ".research" / "hypotheses" / f"{object_id}.yaml"
    write_yaml(
        target,
        hypothesis_data(
            id=object_id,
            status="draft",
            addresses=["Q-0001"],
            statement="Promoted from a proposal by a person.",
        ),
    )
    return target


def _events(store: RuntimeStore, kind: str) -> list[Any]:
    return [event for event in store.list_events(limit=200) if event.kind == kind]


# ------------------------------------------------------------------ digests --
def test_the_digest_is_stable_and_moves_when_the_science_does(
    plane: dict[str, Any],
) -> None:
    kernel = ScientificKernelAdapter(plane["repo"])
    first = capsule_digest(kernel)
    assert first == capsule_digest(kernel), "the digest is not stable"

    _promote_a_hypothesis(plane["repo"])
    assert capsule_digest(kernel) != first


def test_a_status_change_moves_the_digest_though_the_kernel_digest_excludes_it(
    plane: dict[str, Any],
) -> None:
    """The one asymmetry worth a test of its own.

    ``semantic_projection`` deliberately leaves ``status`` out, so a Review does
    not go stale when an object is moved administratively. A Question moving
    from ``open`` to ``answered`` is exactly the human action this watcher must
    not miss, so the *capsule* digest includes it. Both are right and they
    answer different questions.
    """

    from research_os.digests import subject_digest

    kernel = ScientificKernelAdapter(plane["repo"])
    before = capsule_digest(kernel)
    question = next(obj for obj in kernel.objects() if str(obj.id).startswith("Q-"))
    semantic_before = subject_digest(question, project_id="alpha-project")

    path = plane["repo"] / ".research" / "questions" / "Q-0001.yaml"
    path.write_text(
        path.read_text(encoding="utf-8").replace("status: open", "status: answered"),
        encoding="utf-8",
    )

    kernel_after = ScientificKernelAdapter(plane["repo"])
    question_after = next(
        obj for obj in kernel_after.objects() if str(obj.id).startswith("Q-")
    )
    assert subject_digest(question_after, project_id="alpha-project") == (
        semantic_before
    ), "the kernel's semantic digest should ignore an administrative status move"
    assert capsule_digest(kernel_after) != before, (
        "the capsule digest must notice a status change; it is the single most "
        "important human action this watcher exists to see"
    )


def test_an_unreadable_capsule_is_a_digest_rather_than_an_exception(
    tmp_path: Path,
) -> None:
    """One broken project must not stop the control plane observing the others."""

    empty = tmp_path / "not-a-project"
    empty.mkdir()
    digest, frontier = observed_digests(empty)
    assert digest.startswith(UNREADABLE_PREFIX)
    assert frontier == ""


# --------------------------------------------------------------- one event --
def test_the_first_observation_is_not_a_change(plane: dict[str, Any]) -> None:
    """Otherwise every fresh database opens a successor cycle on every project."""

    report = plane["daemon"].tick()
    assert report.capsules_observed == 1
    assert report.capsule_changes == 0
    assert _events(plane["store"], "CAPSULE_CHANGED") == []
    observed = plane["store"].observed_capsule("alpha-project")
    assert observed is not None and observed[2] == 0


def test_a_change_emits_exactly_one_event_however_often_it_is_observed(
    plane: dict[str, Any],
) -> None:
    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()  # the baseline observation

    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)
    first = daemon.tick()
    assert first.capsule_changes == 1

    # Two more passes over the same, now-unchanged, state.
    clock.advance(60)
    daemon.tick()
    clock.advance(60)
    daemon.tick()

    assert len(_events(plane["store"], "CAPSULE_CHANGED")) == 1
    observed = plane["store"].observed_capsule("alpha-project")
    assert observed is not None and observed[2] == 1


def test_a_second_change_is_a_second_event(plane: dict[str, Any]) -> None:
    """The dedup key is per digest, not per project, so a later change is seen.

    Keyed per project, the second promotion would have been swallowed by the
    first one's key and the objective would have parked forever -- which is the
    same silent stall this whole mechanism exists to remove, arrived at by a
    different route.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()

    _promote_a_hypothesis(plane["repo"], object_id="HYP-0002")
    clock.advance(60)
    daemon.tick()
    _promote_a_hypothesis(plane["repo"], object_id="HYP-0003")
    clock.advance(60)
    daemon.tick()

    assert len(_events(plane["store"], "CAPSULE_CHANGED")) == 2


def test_observation_is_paced_rather_than_run_on_every_tick(
    plane: dict[str, Any],
) -> None:
    """Hashing a capsule is cheap and not free, and it happens forever."""

    daemon: Daemon = plane["daemon"]
    assert daemon.tick().capsules_observed == 1
    assert daemon.tick().capsules_observed == 0
    plane["clock"].advance(60)
    assert daemon.tick().capsules_observed == 1


def test_a_project_whose_repository_is_gone_is_noted_and_skipped(
    plane: dict[str, Any],
) -> None:
    from research_os.errors import ResearchOSError

    plane["store"].upsert_project(project_id="missing", repo_path="/nonexistent")

    def resolve(project_id: str) -> Path:
        if project_id == "missing":
            raise ResearchOSError("no such repository")
        return plane["repo"]

    daemon = Daemon(
        config=make_config(plane["dsn"], plane["tmp_path"] / "artifacts"),
        db=plane["db"],
        repo_for=resolve,
        models=lambda _run, _project, _work: plane["router"],
        clock=FrozenClock(),
        owner="test-worker",
    )
    report = daemon.tick()
    assert report.capsules_observed == 1, "the reachable project was still observed"
    assert any("cannot locate missing" in note for note in report.notes)


# ------------------------------------------------- the successor, or not ----
def _park_a_run(
    plane: dict[str, Any],
    *,
    terminal_state: TerminalState = TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
    frontier_digest: str | None = None,
) -> Any:
    """A finished cycle whose objective is waiting for a person."""

    store: RuntimeStore = plane["store"]
    run = store.create_run(
        project_id="alpha-project", objective="whether the widget deforms"
    )
    if frontier_digest is None:
        _capsule, frontier_digest = observed_digests(plane["repo"])
    store.set_frontier_digest(run.run_id, frontier_digest)
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(run.run_id, RunStatus.SUCCEEDED, terminal_state=terminal_state)
    return store.require_run(run.run_id)


def test_a_parked_objective_gets_a_successor_when_the_frontier_moved(
    plane: dict[str, Any],
) -> None:
    """The whole point: no continue command is typed anywhere in this test."""

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    parked = _park_a_run(plane)

    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)
    # One pass: observe, ingest the event into work, claim and run it.
    daemon.tick()
    report = daemon.tick()
    del report

    store: RuntimeStore = plane["store"]
    successors = [
        run
        for run in store.list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert len(successors) == 1, "the parked objective did not continue"
    successor = successors[0]
    assert successor.objective == parked.objective
    assert successor.cycle_index == parked.cycle_index + 1
    # A new thread, not the parked one revived.
    assert successor.thread_id != parked.thread_id
    # And the frontier it recorded is the new one.
    assert successor.frontier_digest != parked.frontier_digest


def test_a_capsule_change_that_leaves_the_frontier_identical_opens_no_cycle(
    plane: dict[str, Any],
) -> None:
    """Recorded as an event, and refused as a cycle, with the reason stored.

    A charter rewrite changes the canonical science and changes nothing about
    what is unresolved. A successor there costs model calls and returns the
    same answer.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    parked = _park_a_run(plane)

    charter = plane["repo"] / ".research" / "CHARTER.md"
    charter.write_text("# Charter\n\nRewritten by a person.\n", encoding="utf-8")

    clock.advance(60)
    daemon.tick()
    daemon.tick()

    assert len(_events(plane["store"], "CAPSULE_CHANGED")) == 1
    successors = [
        run
        for run in plane["store"].list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert successors == []

    with plane["db"].tx() as conn:
        row = conn.execute(
            "select result from work_items where kind = %s order by updated_at desc "
            "limit 1",
            (WorkKind.ADVANCE_OBJECTIVE,),
        ).fetchone()
    assert row is not None, "the advance work item was never run"
    result = row["result"]
    assert result["advanced"] is False
    assert "frontier did not" in result["skipped"][0]["reason"]


def test_two_observations_of_one_change_do_not_create_two_cycles(
    plane: dict[str, Any],
) -> None:
    """Belt and braces: the dedup key, and the no-successor-already check."""

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    parked = _park_a_run(plane)
    _promote_a_hypothesis(plane["repo"])

    clock.advance(60)
    for _ in range(4):
        daemon.tick()
        clock.advance(60)

    successors = [
        run
        for run in plane["store"].list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert len(successors) == 1, f"{len(successors)} successors for one change"


def test_a_cycle_waiting_on_an_approval_is_not_advanced_by_a_capsule_change(
    plane: dict[str, Any],
) -> None:
    """It is waiting for a different answer, and its thread has a live interrupt.

    Opening a successor would leave two threads for one objective. Those runs
    are woken by ``SCIENTIFIC_DECISION_RECORDED``, which is a different event
    with a different meaning.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    store: RuntimeStore = plane["store"]
    waiting = store.create_run(project_id="alpha-project", objective="gated work")
    store.set_run_status(waiting.run_id, RunStatus.RUNNING)
    store.set_run_status(
        waiting.run_id,
        RunStatus.WAITING_HUMAN,
        terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
    )

    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)
    daemon.tick()
    daemon.tick()

    successors = [
        run
        for run in store.list_runs(project_id="alpha-project")
        if run.parent_run_id == waiting.run_id
    ]
    assert successors == []
    assert store.require_run(waiting.run_id).status is RunStatus.WAITING_HUMAN


def test_a_budget_exhausted_objective_is_not_advanced(
    plane: dict[str, Any],
) -> None:
    """The science moving does not create budget.

    Advancing here would turn one exhausted objective into a stream of cycles
    that each stop immediately, which looks like a loop to anybody reading the
    run list.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    parked = _park_a_run(plane, terminal_state=TerminalState.BUDGET_EXHAUSTED)

    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)
    daemon.tick()
    daemon.tick()

    successors = [
        run
        for run in plane["store"].list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert successors == []


def test_a_restart_between_observing_and_ingesting_still_advances_once(
    plane: dict[str, Any],
) -> None:
    """The observation is durable; a fresh daemon ingests the event it left.

    The realistic crash: the capsule was hashed and the event recorded, and the
    process died before the event became work. A new daemon must pick that up --
    and must not re-emit, because its own first observation sees the *new*
    digest already stored and therefore no change.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    daemon.tick()
    parked = _park_a_run(plane)
    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)

    # One pass that observes and emits, then "dies" before running work.
    from research_os.runtime.daemon import Daemon as DaemonClass

    observing = DaemonClass(
        config=make_config(plane["dsn"], plane["tmp_path"] / "artifacts"),
        db=plane["db"],
        repo_for=lambda _project: plane["repo"],
        models=lambda _run, _project, _work: plane["router"],
        clock=FrozenClock(),
        owner="doomed-worker",
    )
    from research_os.runtime.daemon import TickReport

    observing._observe_capsules(TickReport())
    assert len(_events(plane["store"], "CAPSULE_CHANGED")) == 1

    # A fresh daemon. Its own observation sees no change, and it still ingests.
    successor_daemon = DaemonClass(
        config=make_config(plane["dsn"], plane["tmp_path"] / "artifacts"),
        db=plane["db"],
        repo_for=lambda _project: plane["repo"],
        models=lambda _run, _project, _work: plane["router"],
        clock=FrozenClock(),
        owner="fresh-worker",
    )
    report = successor_daemon.tick()
    assert report.capsule_changes == 0, "the change was re-emitted after a restart"
    successor_daemon.tick()

    assert len(_events(plane["store"], "CAPSULE_CHANGED")) == 1
    successors = [
        run
        for run in plane["store"].list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert len(successors) == 1


# --------------------------------------------------- the whole loop, once ----
def test_the_closed_loop_from_finding_to_successor_cycle(
    plane: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The loop this release exists to close, driven only by ``tick()``.

    ```text
    a runtime finding
      -> a cycle plans propose_capsule_change
      -> a grounded, noncanonical proposal
      -> WAITING_FOR_SCIENTIFIC_DECISION
      -> a person promotes it (written here the way the capsule layout expects)
      -> researchd observes the canonical change
      -> CAPSULE_CHANGED, exactly once
      -> a successor cycle, with lineage
      -> a different frontier
    ```

    No continue command, no resume command, and no notification from the
    scientific kernel anywhere in it. The only human act is the promotion,
    which is the one act that is *supposed* to be human.
    """

    from research_os.runtime.actions import proposals as proposal_action
    from research_os.runtime.findings import FindingKind, RuntimeFinding
    from tests.fake_providers import FakeProvider, ScriptedResponse
    from tests.proposal_helpers import assessment_payload, item, proposal_payload

    store: RuntimeStore = plane["store"]
    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]

    finding, _created = store.record_finding(
        RuntimeFinding(
            project_id="alpha-project",
            kind=FindingKind.FRONTIER,
            summary="HYP-0001 has no experiment and Q-0001 is unanswered.",
            source_action="assess_frontier",
            capsule_refs=("Q-0001", "HYP-0001"),
        )
    )
    provider = FakeProvider(
        name="claude",
        family="anthropic",
        responses={
            "planner": [
                ScriptedResponse(
                    structured=proposal_payload(
                        items=[item(grounded_in_findings=[finding.finding_id])]
                    )
                )
            ],
            "reviewer": [ScriptedResponse(structured=assessment_payload())],
        },
    )
    from tests.proposal_helpers import make_controller

    controller = make_controller({"claude": provider})
    monkeypatch.setattr(proposal_action, "_controller", lambda _ctx: controller)

    # The cycle's planner picks the proposal action.
    plane["router"].answers["planner"] = plan_answer(
        str(ActionKind.PROPOSE_CAPSULE_CHANGE)
    )

    # 1. A baseline observation, so a later change is a change.
    daemon.tick()

    # 2. One objective, started the way `researchctl runtime start` starts it.
    from research_os.runtime.cycles import open_cycle

    first = open_cycle(
        config=daemon._config,
        db=plane["db"],
        project_id="alpha-project",
        repo_path=plane["repo"],
        objective="whether the widget deforms sublinearly",
    )

    # 3. Ticks until the cycle has run. Nothing else is typed.
    for _ in range(6):
        daemon.tick()
        clock.advance(5)

    parked = store.require_run(first.run_id)
    assert parked.terminal_state is TerminalState.WAITING_FOR_SCIENTIFIC_DECISION, (
        f"the cycle concluded {parked.terminal_state}, so the objective is not "
        f"parked waiting for a person"
    )
    assert parked.status is RunStatus.SUCCEEDED

    from research_os.proposal.store import ProposalStore

    created = ProposalStore.list_proposal_ids()
    assert len(created) == 1, "the cycle produced no proposal"
    proposal = ProposalStore.open(created[0]).load()
    assert proposal.grounding.finding_ids == [finding.finding_id]
    assert [entry.finding_id for entry in store.proposal_findings(created[0])] == [
        finding.finding_id
    ]

    # No successor yet: a proposal is not canonical science, so the frontier
    # cannot have moved, and `should_continue` says so.
    assert [
        run
        for run in store.list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ] == []

    # 4. The person promotes it. Written through the capsule layout, because
    #    the property under test is that the runtime learns about a change
    #    nothing told it about.
    _promote_a_hypothesis(plane["repo"])

    # 5. Ticks. The change is observed, one event is emitted, and a successor
    #    cycle opens and runs.
    plane["router"].answers["planner"] = plan_answer(str(ActionKind.ASSESS_FRONTIER))
    clock.advance(60)
    for _ in range(6):
        daemon.tick()
        clock.advance(5)

    assert len(_events(store, "CAPSULE_CHANGED")) == 1
    successors = [
        run
        for run in store.list_runs(project_id="alpha-project")
        if run.parent_run_id == parked.run_id
    ]
    assert len(successors) == 1, "the promotion did not continue the objective"
    successor = successors[0]
    assert successor.cycle_index == parked.cycle_index + 1
    assert successor.thread_id != parked.thread_id
    assert successor.frontier_digest != parked.frontier_digest, (
        "the successor recomputed the same frontier, so the promotion changed "
        "nothing the runtime can see"
    )
    assert successor.terminal_state is not None, "the successor never concluded"

    # And the project's science is the person's: the runtime wrote one file
    # under `.research/`, and it was the person's promotion fixture.
    assert (plane["repo"] / ".research" / "hypotheses" / "HYP-0002.yaml").is_file()


# ------------------------------------------- the link that opened the loop ---
def test_an_action_that_observes_something_records_a_citable_finding(
    plane: dict[str, Any],
) -> None:
    """The link that was missing, and that kept the loop open.

    Every handler already returned a one-sentence ``detail`` and structured
    ``data``; nothing turned either into something with an identifier. So
    ``propose_capsule_change`` had an empty grounding allowlist, and the field
    the v1 validator has always checked stayed unused -- the loop was closed in
    code and open in practice.
    """

    from research_os.runtime.cycles import open_cycle
    from research_os.runtime.policy import ActionKind

    daemon: Daemon = plane["daemon"]
    store: RuntimeStore = plane["store"]
    plane["router"].answers["planner"] = plan_answer(str(ActionKind.INSPECT_REPOSITORY))
    run = open_cycle(
        config=daemon._config,
        db=plane["db"],
        project_id="alpha-project",
        repo_path=plane["repo"],
        objective="what does this repository contain",
    )
    for _ in range(4):
        daemon.tick()

    findings = store.list_findings(project_id="alpha-project")
    assert findings, "an inspection produced no finding, so nothing can cite it"
    finding = store.get_finding(findings[0].finding_id)
    assert finding is not None
    assert finding.source_run_id == run.run_id
    assert finding.source_action == str(ActionKind.INSPECT_REPOSITORY)
    assert finding.summary, "the finding carries no statement"
    # The handler's own words, not a paraphrase: a summariser between the
    # observation and the citation is one more place for a claim to drift from
    # its evidence.
    assert finding.summary == findings[0].summary

    # And it names what it rests on. Which edges exist depends on the action --
    # a repository inspection cites the capsule objects the plan addressed, a
    # frontier assessment cites its own artifact, a literature search cites
    # work keys -- so what is asserted is that *some* provenance edge exists,
    # not a particular one. A finding with none would be a citable identifier
    # for nothing.
    assert finding.artifact_ids or finding.capsule_refs or finding.literature_keys, (
        "the finding names nothing it rests on"
    )


def test_a_frontier_assessment_names_the_artifact_it_produced(
    plane: dict[str, Any],
) -> None:
    """The edge a person follows from a proposal to bytes by content hash."""

    from research_os.runtime.cycles import open_cycle
    from research_os.runtime.policy import ActionKind

    daemon: Daemon = plane["daemon"]
    store: RuntimeStore = plane["store"]
    plane["router"].answers["planner"] = plan_answer(str(ActionKind.ASSESS_FRONTIER))
    open_cycle(
        config=daemon._config,
        db=plane["db"],
        project_id="alpha-project",
        repo_path=plane["repo"],
        objective="what is outstanding",
    )
    for _ in range(4):
        daemon.tick()

    findings = [
        store.get_finding(item.finding_id)
        for item in store.list_findings(project_id="alpha-project")
    ]
    assessed = [
        item
        for item in findings
        if item is not None and item.source_action == str(ActionKind.ASSESS_FRONTIER)
    ]
    assert assessed, "the frontier assessment produced no finding"
    assert assessed[0].artifact_ids, "it names no artifact"
    # And the artifact is really there, by its content address.
    from research_os.runtime.artifacts import FilesystemArtifactStore

    artifacts = FilesystemArtifactStore(daemon._config.artifacts_root, store=store)
    assert artifacts.exists(assessed[0].artifact_ids[0])


def test_an_action_whose_output_is_an_ask_records_no_finding(
    plane: dict[str, Any],
) -> None:
    """A finding about having asked would be a citable observation with nothing
    behind it."""

    from research_os.runtime.graphs.cycle import FINDING_FOR_ACTION
    from research_os.runtime.policy import ActionKind

    for action in (
        ActionKind.PROPOSE_CAPSULE_CHANGE,
        ActionKind.NOMINATE_INSIGHT,
        ActionKind.DRAFT_MANUSCRIPT,
        ActionKind.RUN_LOCAL_EXPERIMENT,
        ActionKind.SUBMIT_CLUSTER_EXPERIMENT,
        ActionKind.DESIGN_EXPERIMENT,
    ):
        assert action not in FINDING_FOR_ACTION, (
            f"{action} produces an ask or a plan, not an observation"
        )


def test_the_planner_is_told_how_many_findings_exist(
    plane: dict[str, Any],
) -> None:
    """Because `propose_capsule_change` is the only action that can move the
    frontier, and it has nothing to propose from without them.

    A count rather than the findings themselves: the planner is choosing an
    action, and handing it the text would invite it to plan from a finding's
    content rather than from the project's state.
    """

    from research_os.runtime.cycles import open_cycle
    from research_os.runtime.policy import ActionKind

    daemon: Daemon = plane["daemon"]
    plane["router"].answers["planner"] = plan_answer(str(ActionKind.ASSESS_FRONTIER))
    open_cycle(
        config=daemon._config,
        db=plane["db"],
        project_id="alpha-project",
        repo_path=plane["repo"],
        objective="o",
    )
    for _ in range(4):
        daemon.tick()

    planner_prompts = [
        request.prompt for request in plane["router"].requests_for("planner")
    ]
    assert planner_prompts, "the planner was never asked"
    assert "FINDINGS AVAILABLE:" in planner_prompts[0].upper()
    assert "propose_capsule_change" in planner_prompts[0]


# ------------------------------------- what the first real pilot caught ------
def test_a_parked_cycle_with_an_identical_parent_still_advances(
    plane: dict[str, Any],
) -> None:
    """The defect the first closed-loop CCAO pilot found, as a test.

    The shape is the ordinary one and that is why it mattered: cycle 0 runs,
    cycle 1 is its successor, cycle 1 records the *same* frontier as cycle 0
    (correctly -- the runtime cannot change canonical science, so its own work
    never moves the frontier), cycle 1 produces a proposal and parks.

    A person then promotes it. `CAPSULE_CHANGED` fires, the advance runs, and
    `should_continue` compared cycle 1's digest against **cycle 0's**, found
    them equal, and refused -- at the exact moment the wait had ended. The
    comparison asked "did that old cycle learn anything", and the answer was no,
    which is precisely why it had stopped.

    The question it must ask is "has the frontier moved *since* cycle 1
    recorded one", so the measured frontier is passed in.
    """

    daemon: Daemon = plane["daemon"]
    clock: FrozenClock = plane["clock"]
    store: RuntimeStore = plane["store"]
    daemon.tick()

    _capsule, before = observed_digests(plane["repo"])
    first = store.create_run(
        project_id="alpha-project", objective="whether the widget deforms"
    )
    store.set_frontier_digest(first.run_id, before)
    store.set_run_status(first.run_id, RunStatus.RUNNING)
    store.set_run_status(
        first.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    second = store.create_run(
        project_id="alpha-project",
        objective="whether the widget deforms",
        parent_run_id=first.run_id,
        cycle_index=1,
    )
    # The same digest as its parent. Not a mistake -- the runtime's own work
    # cannot move the frontier, so this is what every real successor records.
    store.set_frontier_digest(second.run_id, before)
    store.set_run_status(second.run_id, RunStatus.RUNNING)
    store.set_run_status(
        second.run_id,
        RunStatus.SUCCEEDED,
        terminal_state=TerminalState.WAITING_FOR_SCIENTIFIC_DECISION,
    )

    _promote_a_hypothesis(plane["repo"])
    clock.advance(60)
    daemon.tick()
    daemon.tick()

    successors = [
        run
        for run in store.list_runs(project_id="alpha-project")
        if run.parent_run_id == second.run_id
    ]
    assert len(successors) == 1, (
        "the promotion did not continue the objective; `should_continue` "
        "compared the parked cycle with its parent instead of with now"
    )
    assert successors[0].cycle_index == second.cycle_index + 1


def test_an_uncomputable_frontier_is_not_treated_as_a_change(
    plane: dict[str, Any],
) -> None:
    """Unknown is not changed.

    `observed_digests` returns an empty frontier when the capsule cannot be
    read -- mid-edit, a checkout in progress. An earlier version of the
    eligibility check was `if frontier and ... == ...`, so an empty string fell
    through to "changed" and opened a successor cycle over a frontier nobody
    had measured.
    """

    from research_os.runtime.cycles import should_continue

    store: RuntimeStore = plane["store"]
    run = store.create_run(project_id="alpha-project", objective="o")
    store.set_frontier_digest(run.run_id, "a" * 64)
    store.set_run_status(run.run_id, RunStatus.RUNNING)
    store.set_run_status(
        run.run_id, RunStatus.SUCCEEDED, terminal_state=TerminalState.DONE_FOR_NOW
    )
    parked = store.require_run(run.run_id)

    from research_os.runtime.cycles import CycleResult

    result = CycleResult(
        run=parked,
        status=parked.status,
        terminal_state=parked.terminal_state,
        pending_approval_id=None,
        recommendation="START_NEXT_CYCLE",
        notes=(),
        state={},
    )
    proceed, why = should_continue(
        db=plane["db"],
        config=plane["daemon"]._config,
        result=result,
        observed_frontier="",
    )
    assert proceed is False
    assert "could not be computed" in why
    assert "Unknown is not changed" in why

    # And a measured, different frontier does proceed.
    proceed, why = should_continue(
        db=plane["db"],
        config=plane["daemon"]._config,
        result=result,
        observed_frontier="b" * 64,
    )
    assert proceed is True, why


def test_status_shows_a_parked_objective_and_the_observation_count(
    plane: dict[str, Any],
) -> None:
    """The state a researcher most needs to see and the one hardest to notice.

    A parked run is ``SUCCEEDED``, so it appears nowhere under RUNNING, WAITING
    FOR YOU or FAILED. Its terminal state is the only thing saying a person is
    the next step, and before this it was visible only by reading
    ``runtime runs``.

    The observation count answers the other question a stuck researcher asks:
    is the watcher watching? Zero changes on a project whose science has moved
    is the symptom of a daemon that is not running, and it looks identical to a
    project nobody has touched.
    """

    from research_os.runtime.report import collect_status, render_status

    daemon: Daemon = plane["daemon"]
    daemon.tick()
    parked = _park_a_run(plane)

    # Before any promotion: the parked run is the one shown, and the project
    # has been observed without anything having changed.
    report = collect_status(plane["db"])
    assert [run.run_id for _project, run in report.parked] == [parked.run_id]
    assert [project for project, _digest, _seen in report.observations] == [
        "alpha-project"
    ]
    assert report.observations[0][2] == 0, "a first observation is not a change"

    rendered = render_status(report)
    assert "WAITING FOR A SCIENTIFIC DECISION" in rendered
    assert parked.run_id in rendered
    assert "CAPSULE OBSERVATION" in rendered
    payload = report.payload()
    assert payload["parked"][0]["run_id"] == parked.run_id
    assert payload["observations"][0]["changes_seen"] == 0

    # After the promotion the count moves, and the parked row becomes the
    # successor -- which is the loop working, not a reporting bug: the original
    # run now has a child, so it is no longer the tip of its lineage.
    _promote_a_hypothesis(plane["repo"])
    plane["clock"].advance(60)
    daemon.tick()
    daemon.tick()

    after = collect_status(plane["db"])
    assert after.observations[0][2] == 1, "the change was not counted"
    parked_ids = {run.run_id for _project, run in after.parked}
    assert parked.run_id not in parked_ids, (
        "a run that has a successor is no longer waiting for anyone"
    )


# -- one validation, not two ------------------------------------------------
def test_both_digests_come_from_one_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pair has to describe one moment, and it did not.

    `capsule_digest(kernel)` called `kernel.validate()` and `kernel.frontier()`
    called it again, so the two digests came from two independent reads of the
    working tree. What lands in between is a researcher's `git commit` of a
    promotion -- the exact event this function exists to notice -- and the
    resulting row paired a capsule digest from before it with a frontier from
    after.
    """

    repo = make_git_repo(tmp_path / "counted")
    write_minimal_capsule(repo, project_id="counted-project")

    from research_os.runtime import kernel as kernel_module

    calls: list[str] = []
    original = kernel_module.validate_project

    def counting(path: object) -> object:
        calls.append(str(path))
        return original(path)  # type: ignore[arg-type]

    monkeypatch.setattr(kernel_module, "validate_project", counting)

    capsule, frontier = observed_digests(repo)

    assert capsule and frontier
    assert len(calls) == 1, f"the capsule was validated {len(calls)} times"


def test_an_unreadable_capsule_is_reported_once(tmp_path: Path) -> None:
    """And the early return does not lose the reason."""

    empty = make_git_repo(tmp_path / "hollow")
    capsule, frontier = observed_digests(empty)

    assert capsule.startswith(UNREADABLE_PREFIX)
    assert frontier == ""
