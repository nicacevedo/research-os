"""Autonomy levels and permissions: complete, minimal, and honest about A2.

The property worth restating: `A2` is short by construction and every member of
it is something the *person* does. The runtime prepares the decision and hands
over the command; it has no method that writes canonical scientific state,
merges to a canonical branch, or publishes.
"""

from __future__ import annotations

from typing import Any

import pytest

from research_os.runtime.interfaces import ModelRole
from research_os.runtime.policy import (
    ACTIONS,
    AUTONOMY_GRANTS,
    ROLE_PERMISSIONS,
    ActionKind,
    AutonomyLevel,
    Permission,
    PolicyRefusedError,
    ScientificGateError,
    authorize,
    autonomous_actions,
    gated_actions,
    granted_permissions,
    human_executed_actions,
    level_for,
)


def test_every_action_has_a_policy() -> None:
    """An action with no entry would be a default-open."""

    assert set(ACTIONS) == set(ActionKind)


def test_every_role_has_a_permission_grant() -> None:
    assert set(ROLE_PERMISSIONS) == set(ModelRole)


def test_most_work_is_autonomous_and_the_gate_is_narrow() -> None:
    """An A2 list that grows until the researcher answers prompts all day has
    failed in the same way as an empty one."""

    gated = gated_actions()
    autonomous = autonomous_actions()
    assert len(gated) <= 10, f"{len(gated)} gated actions is too many to be meaningful"
    assert len(autonomous) > 2 * len(gated)


def test_every_gated_action_is_one_the_person_performs() -> None:
    """All eight of them write canonical state, merge, or publish."""

    assert set(gated_actions()) == set(human_executed_actions())


def test_reviewers_hold_no_permission_that_lets_them_act() -> None:
    """A reviewer reads a frozen packet and returns a verdict.

    No amount of prompt injection in the material under review can turn it into
    an actor, because it holds nothing to act with.
    """

    for role in (ModelRole.SCIENTIFIC_REVIEWER, ModelRole.REFEREE, ModelRole.SKEPTIC):
        held = ROLE_PERMISSIONS[role]
        assert Permission.WRITE_WORKTREE not in held
        assert Permission.WRITE_CAPSULE not in held
        assert Permission.RUN_LOCAL not in held
        assert Permission.SUBMIT_SLURM not in held
        assert Permission.PUBLISH not in held


def test_a_blind_explorer_holds_nothing_at_all() -> None:
    """Its blindness is the point; reading the repository would undo it."""

    assert ROLE_PERMISSIONS[ModelRole.BLIND_EXPLORER] == frozenset()


def test_no_autonomy_level_grants_capsule_writes_or_publication() -> None:
    """The two capabilities the runtime must never hold."""

    for level, grants in AUTONOMY_GRANTS.items():
        assert Permission.WRITE_CAPSULE not in grants, level
        assert Permission.PUBLISH not in grants, level
        assert Permission.DELETE not in grants, level


def test_autonomy_levels_are_nested() -> None:
    assert granted_permissions("low") <= granted_permissions("medium")
    assert granted_permissions("medium") <= granted_permissions("high")


def test_only_high_autonomy_may_submit_to_a_cluster() -> None:
    assert Permission.SUBMIT_SLURM in granted_permissions("high")
    assert Permission.SUBMIT_SLURM not in granted_permissions("medium")


def test_an_unknown_autonomy_setting_is_refused() -> None:
    with pytest.raises(PolicyRefusedError, match="unknown autonomy"):
        granted_permissions("unlimited")


def test_a_read_only_action_is_authorised_at_the_lowest_setting() -> None:
    policy = authorize(ActionKind.SEARCH_LITERATURE, autonomy="low")
    assert policy.level is AutonomyLevel.A0


def test_an_isolated_write_needs_at_least_medium() -> None:
    with pytest.raises(PolicyRefusedError, match="WRITE_WORKTREE"):
        authorize(ActionKind.EDIT_IN_WORKTREE, autonomy="low")
    assert authorize(ActionKind.EDIT_IN_WORKTREE, autonomy="medium")


def test_cluster_submission_needs_high() -> None:
    with pytest.raises(PolicyRefusedError, match="SUBMIT_SLURM"):
        authorize(ActionKind.SUBMIT_CLUSTER_EXPERIMENT, autonomy="medium")
    assert authorize(ActionKind.SUBMIT_CLUSTER_EXPERIMENT, autonomy="high")


def test_a_gated_action_is_refused_without_an_approval_at_every_setting() -> None:
    for setting in ("low", "medium", "high"):
        with pytest.raises(ScientificGateError, match="perform it yourself"):
            authorize(ActionKind.ACCEPT_CLAIM, autonomy=setting)


def test_a_gated_action_is_authorised_once_approved() -> None:
    """Authorised to *record the decision*, which is all the runtime then does."""

    policy = authorize(ActionKind.ACCEPT_CLAIM, autonomy="low", approval_granted=True)
    assert policy.human_executes is True
    assert "researchctl review" in policy.follow_up


def test_a_role_narrows_what_it_may_do_even_at_high_autonomy() -> None:
    """A literature extractor must not be able to write a worktree."""

    with pytest.raises(PolicyRefusedError, match="for role"):
        authorize(
            ActionKind.EDIT_IN_WORKTREE, autonomy="high", role=ModelRole.EXTRACTOR
        )


