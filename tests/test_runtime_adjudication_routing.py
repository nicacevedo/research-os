"""The runtime must not design an experiment to settle a theorem.

These are the §4.4 regression cases, run through the real cycle graph rather
than against the classifier in isolation, because the classifier being right is
not the property that matters -- the property that matters is that a plan the
classifier disagrees with is *refused*, deterministically, after every
authority check, with a refusal that names the action which would answer the
question instead.

What the live runtime did, and must not do again:

.. code-block:: text

    HYP-0002   a biconditional about when an infimum is finite
    planned    design_experiment, six times, six distinct spec digests
    cost       about four dollars, ninety minutes
    moved      nothing -- no measurement can decide a biconditional

``planner@5`` could not stop it: each design differed, so digest dedup saw six
distinct pieces of work, and it was six distinct pieces of the wrong work.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from research_os.runtime.checkpoints import ensure_tables
from research_os.runtime.cycles import permitted_actions, start_cycle
from research_os.runtime.db import Database
from research_os.runtime.findings import FindingKind, RuntimeFinding
from research_os.runtime.policy import ActionKind
from research_os.runtime.store import RuntimeStore
from tests.fs_helpers import hypothesis_data, question_data, write_yaml
from tests.runtime_graph_helpers import (
    ScriptedRouter,
    make_capsule,
    make_config,
    review_answer,
)

PROJECT = "routing-project"

MATHEMATICAL = {
    "id": "HYP-0002",
    "status": "active",
    "title": "Bounded pricing is exactly full LASSO dual feasibility",
    "statement": (
        "Under the model's scaling convention, the pricing subproblem at a "
        "master dual psi has a finite infimum if and only if "
        "||X' psi||_inf <= lambda_1, which is exactly the feasibility "
        "constraint of the dual of the full LASSO."
    ),
    "falsification": (
        "Exhibit a psi with ||X' psi||_inf > lambda_1 at which the pricing "
        "infimum is finite, or a finite infimum that is non-zero."
    ),
    "addresses": ["Q-0001"],
}
EMPIRICAL = {
    "id": "HYP-0007",
    "status": "active",
    "title": "The pricing scan is not the bottleneck",
    "statement": (
        "In the implemented method the per-iteration cost is dominated by the "
        "restricted master conic solve rather than by forming X' psi."
    ),
    "falsification": (
        "Profile the method and find X' psi accounting for a larger share of "
        "per-iteration wall time than the master solve, in any preregistered "
        "instance regime."
    ),
    "addresses": ["Q-0001"],
}
NOVELTY = {
    "id": "HYP-0008",
    "status": "active",
    "title": "The selection rule is new",
    "statement": "This working-set selection rule is a new contribution.",
    "falsification": (
        "Find a published pre-2015 source in the literature stating the same "
        "known rule."
    ),
    "addresses": ["Q-0001"],
}


def capsule_with(root: Path, *targets: dict[str, Any]) -> Path:
    """A real capsule holding exactly the hypotheses a case needs.

    The default helper capsule's HYP-0001 is deliberately removed: its
    falsifier is generic and would classify as EMPIRICAL, quietly supplying a
    measurable target to every plan and making the refusals under test
    unreachable.
    """

    repo = make_capsule(root, project_id=PROJECT)
    research = repo / ".research"
    (research / "hypotheses" / "HYP-0001.yaml").unlink()
    for target in targets:
        write_yaml(
            research / "hypotheses" / f"{target['id']}.yaml",
            hypothesis_data(**target),
        )
    write_yaml(research / "questions" / "Q-0001.yaml", question_data(status="open"))
    return repo


def plan(action: str, addresses: list[str]) -> dict[str, Any]:
    return {
        "action": action,
        "rationale": "the frontier lists it as unresolved",
        "addresses": addresses,
        "expected_information_gain": "high",
    }


@pytest.fixture
def env(runtime_db: Database, pg_dsn: str, tmp_path: Path) -> dict[str, Any]:
    ensure_tables(pg_dsn)
    artifacts = tmp_path / "artifacts"
    store = RuntimeStore(runtime_db)
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "tmp": tmp_path,
        "artifacts": artifacts,
        "config": make_config(pg_dsn, artifacts),
        "store": store,
    }


def run(
    env: dict[str, Any],
    repo: Path,
    answer: dict[str, Any],
    *,
    extra: dict[str, Any] | None = None,
) -> Any:
    env["store"].upsert_project(project_id=PROJECT, repo_path=str(repo))
    router = ScriptedRouter(
        answers={
            "planner": answer,
            "scientific_reviewer": review_answer(),
            **(extra or {}),
        }
    )
    result = start_cycle(
        config=env["config"],
        db=env["db"],
        project_id=PROJECT,
        repo_path=repo,
        objective="settle the open hypothesis",
        models=router,
    )
    return result, router


def refusal_of(result: Any) -> str:
    return str(result.state.get("plan_refusal") or "")


# --- 1. the headline case ----------------------------------------------------
def test_a_mathematical_hypothesis_does_not_get_an_experiment_designed(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, _router = run(
        env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0002"])
    )
    refusal = refusal_of(result)
    assert refusal, "design_experiment for a biconditional was allowed through"
    assert "cannot settle" in refusal
    assert "HYP-0002" in refusal


def test_the_refusal_names_the_action_that_would_answer_it(
    env: dict[str, Any],
) -> None:
    """A refusal a planner cannot act on produces the same plan next cycle.

    This is the lesson `_previous_attempt` was built from: the remedy has to be
    in the refusal text, because the refusal text is what the successor sees.
    """

    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, _router = run(
        env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0002"])
    )
    assert "derive_mathematics" in refusal_of(result)


def test_six_identical_attempts_are_refused_six_times(env: dict[str, Any]) -> None:
    """The literal shape of the live failure: six designs, six digests.

    Each of the six would have been a *different* specification, which is why
    digest deduplication could not stop it. What stops it is that none of them
    could settle the target.
    """

    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    for attempt in range(6):
        result, _router = run(
            env,
            repo,
            {
                **plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0002"]),
                # A different specification each time, as the live runs were.
                "rationale": f"variant {attempt} of the pricing test",
            },
        )
        assert refusal_of(result), f"attempt {attempt} was not refused"


# --- 2. there is somewhere to route to --------------------------------------
def test_a_derivation_is_available_and_permitted(env: dict[str, Any]) -> None:
    """Routing away from an action only helps if another one exists."""

    assert str(ActionKind.DERIVE_MATHEMATICS) in permitted_actions("medium")
    assert str(ActionKind.DERIVE_MATHEMATICS) in permitted_actions("high")


def test_a_mathematical_hypothesis_can_be_derived(env: dict[str, Any]) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, router = run(
        env,
        repo,
        plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"]),
        extra={
            "deriver": {
                "target": "HYP-0002",
                "convention": "lambda_1 scaling as in ASM-0001",
                "assumptions_used": ["ASM-0001"],
                "steps": [
                    {
                        "claim": "The pricing infimum is finite iff the dual is "
                        "feasible.",
                        "justification": "Conjugate of the indicator of the "
                        "l-infinity ball.",
                    }
                ],
                "result": "The biconditional holds under this convention.",
                "outcome": "DERIVED",
                "residual_gaps": [],
                "numerical_witness": {
                    "suggested": "Sweep psi and check finiteness numerically.",
                    "what_it_would_show": (
                        "That no counterexample is easy to find. It would not "
                        "establish the biconditional."
                    ),
                },
            }
        },
    )
    assert not refusal_of(result)
    assert router.requests_for("deriver"), "the deriver was never consulted"
    data = result.state["action_result"]["data"]
    assert data["outcome"] == "DERIVED"


def test_a_derivation_establishes_nothing_by_itself(env: dict[str, Any]) -> None:
    """DERIVED is a model's report, not the project's belief."""

    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    before = (repo / ".research" / "hypotheses" / "HYP-0002.yaml").read_bytes()
    result, _router = run(
        env,
        repo,
        plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"]),
        extra={"deriver": _derivation("DERIVED")},
    )
    assert result.state["action_result"]["data"]["establishes_nothing"] is True
    assert (repo / ".research" / "hypotheses" / "HYP-0002.yaml").read_bytes() == before


# --- 3. a completed derivation prevents repeating the proof ------------------
def test_a_recorded_derivation_stops_a_second_one(env: dict[str, Any]) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    env["store"].upsert_project(project_id=PROJECT, repo_path=str(repo))
    env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.OTHER,
            summary="derivation of HYP-0002: DERIVED in 4 step(s)",
            excerpt="target: HYP-0002\noutcome: DERIVED",
            source_action=str(ActionKind.DERIVE_MATHEMATICS),
            capsule_refs=("HYP-0002",),
        )
    )
    result, router = run(
        env, repo, plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"])
    )
    refusal = refusal_of(result)
    assert "already been run" in refusal
    assert "HYP-0002" in refusal
    assert not router.requests_for("deriver"), "a model was paid to redo the proof"


def test_repeat_suppression_does_not_depend_on_the_target_being_classifiable(
    env: dict[str, Any],
) -> None:
    """The hole an earlier version of the guard left open.

    Whether a derivation has already been done is a fact about this project's
    findings. It does not become unknown because the hypothesis happens to be
    worded without any of the signal vocabulary. Checking it behind the
    UNDETERMINED filter meant a vaguely-worded proposition could be derived
    over and over -- the exact loop this guard exists to close, reachable
    through the one door that was left open.
    """

    vague = {
        "id": "HYP-0009",
        "status": "active",
        "title": "Something is the case",
        "statement": "The thing is the case.",
        "falsification": "It is not.",
        "addresses": ["Q-0001"],
    }
    repo = capsule_with(env["tmp"] / "vague-derive", vague)
    env["store"].upsert_project(project_id=PROJECT, repo_path=str(repo))

    # The target really is unclassifiable -- otherwise this proves nothing.
    from research_os.runtime.adjudication import AdjudicationKind, classify

    assert (
        classify(
            statement=vague["statement"], falsification=vague["falsification"]
        ).kind
        is AdjudicationKind.UNDETERMINED
    )

    env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.OTHER,
            summary="derivation of HYP-0009: INCOMPLETE",
            source_action=str(ActionKind.DERIVE_MATHEMATICS),
            capsule_refs=("HYP-0009",),
        )
    )
    result, router = run(
        env, repo, plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0009"])
    )
    assert "already been run" in refusal_of(result)
    assert not router.requests_for("deriver")


def test_a_derivation_of_a_different_target_is_not_blocked(
    env: dict[str, Any],
) -> None:
    """Suppression is by proposition, not by action name."""

    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL, NOVELTY)
    env["store"].upsert_project(project_id=PROJECT, repo_path=str(repo))
    env["store"].record_finding(
        RuntimeFinding(
            project_id=PROJECT,
            kind=FindingKind.OTHER,
            summary="derivation of HYP-0008",
            source_action=str(ActionKind.DERIVE_MATHEMATICS),
            capsule_refs=("HYP-0008",),
        )
    )
    result, _router = run(
        env,
        repo,
        plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"]),
        extra={"deriver": _derivation("DERIVED")},
    )
    assert not refusal_of(result)


# --- 4. a numerical witness is not a proof -----------------------------------
def test_the_witness_is_carried_as_a_witness_and_not_as_the_result(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, _router = run(
        env,
        repo,
        plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"]),
        extra={"deriver": _derivation("NOT_DERIVABLE_AS_STATED")},
    )
    data = result.state["action_result"]["data"]
    # The outcome is what the deriver said, not upgraded by the witness.
    assert data["outcome"] == "NOT_DERIVABLE_AS_STATED"
    assert data["numerical_witness"]["suggested"]
    # And the excerpt a proposal would quote says what it would not establish.
    excerpt = data["finding_excerpt"]
    assert "NOT a proof" in excerpt
    assert "would not establish" in excerpt


def test_the_deriver_schema_has_no_degree_of_support() -> None:
    """A deriver that could report partial support would be reporting an
    experiment it did not run."""

    from research_os.runtime.prompts import DERIVER

    outcomes = DERIVER.output_schema["properties"]["outcome"]["enum"]
    assert "SUPPORTED" not in outcomes
    assert set(outcomes) == {
        "DERIVED",
        "REFUTED_BY_COUNTEREXAMPLE",
        "NOT_DERIVABLE_AS_STATED",
        "INCOMPLETE",
    }


def test_an_unknown_outcome_degrades_to_incomplete(env: dict[str, Any]) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, _router = run(
        env,
        repo,
        plan(str(ActionKind.DERIVE_MATHEMATICS), ["HYP-0002"]),
        extra={"deriver": _derivation("PROVED_BEYOND_DOUBT")},
    )
    assert result.state["action_result"]["data"]["outcome"] == "INCOMPLETE"


# --- 5. empirical work is untouched ------------------------------------------
def test_an_empirical_hypothesis_still_gets_an_experiment(
    env: dict[str, Any],
) -> None:
    """§4.4's fifth case. The fix must not make the system unable to measure."""

    repo = capsule_with(env["tmp"] / "emp", EMPIRICAL)
    result, _router = run(
        env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0007"])
    )
    assert "cannot settle" not in refusal_of(result)


