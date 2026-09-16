"""The capability handlers, and the scientific properties they promise.

These are the tests that matter for whether the system is trustworthy rather
than merely working. In rough order of how much damage the absence of each
would do:

- a refuted hypothesis is a success, not a failure to retry;
- the specification cannot change between preregistration and execution;
- the blind explorer is not told the project's preferred hypothesis;
- an unaccepted claim is not quotable, and the author is not offered it;
- a citation audit is a lookup, not a model's opinion.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.actions.authoring import (
    _audit,
    audit_citations,
    draft_manuscript,
)
from research_os.runtime.actions.base import ActionOutcome
from research_os.runtime.actions.coding import failure_class_for
from research_os.runtime.actions.experiments import (
    declared_commands,
    design_experiment,
    interpret_results,
    run_local_experiment,
)
from research_os.runtime.actions.explore import propose_hypotheses
from research_os.runtime.actions.inspect import inspect_repository, validate_capsule
from research_os.runtime.actions.review import assess_frontier_ranked, review_science
from research_os.runtime.db import Database
from research_os.runtime.executors import LocalExecutor, spec_digest
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import ExternalJobStatus
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import (
    declare_experiment_command,
    make_capsule,
    make_context,
)


@pytest.fixture
def action_env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    repo = make_capsule(tmp_path / "project")
    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id="alpha-project", repo_path=str(repo))
    run = store.create_run(project_id="alpha-project", objective="whether X holds")
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts_root": tmp_path / "artifacts",
        "store": store,
        "run": run,
        "state": {
            "run_id": run.run_id,
            "project_id": "alpha-project",
            "repo_path": str(repo),
            "objective": "whether X holds",
            "autonomy": "high",
            "cycle_index": 0,
            "artifacts": [],
            "notes": [],
            "frontier": {
                "open_questions": ["Q-0001"],
                "actionable_hypotheses": ["HYP-0001"],
                "hypotheses_without_tests": ["HYP-0001"],
                "claims_awaiting_review": ["CLAIM-0001"],
                "claims_with_stale_review": [],
                "contested_claims": [],
                "evidence_gaps": [],
                "pending_experiments": [],
                "empty": False,
            },
        },
    }


def _context(
    env: dict[str, Any], answers: dict[str, dict[str, Any]], **kwargs: Any
) -> Any:
    from tests.runtime_graph_helpers import make_router

    router = make_router(
        db=env["db"],
        artifacts_root=env["artifacts_root"],
        run_id=env["run"].run_id,
        project_id="alpha-project",
        answers=answers,
        **kwargs,
    )
    context = make_context(
        db=env["db"],
        repo=env["repo"],
        artifacts_root=env["artifacts_root"],
        dsn=env["dsn"],
        models=router,
        permitted=(),
    )
    context.executors = {"local": LocalExecutor()}
    return context


# --------------------------------------------------------------- inspection --
def test_inspecting_a_repository_reports_its_git_state(
    action_env: dict[str, Any],
) -> None:
    context = _context(action_env, {})
    outcome = inspect_repository(action_env["state"], context, {})
    assert outcome.ok
    assert len(outcome.data["head"]) == 40
    assert outcome.data["dirty"] is False


def test_a_capsule_with_errors_is_reported_not_failed(
    action_env: dict[str, Any],
) -> None:
    """Reporting accurately that the capsule is broken is this action succeeding."""

    bad = action_env["repo"] / ".research" / "claims" / "CLAIM-9999.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text("id: CLAIM-9999\ntype: claim\n", encoding="utf-8")
    context = _context(action_env, {})
    outcome = validate_capsule(action_env["state"], context, {})
    assert outcome.ok, "a broken capsule is a finding, not a handler failure"
    assert outcome.data["ok"] is False
    assert outcome.data["errors"]


# ------------------------------------------------------------ co-exploration --
def test_the_blind_branch_is_not_given_the_frontier(action_env: dict[str, Any]) -> None:
    """The scientific value of that branch is entirely what it does not know."""

    proposals = {
        "proposals": [
            {
                "statement": "X because Y",
                "mechanism": "Y causes X",
                "predictions": ["p"],
                "falsifiers": ["f"],
                "required_data": ["d"],
                "proposed_test": "t",
                "confounds": ["c"],
                "novelty": "incremental",
                "confidence": "medium",
            }
        ]
    }
    context = _context(
        action_env,
        {"planner": proposals, "analyst": proposals, "reviewer": proposals},
        families={"one": "family-one", "two": "family-two"},
    )
    outcome = propose_hypotheses(
        action_env["state"], context, {"addresses": ["Q-0001"]}
    )
    assert outcome.ok

    calls = RuntimeStore(action_env["db"]).list_model_calls(
        run_id=action_env["run"].run_id
    )
    blind = [call for call in calls if call.role == "blind_explorer"]
    assert blind, "the blind branch never ran"
    from research_os.runtime.artifacts import FilesystemArtifactStore

    store = FilesystemArtifactStore(action_env["artifacts_root"])
    prompt = store.get_text(blind[0].input_digest)
    assert "HYP-0001" not in prompt, "the blind branch was shown a hypothesis"
    assert "actionable_hypotheses" not in prompt
    assert "deliberately without" in prompt


def test_the_seeded_branch_is_given_the_frontier(action_env: dict[str, Any]) -> None:
    """The contrast that makes the blind branch meaningful."""

    proposals = {"proposals": []}
    context = _context(
        action_env, {"planner": proposals, "analyst": proposals, "reviewer": proposals}
    )
    propose_hypotheses(action_env["state"], context, {"addresses": ["Q-0001"]})
    calls = RuntimeStore(action_env["db"]).list_model_calls(
        run_id=action_env["run"].run_id
    )
    seeded = [call for call in calls if call.role == "seeded_explorer"]
    assert seeded
    from research_os.runtime.artifacts import FilesystemArtifactStore

    prompt = FilesystemArtifactStore(action_env["artifacts_root"]).get_text(
        seeded[0].input_digest
    )
    assert "HYP-0001" in prompt


def test_a_proposal_with_no_falsifier_is_flagged(action_env: dict[str, Any]) -> None:
    """A proposal with no falsifier is not a hypothesis."""

    payload = {
        "proposals": [
            {
                "statement": "X is simply true",
                "mechanism": "m",
                "predictions": [],
                "falsifiers": [],
                "required_data": [],
                "proposed_test": "",
                "confounds": [],
                "novelty": "unknown",
                "confidence": "low",
            }
        ]
    }
    context = _context(
        action_env, {"planner": payload, "analyst": payload, "reviewer": payload}
    )
    outcome = propose_hypotheses(action_env["state"], context, {})
    assert outcome.ok
    assert outcome.data["unfalsifiable"]


def test_one_family_makes_the_degradation_visible(action_env: dict[str, Any]) -> None:
    payload = {
        "proposals": [
            {
                "statement": "s",
                "mechanism": "m",
                "predictions": [],
                "falsifiers": ["f"],
                "required_data": [],
                "proposed_test": "t",
                "confounds": [],
                "novelty": "known",
                "confidence": "low",
            }
        ]
    }
    context = _context(
        action_env,
        {"planner": payload, "analyst": payload, "reviewer": payload},
        families={"only": "one-family"},
    )
    outcome = propose_hypotheses(action_env["state"], context, {})
    assert outcome.ok
    assert outcome.data["degraded"], "a single-family run must say so"


# ---------------------------------------------------------------- experiments --
_DESIGN = {
    "testable": True,
    "primary_endpoint": "the mean of column A",
    "secondary_endpoints": ["the variance"],
    "success_criteria": "mean > 0.5",
    "failure_criteria": "mean <= 0.5",
    # The *name* of a declared command, and values for its declared parameters.
    # Not an argv vector: a model cannot specify what this system runs.
    "command": "demo-test",
    "command_parameters": {},
    "resources": {},
    "seeds": [7],
    "dataset_identity": "demo@v1",
}


def test_an_undeclared_project_can_run_nothing(action_env: dict[str, Any]) -> None:
    """An experiment the runtime may run is one the researcher declared."""

    context = _context(action_env, {"planner": _DESIGN})
    outcome = design_experiment(
        action_env["state"], context, {"addresses": ["HYP-0001"]}
    )
    assert outcome.ok
    assert outcome.data["declared_commands"] == []
    assert "declared" in outcome.detail


def test_a_design_freezes_the_endpoint_before_any_result_exists(
    action_env: dict[str, Any],
) -> None:
    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    outcome = design_experiment(
        action_env["state"], context, {"addresses": ["HYP-0001"]}
    )
    assert outcome.ok, outcome.detail
    assert outcome.data["primary_endpoint"] == "the mean of column A"
    assert outcome.data["success_criteria"]
    assert outcome.data["failure_criteria"]
    assert outcome.data["command"] == "demo-test"
    assert len(outcome.data["spec_digest"]) == 64


def test_an_untestable_hypothesis_is_a_valid_answer(action_env: dict[str, Any]) -> None:
    """Better science than specifying something else and calling it a test."""

    declare_experiment_command()
    context = _context(
        action_env,
        {
            "planner": {
                **_DESIGN,
                "testable": False,
                "untestable_reason": "no such data",
            }
        },
    )
    outcome = design_experiment(
        action_env["state"], context, {"addresses": ["HYP-0001"]}
    )
    assert outcome.ok
    assert outcome.data["testable"] is False
    assert outcome.data["reason"] == "no such data"


def test_a_command_the_researcher_did_not_declare_is_refused(
    action_env: dict[str, Any],
) -> None:
    """The boundary that stops the runtime executing model-authored argv.

    An independent review found the earlier design: `argv` came straight from
    the model and ran with `cwd` set to the canonical checkout. It was
    unreachable only because the executors were never wired up -- a wiring
    omission, not a policy. Now the policy exists.
    """

    declare_experiment_command()
    context = _context(action_env, {"planner": {**_DESIGN, "command": "rm-minus-rf"}})
    outcome = design_experiment(
        action_env["state"], context, {"addresses": ["HYP-0001"]}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "does not declare" in outcome.detail or "not a command" in outcome.detail


def test_the_declared_commands_come_from_the_researchers_configuration(
    action_env: dict[str, Any],
) -> None:
    declare_experiment_command(name="benchmark")
    context = _context(action_env, {})
    assert sorted(declared_commands(context, "alpha-project")) == ["benchmark"]
    assert declared_commands(context, "some-other-project") == {}


def test_the_spec_digest_ignores_dictionary_ordering(
    action_env: dict[str, Any],
) -> None:
    from research_os.runtime.actions.experiments import _spec_from_record

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    record = design["spec"]
    shuffled = {key: record[key] for key in reversed(list(record))}
    assert spec_digest(_spec_from_record(record)) == spec_digest(
        _spec_from_record(shuffled)
    )


def test_the_spec_digest_changes_with_the_seed(action_env: dict[str, Any]) -> None:
    """An experiment whose seed can drift is an experiment nobody can rerun."""

    from research_os.runtime.actions.experiments import _spec_from_record

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    first = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    context = _context(action_env, {"planner": {**_DESIGN, "seeds": [8]}})
    second = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    assert first["spec_digest"] != second["spec_digest"]
    assert spec_digest(_spec_from_record(first["spec"])) == first["spec_digest"]
    assert spec_digest(_spec_from_record(second["spec"])) == second["spec_digest"]


def test_running_a_spec_that_changed_after_preregistration_is_refused(
    action_env: dict[str, Any],
) -> None:
    """The single most important gate: a post-hoc change is not a runtime decision."""

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    tampered = {
        **design,
        "spec": {**design["spec"], "argv": ["echo", "something-else"]},
    }
    outcome = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": tampered}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert action_env["store"].list_external_jobs(run_id=action_env["run"].run_id) == ()


def test_a_design_with_no_digest_is_refused(action_env: dict[str, Any]) -> None:
    """`if declared and declared != digest` skipped the guard by omission.

    The design arrives in plan parameters under an unconstrained schema, so the
    planner supplied both halves of the comparison -- and omitting one half
    skipped it entirely. Found by an independent review.
    """

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    del design["spec_digest"]
    outcome = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": design}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "no spec_digest" in outcome.detail


def test_a_fabricated_design_matching_its_own_digest_is_still_refused(
    action_env: dict[str, Any],
) -> None:
    """The digest must name a preregistration this runtime actually stored.

    Otherwise the check compares a self-consistent blob against itself, which
    establishes nothing about when the endpoint was fixed.
    """

    from research_os.runtime.actions.experiments import _spec_from_record

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    forged_spec = {**design["spec"], "argv": ["echo", "whatever-i-like"]}
    forged = {
        **design,
        "spec": forged_spec,
        # Self-consistent: the digest matches the forged spec.
        "spec_digest": spec_digest(_spec_from_record(forged_spec)),
    }
    outcome = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": forged}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.MISSING_SCIENTIFIC_AUTHORITY
    assert "no stored preregistration" in outcome.detail


def test_a_local_experiment_records_the_job_and_its_digest(
    action_env: dict[str, Any],
) -> None:
    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    outcome = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": design}}
    )
    assert outcome.ok, outcome.detail
    jobs = action_env["store"].list_external_jobs(run_id=action_env["run"].run_id)
    assert len(jobs) == 1
    assert jobs[0].spec_digest == design["spec_digest"]
    assert jobs[0].status is ExternalJobStatus.COMPLETED


def test_submitting_the_same_spec_twice_submits_once(
    action_env: dict[str, Any],
) -> None:
    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    plan = {"parameters": {"design": design}}
    first = run_local_experiment(action_env["state"], context, plan)
    assert first.ok, first.detail
    second = run_local_experiment(action_env["state"], context, plan)
    assert second.ok, second.detail
    jobs = action_env["store"].list_external_jobs(run_id=action_env["run"].run_id)
    assert len(jobs) == 1, "the same frozen spec was submitted twice"


def test_a_refutation_is_a_success(action_env: dict[str, Any]) -> None:
    """The property the whole failure taxonomy is shaped around.

    An experiment that ran correctly and answered "no" has succeeded. Nothing
    in the interpretation step can express it as a failure, and the criteria it
    is judged against are the ones the preregistration fixed beforehand.
    """

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    submitted = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": design}}
    )
    assert submitted.ok, submitted.detail

    outcome = interpret_results(
        action_env["state"],
        context,
        {"parameters": {"job_id": submitted.data["job_id"]}},
    )
    assert outcome.ok is True
    assert outcome.failure_class is None
    assert outcome.data["ran_correctly"] is True
    assert outcome.data["criteria_were_fixed_before_results"] is True
    # Read back from the stored preregistration, not from graph state.
    assert outcome.data["success_criteria"] == "mean > 0.5"
    assert outcome.data["failure_criteria"] == "mean <= 0.5"
    assert outcome.data["primary_endpoint"] == "the mean of column A"
    assert len(outcome.data["preregistration_artifact"]) == 64


def test_a_job_that_did_not_run_yields_no_scientific_conclusion(
    action_env: dict[str, Any],
) -> None:
    """A crashed experiment is not a negative result."""

    declare_experiment_command()
    context = _context(action_env, {"planner": _DESIGN})
    design = dict(
        design_experiment(
            action_env["state"], context, {"addresses": ["HYP-0001"]}
        ).data
    )
    submitted = run_local_experiment(
        action_env["state"], context, {"parameters": {"design": design}}
    )
    assert submitted.ok, submitted.detail
    job_id = submitted.data["job_id"]
    action_env["store"].update_external_job(
        job_id,
        status=ExternalJobStatus.FAILED,
        exit_code=1,
        allow_terminal_override=True,
    )

    outcome = interpret_results(
        action_env["state"], context, {"parameters": {"job_id": job_id}}
    )
    assert outcome.ok
    assert outcome.data["ran_correctly"] is False
    assert "no scientific conclusion follows" in outcome.detail
    # The criteria still come from the preregistration, unchanged.
    assert outcome.data["success_criteria"] == "mean > 0.5"


def test_a_job_with_no_preregistration_cannot_be_interpreted(
    action_env: dict[str, Any],
) -> None:
    """Without the criteria there is nothing to compare against.

    An earlier version read them from graph state -- which is empty on the
    cross-cycle path this handler is normally reached by -- and asserted
    `criteria_were_fixed_before_results: True` while reporting none of them.
    That is the one claim in this handler that must never be made loosely, so
    the absence of a preregistration is now a refusal.
    """

    context = _context(action_env, {"planner": _DESIGN})
    job = action_env["store"].create_external_job(
        project_id="alpha-project",
        run_id=action_env["run"].run_id,
        executor="local",
        spec_digest="d" * 64,
        run_dir=str(action_env["repo"]),
    )
    action_env["store"].update_external_job(
        job.job_id, status=ExternalJobStatus.COMPLETED, exit_code=0
    )
    outcome = interpret_results(
        action_env["state"], context, {"parameters": {"job_id": job.job_id}}
    )
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.ARTIFACT_MISSING
    assert "no preregistration is reachable" in outcome.detail
    assert outcome.data["interpreted"] is False


# ----------------------------------------------------------------- authoring --
def test_nothing_is_quotable_so_nothing_is_drafted(action_env: dict[str, Any]) -> None:
    """Writing from unaccepted claims is the failure the apparatus prevents."""

    context = _context(
        action_env, {"coder": {"markdown": "x", "cited_claims": [], "gaps": []}}
    )
    outcome = draft_manuscript(
        action_env["state"], context, {"parameters": {"section": "results"}}
    )
    assert outcome.ok
    assert outcome.data["drafted"] is False
    assert outcome.data["quotable_claims"] == []
    assert "nothing is quotable" in outcome.detail


def test_the_citation_audit_is_a_lookup_not_an_opinion() -> None:
    audit = _audit(
        "We showed CLAIM-0001 and also CLAIM-0002.",
        cited=("CLAIM-0001",),
        quotable=("CLAIM-0001",),
    )
    assert audit["unsupported"] == ["CLAIM-0002"]
    assert audit["undeclared"] == ["CLAIM-0002"]
    assert audit["in_prose"] == ["CLAIM-0001", "CLAIM-0002"]


def test_an_audit_reports_quotable_claims_the_draft_never_used() -> None:
    audit = _audit("Nothing cited here.", cited=(), quotable=("CLAIM-0001",))
    assert audit["unused"] == ["CLAIM-0001"]
    assert audit["unsupported"] == []


def test_auditing_with_no_draft_is_not_an_error(action_env: dict[str, Any]) -> None:
    context = _context(action_env, {})
    outcome = audit_citations(action_env["state"], context, {})
    assert outcome.ok
    assert outcome.data["audited"] is False


# -------------------------------------------------------------------- review --
def test_a_scientific_review_records_that_it_approves_nothing(
    action_env: dict[str, Any],
) -> None:
    verdict = {
        "verdict": "sound_with_changes",
        "methodological_validity": "adequate",
        "claim_support": "partially_supported",
        "alternative_explanations": ["confounding by Z"],
        "evidence_gaps": ["no control"],
        "overclaiming": ["the abstract says 'proves'"],
        "required_changes": ["add the control"],
    }
    context = _context(action_env, {"reviewer": verdict})
    state = {**action_env["state"], "action_result": {"data": {"ran": True}}}
    outcome = review_science(state, context, {"addresses": ["HYP-0001"]})
    assert outcome.ok
    assert outcome.data["approves_nothing"] is True
    assert outcome.data["alternative_explanations"] == ["confounding by Z"]
    assert outcome.data["overclaiming"]


def test_reviewing_nothing_costs_no_model_call(action_env: dict[str, Any]) -> None:
    context = _context(action_env, {"reviewer": {}})
    outcome = review_science(action_env["state"], context, {})
    assert outcome.ok
    assert outcome.data["reviewed"] is False
    assert (
        RuntimeStore(action_env["db"]).list_model_calls(run_id=action_env["run"].run_id)
        == ()
    )


# ------------------------------------------------------------------ frontier --
def test_an_empty_frontier_needs_no_ranking_call(
    action_env: dict[str, Any], tmp_path: Path
) -> None:
    quiet = make_capsule(tmp_path / "quiet", project_id="quiet-project", empty=True)
    action_env["store"].upsert_project(project_id="quiet-project", repo_path=str(quiet))
    from tests.runtime_graph_helpers import make_router

    router = make_router(
        db=action_env["db"],
        artifacts_root=action_env["artifacts_root"],
        run_id=action_env["run"].run_id,
        project_id="quiet-project",
        answers={"planner": {}},
    )
    context = make_context(
        db=action_env["db"],
        repo=quiet,
        artifacts_root=action_env["artifacts_root"],
        dsn=action_env["dsn"],
        models=router,
        permitted=(),
    )
    outcome = assess_frontier_ranked(
        {**action_env["state"], "repo_path": str(quiet)}, context, {}
    )
    assert outcome.ok
    assert outcome.data["recommendation"] == "DONE_FOR_NOW"
    assert (
        RuntimeStore(action_env["db"]).list_model_calls(run_id=action_env["run"].run_id)
        == ()
    )


def test_an_unknown_recommendation_falls_back_conservatively(
    action_env: dict[str, Any],
) -> None:
    """A recommendation this build does not understand must not be acted on."""

    context = _context(
        action_env,
        {"planner": {"ranked_actions": [], "recommendation": "TAKE_OVER_THE_WORLD"}},
    )
    outcome = assess_frontier_ranked(action_env["state"], context, {})
    assert outcome.ok
    assert outcome.data["recommendation"] == "START_NEXT_CYCLE"


def test_ranking_degrades_to_the_deterministic_frontier(
    action_env: dict[str, Any],
) -> None:
    """Ranking is an improvement on the frontier, not a prerequisite for it."""

    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.budgets import BudgetLedger
    from research_os.runtime.routing import ModelRouter, ProviderProfile
    from tests.fake_providers import FakeProvider, ScriptedResponse

    store = RuntimeStore(action_env["db"])
    broken = FakeProvider(
        name="broken",
        family="a",
        responses={
            "planner": [ScriptedResponse(structured=None, exit_code=1, error="down")]
        },
    )
    router = ModelRouter(
        adapters={"broken": broken},
        profiles=(ProviderProfile(name="broken", family="a", tier=3),),
        store=store,
        artifacts=FilesystemArtifactStore(action_env["artifacts_root"], store=store),
        budgets=BudgetLedger(action_env["db"]),
        run_id=action_env["run"].run_id,
        project_id="alpha-project",
    )
    context = make_context(
        db=action_env["db"],
        repo=action_env["repo"],
        artifacts_root=action_env["artifacts_root"],
        dsn=action_env["dsn"],
        models=router,
        permitted=(),
    )
    outcome = assess_frontier_ranked(action_env["state"], context, {})
    assert outcome.ok, "a missing ranker must not fail the cycle"
    assert outcome.data["recommendation"] == "START_NEXT_CYCLE"
    assert outcome.data["summary"]["open_questions"] >= 1


# -------------------------------------------------------------------- coding --
@pytest.mark.parametrize(
    ("reason", "expected"),
    [
        (
            "a required acceptance command (pytest -q) failed",
            FailureClass.DETERMINISTIC_CHECK_FAILED,
        ),
        ("model-call budget exhausted: 8 of 8 used", FailureClass.BUDGET_EXHAUSTED),
        ("scope violation: wrote outside allowed_paths", FailureClass.POLICY_REFUSED),
        ("symlink escaped the worktree", FailureClass.POLICY_REFUSED),
        ("provider claude is not available", FailureClass.PROVIDER_UNAVAILABLE),
        ("something else entirely", FailureClass.CODE_EXCEPTION),
        (None, FailureClass.CODE_EXCEPTION),
    ],
)
def test_a_coding_failure_is_classified_by_its_reason(
    reason: str | None, expected: FailureClass
) -> None:
    """A red test wants a repair, not a retry: running it again produces the same red."""

    assert failure_class_for(reason) is expected


def test_a_coding_task_with_no_goal_is_refused(action_env: dict[str, Any]) -> None:
    from research_os.runtime.actions.coding import run_coding_task

    context = _context(action_env, {})
    outcome = run_coding_task(action_env["state"], context, {})
    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED


# -------------------------------------------------------------------- shared --
def test_no_handler_can_report_a_negative_result_as_a_failure() -> None:
    """Structural: the outcome type refuses the combination."""

    with pytest.raises(ValueError, match="negative result is a success"):
        ActionOutcome(ok=True, failure_class=FailureClass.EXECUTOR_FAILED)
