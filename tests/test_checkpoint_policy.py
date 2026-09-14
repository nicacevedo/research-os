"""What may stop an unattended run, and what may never be stopped from stopping it.

v1.0.0's residual risks record the problem: *the planner inserts human
checkpoints readily; two of four pilots stopped at one.* One of those was a
genuine scientific decision. The other was a planner being careful, and a
bounded unattended run that stops to ask whether it should carry on has not run
unattended.

Every test here is on one of two sides of that. Either a discretionary
checkpoint must not stop a scientific-only run, or a hard one must -- and no
flag, goal, or label may move a case from the second side to the first.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import Role
from research_os.errors import ResearchPlanError
from research_os.research.checkpoints import (
    HARD_CHECKPOINT_KINDS,
    CheckpointContext,
    CheckpointKind,
    CheckpointPolicy,
    eligibility_failure,
    is_hard,
)
from research_os.research.models import ResearchBudget, ResearchState, TaskStatus
from research_os.research.planner import (
    ResearchPlan,
    build_research_plan_prompt,
    validate_research_plan,
)
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.research_helpers import (
    checkpoint_task,
    experiment_config,
    init_repo,
    make_controller,
    plan_payload,
    task,
)

CAPSULE_CONTEXT = CheckpointContext(
    capsule_present=True, claim_count=1, prespecified_count=2
)
BARE_CONTEXT = CheckpointContext(capsule_present=False)


def validate(
    payload: dict[str, Any],
    *,
    policy: CheckpointPolicy = CheckpointPolicy.STANDARD,
    context: CheckpointContext = CAPSULE_CONTEXT,
) -> None:
    validate_research_plan(
        ResearchPlan.model_validate(payload),
        budget=ResearchBudget(),
        declared_experiments=frozenset({"fit-model"}),
        checkpoint_policy=policy,
        checkpoint_context=context,
    )


# -- the vocabulary ------------------------------------------------------


def test_every_kind_but_discretionary_is_hard() -> None:
    for kind in CheckpointKind:
        assert is_hard(kind) is (kind is not CheckpointKind.DISCRETIONARY)
    assert CheckpointKind.DISCRETIONARY not in HARD_CHECKPOINT_KINDS
    assert len(HARD_CHECKPOINT_KINDS) == len(list(CheckpointKind)) - 1


def test_standard_is_the_default() -> None:
    from research_os.research.models import ResearchRun

    run = ResearchRun(
        run_id="RR-20260914T101500Z-0a1b2c3d",
        project_path="/tmp/x",
        goal="anything",
    )
    assert run.checkpoint_policy is CheckpointPolicy.STANDARD


# -- discretionary checkpoints -------------------------------------------


def test_a_discretionary_checkpoint_is_fine_under_standard() -> None:
    validate(plan_payload(tasks=[checkpoint_task()]))


def test_a_discretionary_checkpoint_is_refused_under_scientific_only() -> None:
    with pytest.raises(ResearchPlanError, match="scientific_only mode permits only"):
        validate(
            plan_payload(tasks=[checkpoint_task()]),
            policy=CheckpointPolicy.SCIENTIFIC_ONLY,
        )


def test_the_refusal_says_what_to_do_instead() -> None:
    with pytest.raises(ResearchPlanError) as caught:
        validate(
            plan_payload(tasks=[checkpoint_task()]),
            policy=CheckpointPolicy.SCIENTIFIC_ONLY,
        )
    message = str(caught.value)
    assert "Revise the plan to continue without it" in message
    assert "within the same budgets" in message


# -- a model cannot invent authority -------------------------------------


def test_relabelling_a_preference_as_a_claim_acceptance_is_refused() -> None:
    """ "Would you like me to continue?" does not become scientific by labelling."""

    payload = plan_payload(
        tasks=[
            checkpoint_task(
                question="Would you like me to continue?",
                checkpoint_kind="claim_acceptance",
            )
        ]
    )
    with pytest.raises(ResearchPlanError, match="holds no Claim"):
        validate(
            payload,
            policy=CheckpointPolicy.SCIENTIFIC_ONLY,
            context=CheckpointContext(capsule_present=True, claim_count=0),
        )


def test_a_capsule_less_project_cannot_claim_a_scientific_checkpoint() -> None:
    for kind in (
        CheckpointKind.HUMAN_REVIEW,
        CheckpointKind.CLAIM_ACCEPTANCE,
        CheckpointKind.PRESPECIFIED_CHANGE,
        CheckpointKind.CROSS_PROJECT_PROMOTION,
    ):
        payload = plan_payload(tasks=[checkpoint_task(checkpoint_kind=kind.value)])
        with pytest.raises(ResearchPlanError, match="no Research Capsule"):
            validate(
                payload,
                policy=CheckpointPolicy.SCIENTIFIC_ONLY,
                context=BARE_CONTEXT,
            )


def test_a_costly_authorization_needs_something_costly_in_the_plan() -> None:
    payload = plan_payload(
        tasks=[checkpoint_task(checkpoint_kind="costly_authorization")]
    )
    with pytest.raises(ResearchPlanError, match="spends nothing that needs"):
        validate(payload, policy=CheckpointPolicy.SCIENTIFIC_ONLY)


def test_a_costly_authorization_is_eligible_beside_an_experiment() -> None:
    from tests.research_helpers import experiment_task

    payload = plan_payload(
        tasks=[
            checkpoint_task(checkpoint_kind="costly_authorization"),
            experiment_task(task_id="T-002", depends_on=["T-001"]),
        ]
    )
    validate(payload, policy=CheckpointPolicy.SCIENTIFIC_ONLY)


def test_eligibility_is_checked_under_standard_too() -> None:
    """A relabelled preference is refused whether or not the run is unattended.

    Otherwise the relabelling path stays open in the interactive mode and can be
    walked down by the unattended one later.
    """

    payload = plan_payload(tasks=[checkpoint_task(checkpoint_kind="claim_acceptance")])
    with pytest.raises(ResearchPlanError, match="no Research Capsule"):
        validate(payload, policy=CheckpointPolicy.STANDARD, context=BARE_CONTEXT)


def test_only_a_checkpoint_task_may_declare_a_kind() -> None:
    payload = plan_payload(
        tasks=[task(kind="literature", checkpoint_kind="claim_acceptance")]
    )
    with pytest.raises(ResearchPlanError, match="only a human_checkpoint task"):
        validate(payload)


@pytest.mark.parametrize(
    ("kind", "context", "experiments", "expected"),
    [
        (CheckpointKind.DISCRETIONARY, BARE_CONTEXT, 0, None),
        (CheckpointKind.HUMAN_REVIEW, CAPSULE_CONTEXT, 0, None),
        (CheckpointKind.CLAIM_ACCEPTANCE, CAPSULE_CONTEXT, 0, None),
        (CheckpointKind.PRESPECIFIED_CHANGE, CAPSULE_CONTEXT, 0, None),
        (CheckpointKind.CROSS_PROJECT_PROMOTION, CAPSULE_CONTEXT, 0, None),
        (CheckpointKind.COSTLY_AUTHORIZATION, CAPSULE_CONTEXT, 1, None),
        (
            CheckpointKind.PRESPECIFIED_CHANGE,
            CheckpointContext(capsule_present=True, prespecified_count=0),
            0,
            "no Experiment or Hypothesis",
        ),
    ],
)
def test_eligibility_table(
    kind: CheckpointKind,
    context: CheckpointContext,
    experiments: int,
    expected: str | None,
) -> None:
    failure = eligibility_failure(kind, context=context, experiment_tasks=experiments)
    if expected is None:
        assert failure is None
    else:
        assert failure is not None and expected in failure


# -- hard checkpoints are absolute ---------------------------------------


def test_scientific_only_does_not_suppress_a_hard_checkpoint() -> None:
    for kind in sorted(HARD_CHECKPOINT_KINDS):
        if kind is CheckpointKind.COSTLY_AUTHORIZATION:
            continue
        validate(
            plan_payload(tasks=[checkpoint_task(checkpoint_kind=kind.value)]),
            policy=CheckpointPolicy.SCIENTIFIC_ONLY,
        )


def test_a_hard_checkpoint_stops_a_scientific_only_run(tmp_path: Path) -> None:
    """The end of the argument: the policy narrows what may stop a run, and a
    hard checkpoint is still one of the things that may."""

    project = init_repo(tmp_path / "project", capsule=True)
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            checkpoint_task(
                                title="Ask before changing the prespecified rule",
                                goal=(
                                    "The next step would alter a threshold fixed "
                                    "before the result was known."
                                ),
                                question=(
                                    "E.4's threshold was prespecified at 0.30 and "
                                    "the result is 0.31. May I change the "
                                    "threshold to 0.35?"
                                ),
                                checkpoint_kind="prespecified_change",
                            )
                        ],
                        summary="Stop at the prespecified criterion.",
                    )
                )
            ]
        },
    )
    controller = make_controller(provider)
    store, run = controller.start(
        project_path=project,
        goal="Re-score the frozen diagnostics as a sensitivity disclosure.",
        budget=ResearchBudget(max_tasks=1, max_model_calls=4, max_write_tasks=0),
        checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
    )
    run = controller.execute(store)
    assert run.state is ResearchState.WAITING_FOR_HUMAN
    assert run.checkpoint_policy is CheckpointPolicy.SCIENTIFIC_ONLY
    pending = run.pending_checkpoints
    assert len(pending) == 1
    assert pending[0].kind is CheckpointKind.PRESPECIFIED_CHANGE
    assert pending[0].hard is True
    reached = [
        item
        for item in store.iter_events()
        if item["event"] == "human_checkpoint_reached"
    ]
    assert reached and reached[0]["hard"] is True


# -- the bounded replan --------------------------------------------------


def test_a_discretionary_checkpoint_is_replanned_once(tmp_path: Path) -> None:
    """The preferred behaviour: refuse, re-ask once with the reason, continue."""

    project = init_repo(tmp_path / "project", capsule=True)
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            checkpoint_task(
                                question="Shall I keep searching the literature?"
                            )
                        ],
                        summary="Ask before continuing.",
                    )
                ),
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            task(
                                title="Search the authorised literature",
                                goal=(
                                    "Continue the already-authorised search rather "
                                    "than asking whether to."
                                ),
                                query="widget deformation under load",
                            )
                        ],
                        summary="Search the literature that was already authorised.",
                    )
                ),
            ]
        },
    )
    controller = make_controller(provider)
    store, run = controller.start(
        project_path=project,
        goal="Find what is already known about widget deformation.",
        budget=ResearchBudget(max_tasks=2, max_model_calls=6, max_write_tasks=0),
        checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
    )
    events = [item["event"] for item in store.iter_events()]
    assert "plan_rejected" in events
    assert "plan_correction_started" in events
    assert "plan_correction_accepted" in events
    assert run.state is ResearchState.PLAN_READY
    assert [str(item.kind) for item in run.tasks] == ["literature"]

    rejected = [
        item for item in store.iter_events() if item["event"] == "plan_rejected"
    ]
    assert "scientific_only mode permits only hard checkpoints" in rejected[0]["detail"]

    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert run.pending_checkpoints == []


def test_a_second_discretionary_plan_fails_rather_than_looping(tmp_path: Path) -> None:
    """One correction. Asking until the planner complies is not a gate."""

    project = init_repo(tmp_path / "project", capsule=True)
    stubborn = ScriptedResponse(
        structured=plan_payload(
            tasks=[checkpoint_task(question="Are you sure you want me to continue?")],
            summary="Ask before continuing.",
        )
    )
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={str(Role.PLANNER): [stubborn, stubborn, stubborn]},
    )
    controller = make_controller(provider)
    with pytest.raises(ResearchPlanError, match="scientific_only"):
        controller.start(
            project_path=project,
            goal="Find what is already known.",
            budget=ResearchBudget(max_tasks=2, max_model_calls=6, max_write_tasks=0),
            checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
        )
    assert len(provider.calls) == 2


def test_nothing_after_a_refused_plan_is_silently_skipped(tmp_path: Path) -> None:
    """A refused plan is not a plan with one task removed.

    The whole plan is rewritten, so the tasks that followed the checkpoint are
    whatever the second plan says they are -- never the first plan's tasks run
    with a gap where the checkpoint was.
    """

    project = init_repo(tmp_path / "project", capsule=True)
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            checkpoint_task(question="Shall I go on?"),
                            task(task_id="T-002", depends_on=["T-001"]),
                        ],
                        summary="Ask, then search.",
                    )
                ),
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            task(
                                title="Search the authorised literature",
                                goal="Search without asking first.",
                            )
                        ],
                        summary="Search the literature.",
                    )
                ),
            ]
        },
    )
    _store, run = make_controller(provider).start(
        project_path=project,
        goal="Find what is known.",
        budget=ResearchBudget(max_tasks=3, max_model_calls=6, max_write_tasks=0),
        checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
    )
    assert len(run.tasks) == 1
    assert all(item.status is TaskStatus.PENDING for item in run.tasks)
    assert not any(item.status is TaskStatus.SKIPPED for item in run.tasks)


# -- what the planner is told --------------------------------------------


def test_the_scientific_only_prompt_says_nobody_is_waiting() -> None:
    prompt = build_research_plan_prompt(
        goal="Assess it.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=[],
        checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
    )
    assert "This run is unattended" in prompt
    assert "prespecified_change" in prompt
    assert "Any plan containing a discretionary checkpoint is refused." in prompt
    assert "Where you would have" in prompt


def test_the_standard_prompt_is_unchanged_in_substance() -> None:
    prompt = build_research_plan_prompt(
        goal="Assess it.",
        budget=ResearchBudget(),
        science_context="",
        repository_context="",
        declared_experiments=[],
        checkpoint_policy=CheckpointPolicy.STANDARD,
    )
    assert "This run is unattended" not in prompt
    assert "The controller stops there and waits." in prompt


# -- the boundaries no policy reaches ------------------------------------


def test_no_policy_lets_automation_record_a_review(tmp_path: Path) -> None:
    """The kernel rule, re-asserted from this layer.

    ``scientific_only`` narrows what may *stop* a run. It does not widen what a
    run may *do*, and the two are separate mechanisms: this asserts the second
    is untouched by the first.
    """

    from research_os.models import Review

    with pytest.raises(ValueError):
        Review.model_validate(
            {
                "id": "REV-0001",
                "type": "review",
                "schema_version": 1,
                "status": "concluded",
                "title": "Automated approval",
                "subject": "CLAIM-0001",
                "subject_digest": "0" * 64,
                "verdict": "approve",
                "reviewer_kind": "automation",
                "findings": "looks fine",
            }
        )


def test_no_policy_lets_a_task_write_into_the_capsule() -> None:
    from research_os.research.models import ResearchTask, TaskKind

    with pytest.raises(ValueError, match=".research"):
        ResearchTask(
            task_id="T-001",
            kind=TaskKind.CODE,
            title="Write the claim",
            goal="Record the conclusion.",
            allowed_paths=[".research/claims"],
            required_checks=["tests"],
        )


def test_scientific_only_does_not_authorise_experiments(tmp_path: Path) -> None:
    """Two different authorisations, and one does not imply the other."""

    from tests.research_helpers import experiment_task

    project = init_repo(tmp_path / "project", capsule=True)
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[experiment_task()], summary="Run the declared fit."
                    )
                )
            ]
        },
    )
    controller = make_controller(provider, experiments=experiment_config())
    store, run = controller.start(
        project_path=project,
        goal="Fit the model.",
        budget=ResearchBudget(max_tasks=1, max_model_calls=4, max_experiments=1),
        checkpoint_policy=CheckpointPolicy.SCIENTIFIC_ONLY,
    )
    assert run.execute_experiments is False
    run = controller.execute(store)
    assert run.experiments_used == 0
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert not (project / "results").exists()