def test_a_plan_addressing_both_kinds_is_allowed(env: dict[str, Any]) -> None:
    """A plan with a legitimate empirical half is not refused for its other
    half. Refusing it would make a mixed frontier unplannable."""

    repo = capsule_with(env["tmp"] / "both", MATHEMATICAL, EMPIRICAL)
    result, _router = run(
        env,
        repo,
        plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0002", "HYP-0007"]),
    )
    assert "cannot settle" not in refusal_of(result)


# --- 6. novelty routes to the literature -------------------------------------
def test_a_novelty_question_is_not_answered_by_an_experiment(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "nov", NOVELTY)
    result, _router = run(
        env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0008"])
    )
    refusal = refusal_of(result)
    assert "cannot settle" in refusal
    assert "search_literature" in refusal or "fetch_literature" in refusal


def test_a_literature_action_for_a_novelty_question_is_not_refused(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "nov", NOVELTY)
    result, _router = run(
        env, repo, plan(str(ActionKind.SEARCH_LITERATURE), ["HYP-0008"])
    )
    assert "cannot settle" not in refusal_of(result)


# --- 7. conservative: it blocks nothing it does not understand ---------------
def test_an_unclassifiable_target_does_not_block_an_experiment(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(
        env["tmp"] / "vague",
        {
            "id": "HYP-0009",
            "status": "active",
            "title": "Something is the case",
            "statement": "The thing is the case.",
            "falsification": "It is not.",
            "addresses": ["Q-0001"],
        },
    )
    result, _router = run(
        env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), ["HYP-0009"])
    )
    assert "cannot settle" not in refusal_of(result)