def test_the_author_may_write_a_worktree_because_that_is_its_job() -> None:
    assert authorize(
        ActionKind.DRAFT_MANUSCRIPT, autonomy="high", role=ModelRole.AUTHOR
    )


@pytest.mark.parametrize(
    "action",
    [
        ActionKind.ACCEPT_CLAIM,
        ActionKind.CHANGE_PRIMARY_ENDPOINT,
        ActionKind.CHANGE_PREREGISTRATION,
        ActionKind.PROMOTE_CONTESTED_CLAIM,
        ActionKind.CHANGE_PROJECT_OBJECTIVE,
        ActionKind.INTEGRATE_TO_CANONICAL_BRANCH,
        ActionKind.PUBLISH_EXTERNALLY,
        ActionKind.DELETE_SCIENTIFIC_STATE,
    ],
)
def test_the_scientific_authority_actions_are_all_a2(action: ActionKind) -> None:
    assert level_for(action) is AutonomyLevel.A2


@pytest.mark.parametrize(
    "action",
    [
        ActionKind.SEARCH_LITERATURE,
        ActionKind.PROPOSE_HYPOTHESES,
        ActionKind.CRITIQUE_HYPOTHESES,
        ActionKind.REVIEW_SCIENCE,
        ActionKind.DESIGN_EXPERIMENT,
        ActionKind.ASSESS_FRONTIER,
    ],
)
def test_the_thinking_actions_are_all_a0(action: ActionKind) -> None:
    """Proposing, critiquing, reviewing and designing change nothing."""

    assert level_for(action) is AutonomyLevel.A0


def test_every_policy_rationale_is_written_for_a_person() -> None:
    for action, policy in ACTIONS.items():
        assert policy.rationale.strip(), f"{action} has no rationale"
        assert policy.rationale.strip()[0].isupper(), f"{action}: {policy.rationale}"


def test_the_planner_is_told_what_the_previous_cycle_was_refused(
    runtime_db: Any, tmp_path: Any
) -> None:
    """A successor cycle used to repeat the action its parent was refused for.

    Observed twice on real work on 2026-09-17, on two different actions with
    two different guards. `run_local_experiment` refused with
    "design_experiment must come first" and the successor planned
    `run_local_experiment`; `design_experiment` refused an absolute path for a
    `path` parameter, naming the exact reason, and the successor supplied
    another absolute path.

    A successor is a new LangGraph thread seeded with identity alone, so the
    refusal -- the single most informative thing that had happened -- was
    invisible to the next planner. It is now a field in the planning prompt.
    """

    import json

    from research_os.runtime.graphs.cycle import _previous_attempt
    from research_os.runtime.store import RuntimeStore

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="feedback", repo_path=str(tmp_path))
    parent = store.create_run(project_id="feedback", objective="o")
    child = store.create_run(
        project_id="feedback", objective="o", parent_run_id=parent.run_id, cycle_index=1
    )

    class _Context:
        pass

    context = _Context()
    context.store = store

    # Nothing refused yet.
    assert _previous_attempt(context, {"run_id": child.run_id}) == "nothing"
    assert _previous_attempt(context, {"run_id": parent.run_id}) == "nothing"

    with runtime_db.tx() as conn:
        conn.execute(
            """
            insert into tool_invocations
                (invocation_id, idempotency_key, run_id, kind, status, result)
            values (%s, %s, %s, %s, 'COMPLETED', %s)
            """,
            (
                "INV-refused",
                "k:refused",
                parent.run_id,
                "cycle.run_local_experiment",
                json.dumps(
                    {
                        "ok": False,
                        "detail": (
                            "no preregistered design to run; design_experiment "
                            "must come first"
                        ),
                    }
                ),
            ),
        )

    told = _previous_attempt(context, {"run_id": child.run_id})
    assert "run_local_experiment was REFUSED" in told
    assert "design_experiment must come first" in told
    # And the parent is told nothing about itself.
    assert _previous_attempt(context, {"run_id": parent.run_id}) == "nothing"


def test_a_successful_previous_action_is_not_reported_as_a_refusal(
    runtime_db: Any, tmp_path: Any
) -> None:
    """Only refusals. A transcript of everything would crowd out the frontier."""

    import json

    from research_os.runtime.graphs.cycle import _previous_attempt
    from research_os.runtime.store import RuntimeStore

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="fine", repo_path=str(tmp_path))
    parent = store.create_run(project_id="fine", objective="o")
    child = store.create_run(
        project_id="fine", objective="o", parent_run_id=parent.run_id, cycle_index=1
    )
    with runtime_db.tx() as conn:
        conn.execute(
            """
            insert into tool_invocations
                (invocation_id, idempotency_key, run_id, kind, status, result)
            values (%s, %s, %s, %s, 'COMPLETED', %s)
            """,
            (
                "INV-fine",
                "k:fine",
                parent.run_id,
                "cycle.inspect_repository",
                json.dumps({"ok": True, "detail": "read the repository"}),
            ),
        )

    class _Context:
        pass

    context = _Context()
    context.store = store
    assert _previous_attempt(context, {"run_id": child.run_id}) == "nothing"