def test_a_plan_addressing_nothing_is_not_refused_by_this_guard(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    result, _router = run(env, repo, plan(str(ActionKind.DESIGN_EXPERIMENT), []))
    assert "cannot settle" not in refusal_of(result)


def test_actions_other_than_the_two_gated_ones_are_never_adjudication_refused(
    env: dict[str, Any],
) -> None:
    """Critiquing a theorem is useful. Proposing it to a person is useful."""

    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL)
    for action in (
        ActionKind.CRITIQUE_HYPOTHESES,
        ActionKind.INSPECT_REPOSITORY,
        ActionKind.ASSESS_FRONTIER,
    ):
        result, _router = run(
            env,
            repo,
            plan(str(action), ["HYP-0002"]),
            extra={"skeptic": {"proposals": []}},
        )
        assert "cannot settle" not in refusal_of(result), action


# --- 8. the planner is told, so it need not learn by refusal -----------------
def test_the_planner_is_shown_what_could_settle_each_target(
    env: dict[str, Any],
) -> None:
    repo = capsule_with(env["tmp"] / "math", MATHEMATICAL, EMPIRICAL)
    _result, router = run(
        env, repo, plan(str(ActionKind.INSPECT_REPOSITORY), ["HYP-0002"])
    )
    prompt = router.requests_for("planner")[0].prompt
    assert "ADJUDICATION" in prompt
    assert '"settled_by": "mathematical"' in prompt
    assert '"settled_by": "empirical"' in prompt
    assert '"experiment_can_decide_it": false' in prompt
    # And it is labelled as the planning metadata it is.
    assert '"noncanonical": true' in prompt


def test_the_planner_template_declares_the_block() -> None:
    from research_os.runtime.prompts import PLANNER

    assert "adjudication" in {name for name, _fence in PLANNER.blocks}
    assert PLANNER.identity == "planner@6"


def _derivation(outcome: str) -> dict[str, Any]:
    return {
        "target": "HYP-0002",
        "convention": "lambda_1 scaling",
        "assumptions_used": ["ASM-0001"],
        "steps": [{"claim": "step one", "justification": "by definition"}],
        "result": "what the derivation reached",
        "outcome": outcome,
        "residual_gaps": ["the unbounded case"],
        "numerical_witness": {
            "suggested": "sweep psi over a grid and look for a counterexample",
            "what_it_would_show": (
                "that no counterexample is easy to find; it would not establish "
                "the proposition"
            ),
        },
    }
