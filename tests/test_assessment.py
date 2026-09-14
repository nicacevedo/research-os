"""The capsule-less half of the system, and the boundary it must not cross.

The failure this whole layer exists for is concrete and is in the v1.0.0 build
record: a read-only run against a real repository with no Research Capsule
produced a scientific proposal citing ``PR-002`` and ``PR-003``, objects that did
not exist. The grounding validator refused it, which was right. What was wrong
was that the worker had been given a universe with nothing in it.

So these tests check two things in opposite directions. That a capsule-less
repository can now reach a grounded, useful, archived assessment. And that it
still cannot reach one by inventing a scientific identifier.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.assessment.controller import AssessmentController
from research_os.assessment.models import (
    AssessmentGrounding,
    AssessmentMode,
    FileRef,
    Observation,
    TechnicalAssessment,
)
from research_os.assessment.planner import (
    build_assessment_prompt,
    grounding_violations,
    validate_assessment,
)
from research_os.assessment.report import render_assessment
from research_os.assessment.store import AssessmentStore
from research_os.automation.models import Role
from research_os.automation.profile import build_project_profile
from research_os.errors import (
    AssessmentGroundingError,
    AssessmentValidationError,
)
from research_os.research.models import ResearchBudget, ResearchRun, ResearchState
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.proposal_helpers import fake_config
from tests.research_helpers import make_controller, plan_payload, proposal_task

# -- fixtures ------------------------------------------------------------


CUPDLP_SOLVER = '''"""A first-order LP solver, roughly in the shape of a real one."""


def solve(problem, *, max_iterations=10000, restart="adaptive"):
    """Run PDHG until the primal-dual gap is small enough."""
    return {"status": "optimal", "iterations": 1}


def restart_criterion(gap_history):
    """Decide whether to restart the averaging sequence."""
    return len(gap_history) > 10
'''


def _git(args: list[str], cwd: Path) -> None:
    subprocess.run(["git", *args], cwd=str(cwd), check=True, capture_output=True)


def capsule_less_repo(path: Path) -> Path:
    """A real, committed repository with real code and no ``.research/``.

    The cuPDLP shape: a solver, a benchmark harness, a README, no scientific
    objects of any kind.
    """

    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-q", "--initial-branch=main"], path)
    _git(["config", "user.email", "tests@example.invalid"], path)
    _git(["config", "user.name", "Research OS tests"], path)
    (path / "README.md").write_text(
        "# solver\n\nA first-order LP solver.\n", encoding="utf-8"
    )
    (path / "solver.py").write_text(CUPDLP_SOLVER, encoding="utf-8")
    (path / "benchmark.py").write_text(
        "from solver import solve\n\n\ndef run(problems):\n"
        "    return [solve(item) for item in problems]\n",
        encoding="utf-8",
    )
    _git(["add", "-A"], path)
    _git(["commit", "-qm", "initial"], path)
    return path


def assessment_payload(**overrides: Any) -> dict[str, Any]:
    """A schema-conforming technical assessment about the fixture repository."""

    payload: dict[str, Any] = {
        "summary": (
            "A small first-order LP solver with an adaptive restart rule and a "
            "benchmark harness, with no test suite and no recorded results."
        ),
        "capsule_suggestion": (
            "If the restart rule is going to be compared against alternatives, a "
            "capsule would give those comparisons somewhere to live."
        ),
        "observations": [
            {
                "observation_id": "OB-001",
                "statement": (
                    "The restart criterion is a fixed iteration-count threshold "
                    "rather than the gap-based rule the literature uses."
                ),
                "rationale": (
                    "restart_criterion returns on the length of the gap history "
                    "and never inspects the gap values themselves."
                ),
                "file_refs": [
                    {
                        "path": "solver.py",
                        "symbol": "restart_criterion",
                        "note": "threshold is on length, not on the gap",
                    }
                ],
                "importance": "high",
                "confidence": "medium",
            },
            {
                "observation_id": "OB-002",
                "statement": "The benchmark harness records no timings.",
                "rationale": (
                    "run() returns solver outputs and discards everything about "
                    "how long each solve took."
                ),
                "file_refs": [{"path": "benchmark.py", "symbol": "run"}],
                "importance": "medium",
                "confidence": "high",
            },
        ],
        "uncertainties": [
            {
                "statement": "Whether the restart rule was ever compared to a gap rule.",
                "what_would_settle_it": "A benchmark run recording both variants.",
                "blocks": ["OB-001"],
            }
        ],
        "open_question": {
            "question": (
                "Does the length-based restart rule cost iterations against a "
                "gap-based rule on the harness's own problem set?"
            ),
            "why_it_matters": (
                "Restart choice dominates first-order LP convergence, and this "
                "one was chosen without a recorded comparison."
            ),
            "what_would_answer_it": (
                "Instrument benchmark.py to record iteration counts under both "
                "rules on the same problems."
            ),
            "blocked_by_evidence": False,
        },
        "next_actions": [
            {
                "action": "Record per-solve iteration counts in the benchmark harness.",
                "kind": "code",
                "rationale": "Nothing can be compared until something is measured.",
                "addresses_observations": ["OB-002"],
                "requires_human": False,
            }
        ],
    }
    payload.update(overrides)
    return payload


def grounding_for(root: Path, **overrides: Any) -> AssessmentGrounding:
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=str(root), capture_output=True, text=True, check=True
    ).stdout.split()
    payload: dict[str, Any] = {
        "repository_files": sorted(tracked),
        "literature_keys": [],
        "check_ids": [],
        "capsule_ids": [],
        "base_commit": "0" * 40,
    }
    payload.update(overrides)
    return AssessmentGrounding(**payload)


def build(root: Path, payload: dict[str, Any], **grounding: Any) -> TechnicalAssessment:
    document = dict(payload)
    document.update(
        {
            "assessment_id": "TA-20260914T101500Z-0a1b2c3d",
            "project_path": str(root),
            "goal": "Assess this repository and say what to do next.",
            "grounding": grounding_for(root, **grounding).model_dump(mode="json"),
            "provider": "fake",
        }
    )
    return TechnicalAssessment.model_validate(document)


# -- the object ----------------------------------------------------------


def test_a_grounded_assessment_validates(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    assessment = build(root, assessment_payload())
    validate_assessment(assessment)
    assert assessment.mode is AssessmentMode.REPOSITORY_ASSESSMENT
    assert assessment.cited_files == ["benchmark.py", "solver.py"]


def test_an_assessment_needs_no_scientific_identifier(tmp_path: Path) -> None:
    """The whole point: nothing here requires a capsule object to exist."""

    root = capsule_less_repo(tmp_path / "solver")
    assessment = build(root, assessment_payload())
    assert assessment.grounding.capsule_ids == []
    for observation in assessment.observations:
        assert observation.related_capsule_ids == []


def test_an_invented_proposal_identifier_is_refused(tmp_path: Path) -> None:
    """The exact cuPDLP failure, as a rule rather than as a hope."""

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][0]["related_capsule_ids"] = ["PR-002"]
    with pytest.raises(ValueError, match="scientific object"):
        build(root, payload)


def test_an_invented_claim_identifier_is_refused(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][1]["related_capsule_ids"] = ["CLAIM-0001"]
    with pytest.raises(ValueError, match="CLAIM-0001"):
        build(root, payload)


def test_a_file_that_is_not_tracked_is_refused(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][0]["file_refs"] = [{"path": "src/restart.py"}]
    with pytest.raises(ValueError, match="repository file"):
        build(root, payload)


def test_a_deleted_file_is_refused(tmp_path: Path) -> None:
    """A path that existed once is not a path this assessment may cite."""

    root = capsule_less_repo(tmp_path / "solver")
    (root / "legacy.py").write_text("# gone in the next commit\n", encoding="utf-8")
    _git(["add", "-A"], root)
    _git(["commit", "-qm", "add legacy"], root)
    _git(["rm", "-q", "legacy.py"], root)
    _git(["commit", "-qm", "remove legacy"], root)
    payload = assessment_payload()
    payload["observations"][0]["file_refs"] = [{"path": "legacy.py"}]
    with pytest.raises(ValueError, match="legacy.py"):
        build(root, payload)


@pytest.mark.parametrize(
    "path",
    [
        "/etc/passwd",
        "../outside.py",
        "solver/../../escape.py",
        "./solver.py",
        "sol\x00ver.py",
        "sol\nver.py",
        "windows\\path.py",
        "",
    ],
)
def test_a_path_that_cannot_address_this_repository_is_refused(path: str) -> None:
    with pytest.raises(ValueError):
        FileRef(path=path)


def test_a_symbol_must_look_like_a_symbol() -> None:
    assert FileRef(path="solver.py", symbol="restart_criterion").symbol is not None
    assert FileRef(path="solver.py", symbol="Solver.step").symbol == "Solver.step"
    with pytest.raises(ValueError, match="not a symbol"):
        FileRef(path="solver.py", symbol="the function that handles restarts")


def test_an_observation_must_rest_on_something() -> None:
    with pytest.raises(ValueError, match="rests on nothing"):
        Observation(
            observation_id="OB-001",
            statement="This code could be faster.",
            rationale="It feels slow.",
        )


def test_observation_ids_must_be_sequential(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][1]["observation_id"] = "OB-007"
    with pytest.raises(ValueError, match="sequential"):
        build(root, payload)


def test_an_assessment_must_name_the_question_that_matters(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload.pop("open_question")
    with pytest.raises(AssessmentValidationError, match="highest-value"):
        validate_assessment(build(root, payload))


def test_a_grounded_statement_of_insufficiency_is_a_valid_result(
    tmp_path: Path,
) -> None:
    """ "I cannot answer this from what I was given" is an answer."""

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["open_question"]["blocked_by_evidence"] = True
    assessment = build(root, payload)
    validate_assessment(assessment)
    assert assessment.open_question is not None
    assert assessment.open_question.blocked_by_evidence is True


# -- grounding violations and the prompt ---------------------------------


def test_violations_are_computed_from_the_payload(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][0]["related_capsule_ids"] = ["PR-002"]
    payload["observations"][1]["file_refs"] = [{"path": "does/not/exist.py"}]
    violations = grounding_violations(payload, grounding_for(root))
    cited = {item.cited for item in violations}
    assert cited == {"PR-002", "does/not/exist.py"}


def test_a_structurally_broken_payload_reports_no_grounding_violation(
    tmp_path: Path,
) -> None:
    """Only a grounding failure may enter the correction path.

    A blank statement is a shape problem, not a grounding problem: there is
    nothing for a correction worker to reground, and sending it to one would be
    asking a model again in the hope of a different answer.
    """

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][0]["statement"] = ""
    assert grounding_violations(payload, grounding_for(root)) == ()


def test_an_observation_resting_on_nothing_is_a_grounding_violation(
    tmp_path: Path,
) -> None:
    """Found by the cuPDLP pilot.

    One observation of seven omitted its grounding entirely and the whole
    assessment was refused with no correction available, because the correction
    trigger knew only about citations that were *wrong* and not about citations
    that were *absent*. Those are the same category and have the same repair.
    """

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][1].pop("file_refs")
    violations = grounding_violations(payload, grounding_for(root))
    assert len(violations) == 1
    assert violations[0].observation_id == "OB-002"
    assert violations[0].ungrounded is True
    assert "rests on nothing" in violations[0].describe()


def test_an_ungrounded_observation_is_repaired_by_the_one_correction(
    tmp_path: Path,
) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    ungrounded = assessment_payload()
    ungrounded["observations"][1].pop("file_refs")
    provider = assessment_provider(
        [
            ScriptedResponse(structured=ungrounded),
            ScriptedResponse(structured=assessment_payload()),
        ]
    )
    outcome = AssessmentController(
        providers={provider.name: provider}, config=fake_config()
    ).assess(
        project_path=root,
        goal="Assess this repository.",
        profile=build_project_profile(project_path=root),
    )
    assert outcome.grounding_correction is not None
    assert outcome.model_calls == 2
    assert len(outcome.assessment.observations) == 2


def test_the_correction_prompt_names_an_ungrounded_observation(
    tmp_path: Path,
) -> None:
    from research_os.assessment.planner import build_grounding_correction_prompt

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][1].pop("file_refs")
    grounding = grounding_for(root)
    prompt = build_grounding_correction_prompt(
        goal="Assess this repository.",
        payload=payload,
        violations=grounding_violations(payload, grounding),
        grounding=grounding,
    )
    assert "OB-002 rests on nothing" in prompt
    assert "rests on at least one entry from the lists above" in prompt


def test_the_repository_context_does_not_repeat_the_citable_list(
    tmp_path: Path,
) -> None:
    """One copy of the file list, not two.

    The prompt enumerates every citable path once, under the heading that says
    what citing one means. Repeating them in the repository block made an
    assessment prompt for a 5,402-file repository 199,551 characters, of which
    196,000 were the same list twice.
    """

    from research_os.assessment.planner import render_repository_context

    root = capsule_less_repo(tmp_path / "solver")
    grounding = grounding_for(root)
    context = render_repository_context(
        tracked=list(grounding.repository_files), base_commit="0" * 40, notes=[]
    )
    assert "solver.py" not in context
    assert "tracked_files: 3" in context
    assert "base_commit: " + "0" * 40 in context

    prompt = build_assessment_prompt(
        goal="Assess this repository.",
        grounding=grounding,
        repository_context=context,
    )
    assert prompt.count("solver.py") == 1


def test_a_truncated_file_list_says_so_in_both_places(tmp_path: Path) -> None:
    from research_os.assessment.planner import (
        MAX_CITABLE_FILES,
        render_repository_context,
    )

    many = [f"src/module_{index:05d}.py" for index in range(MAX_CITABLE_FILES + 50)]
    context = render_repository_context(tracked=many, base_commit="0" * 40, notes=[])
    assert f"tracked_files: {len(many)}" in context
    assert "cannot rest on a path that is not listed" in context


def test_an_uncertainty_pointing_at_a_non_observation_is_a_violation(
    tmp_path: Path,
) -> None:
    """Found by Pilot C.

    An uncertainty declared it blocked ``"open_question"`` -- the name of a
    sibling field rather than an observation id -- and the whole assessment was
    discarded for it with no correction available.
    """

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["uncertainties"][0]["blocks"] = ["open_question"]
    violations = grounding_violations(payload, grounding_for(root))
    assert len(violations) == 1
    assert violations[0].internal is True
    assert "not an observation in this assessment" in violations[0].describe()


def test_an_action_pointing_at_a_missing_observation_is_a_violation(
    tmp_path: Path,
) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["next_actions"][0]["addresses_observations"] = ["OB-009"]
    violations = grounding_violations(payload, grounding_for(root))
    assert len(violations) == 1
    assert violations[0].cited == "OB-009"


def test_a_broken_internal_reference_is_repaired_by_the_one_correction(
    tmp_path: Path,
) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    broken = assessment_payload()
    broken["uncertainties"][0]["blocks"] = ["open_question"]
    provider = assessment_provider(
        [
            ScriptedResponse(structured=broken),
            ScriptedResponse(structured=assessment_payload()),
        ]
    )
    outcome = AssessmentController(
        providers={provider.name: provider}, config=fake_config()
    ).assess(
        project_path=root,
        goal="Assess this repository.",
        profile=build_project_profile(project_path=root),
    )
    assert outcome.grounding_correction is not None
    assert outcome.model_calls == 2


def test_the_correction_prompt_explains_what_an_observation_id_is(
    tmp_path: Path,
) -> None:
    from research_os.assessment.planner import build_grounding_correction_prompt

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["uncertainties"][0]["blocks"] = ["open_question"]
    grounding = grounding_for(root)
    prompt = build_grounding_correction_prompt(
        goal="Assess this repository.",
        payload=payload,
        violations=grounding_violations(payload, grounding),
        grounding=grounding,
    )
    assert "A field name is not" in prompt
    assert "an observation id" in prompt


def test_the_correction_prompt_says_to_renumber_after_dropping(
    tmp_path: Path,
) -> None:
    """The prompt must not ask for something another validator then refuses.

    Pilot C's correction did exactly what it was told -- dropped an observation
    that could not be grounded -- and left the remaining ids non-contiguous, so
    the sequential-id rule refused the repair. The instruction was achievable
    only if you already knew about a rule the prompt never mentioned.
    """

    from research_os.assessment.planner import build_grounding_correction_prompt

    root = capsule_less_repo(tmp_path / "solver")
    payload = assessment_payload()
    payload["observations"][1].pop("file_refs")
    grounding = grounding_for(root)
    prompt = build_grounding_correction_prompt(
        goal="Assess this repository.",
        payload=payload,
        violations=grounding_violations(payload, grounding),
        grounding=grounding,
    )
    assert "RENUMBER" in prompt
    assert "with no gap" in prompt


def test_every_reference_failure_is_correctable_and_no_shape_failure_is(
    tmp_path: Path,
) -> None:
    """The class, not the instances.

    Three live pilots in this release each found a different broken-reference
    shape. This asserts the set: every way an assessment can point at something
    that is not there reaches the single bounded correction, and every way it
    can simply be malformed does not.
    """

    root = capsule_less_repo(tmp_path / "solver")
    grounding = grounding_for(root)

    def violations_for(mutate) -> tuple:
        payload = assessment_payload()
        mutate(payload)
        return grounding_violations(payload, grounding)

    def set_ref(field: str, value: object):
        def apply(payload: dict[str, Any]) -> None:
            payload["observations"][0][field] = value

        return apply

    correctable = [
        set_ref("file_refs", [{"path": "nowhere.py"}]),
        set_ref("literature_keys", ["doi:10.0000/invented"]),
        set_ref("check_ids", ["typecheck"]),
        set_ref("related_capsule_ids", ["CLAIM-0001"]),
        lambda payload: payload["observations"][1].pop("file_refs"),
        lambda payload: payload["uncertainties"][0].update({"blocks": ["nope"]}),
        lambda payload: payload["next_actions"][0].update(
            {"addresses_observations": ["OB-404"]}
        ),
    ]
    for mutate in correctable:
        assert violations_for(mutate), "a broken reference must be correctable"

    def renumber_without_dangling(payload: dict[str, Any]) -> None:
        """A non-sequential id, with nothing left pointing at the old one."""

        payload["next_actions"][0]["addresses_observations"] = []
        payload["uncertainties"][0]["blocks"] = []
        payload["observations"][1]["observation_id"] = "OB-007"

    not_correctable = [
        lambda payload: payload["observations"][0].update({"statement": ""}),
        lambda payload: payload.update({"summary": ""}),
        lambda payload: payload.pop("open_question"),
        renumber_without_dangling,
    ]
    for mutate in not_correctable:
        assert not violations_for(mutate), (
            "a shape failure has nothing to reground and must not be re-asked"
        )

    # A payload carrying both kinds enters the correction on the strength of its
    # grounding half, exactly as the proposal layer does. That was reviewed and
    # accepted one release ago: the cost is one budget-checked, non-recursive
    # call, and the second failure is reported honestly.
    both = violations_for(
        lambda payload: (
            payload["observations"][0].update({"statement": ""}),
            payload["observations"][0].update({"check_ids": ["typecheck"]}),
        )
    )
    assert both


def test_the_prompt_forbids_scientific_identifiers(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    prompt = build_assessment_prompt(
        goal="Assess this repository.",
        grounding=grounding_for(root),
        repository_context="",
    )
    assert "THIS REPOSITORY IS NOT A RESEARCH CAPSULE PROJECT" in prompt
    assert "Do not write one." in prompt
    assert "PR-002" in prompt
    assert "solver.py" in prompt


# -- the controller ------------------------------------------------------


def assessment_provider(
    responses: list[ScriptedResponse], *, name: str = "fake"
) -> FakeProvider:
    return FakeProvider(
        name=name, family="fake-family", responses={str(Role.PLANNER): responses}
    )


def test_the_controller_produces_and_archives_an_assessment(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    provider = assessment_provider([ScriptedResponse(structured=assessment_payload())])
    controller = AssessmentController(
        providers={provider.name: provider}, config=fake_config()
    )
    outcome = controller.assess(
        project_path=root,
        goal="Assess this repository and say what to do next.",
        profile=build_project_profile(project_path=root),
    )
    assert outcome.assessment.mode is AssessmentMode.REPOSITORY_ASSESSMENT
    assert outcome.model_calls == 1
    assert outcome.grounding_correction is None
    stored = AssessmentStore.open(outcome.assessment.assessment_id).load()
    assert stored.summary == outcome.assessment.summary
    assert stored.base_commit is not None


def test_an_assessment_writes_nothing_into_the_repository(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    before = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    provider = assessment_provider([ScriptedResponse(structured=assessment_payload())])
    AssessmentController(
        providers={provider.name: provider}, config=fake_config()
    ).assess(
        project_path=root,
        goal="Assess this repository.",
        profile=build_project_profile(project_path=root),
    )
    after = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(root),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert after == before
    assert not (root / ".research").exists()
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=str(root),
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == head
    )


def test_the_controller_refuses_to_assess_a_capsule_project(tmp_path: Path) -> None:
    from tests.research_helpers import init_repo

    root = init_repo(tmp_path / "capsule", capsule=True)
    provider = assessment_provider([ScriptedResponse(structured=assessment_payload())])
    with pytest.raises(AssessmentValidationError, match="Research Capsule"):
        AssessmentController(
            providers={provider.name: provider}, config=fake_config()
        ).assess(
            project_path=root,
            goal="Assess this project.",
            profile=build_project_profile(project_path=root),
        )
    assert provider.calls == []


def test_one_grounding_correction_repairs_an_invented_identifier(
    tmp_path: Path,
) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    invented = assessment_payload()
    invented["observations"][0]["related_capsule_ids"] = ["PR-002"]
    provider = assessment_provider(
        [
            ScriptedResponse(structured=invented),
            ScriptedResponse(structured=assessment_payload()),
        ]
    )
    outcome = AssessmentController(
        providers={provider.name: provider}, config=fake_config()
    ).assess(
        project_path=root,
        goal="Assess this repository.",
        profile=build_project_profile(project_path=root),
    )
    assert outcome.grounding_correction is not None
    assert outcome.grounding_correction.refused == ("PR-002",)
    assert outcome.model_calls == 2
    events = [
        item["event"]
        for item in AssessmentStore.open(outcome.assessment.assessment_id).iter_events()
    ]
    assert "assessment_grounding_refused" in events
    assert "assessment_grounding_corrected" in events


def test_there_is_exactly_one_correction(tmp_path: Path) -> None:
    """A second failure ends it. Asking until it passes is not a gate."""

    root = capsule_less_repo(tmp_path / "solver")
    invented = assessment_payload()
    invented["observations"][0]["related_capsule_ids"] = ["PR-002"]
    still_invented = assessment_payload()
    still_invented["observations"][0]["related_capsule_ids"] = ["PR-003"]
    provider = assessment_provider(
        [
            ScriptedResponse(structured=invented),
            ScriptedResponse(structured=still_invented),
            ScriptedResponse(structured=assessment_payload()),
        ]
    )
    with pytest.raises(AssessmentGroundingError, match="one correction did not"):
        AssessmentController(
            providers={provider.name: provider}, config=fake_config()
        ).assess(
            project_path=root,
            goal="Assess this repository.",
            profile=build_project_profile(project_path=root),
        )
    assert len(provider.calls) == 2


def test_a_budget_of_one_permits_no_correction(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    invented = assessment_payload()
    invented["observations"][0]["related_capsule_ids"] = ["PR-002"]
    provider = assessment_provider(
        [
            ScriptedResponse(structured=invented),
            ScriptedResponse(structured=assessment_payload()),
        ]
    )
    with pytest.raises(AssessmentValidationError, match="model call"):
        AssessmentController(
            providers={provider.name: provider}, config=fake_config()
        ).assess(
            project_path=root,
            goal="Assess this repository.",
            profile=build_project_profile(project_path=root),
            max_model_calls=1,
        )
    assert len(provider.calls) == 1


def test_the_rendering_says_what_this_object_is_not(tmp_path: Path) -> None:
    root = capsule_less_repo(tmp_path / "solver")
    rendered = render_assessment(build(root, assessment_payload()))
    assert "not a scientific" in rendered
    assert "nothing in it is promotable" in rendered
    assert "restart_criterion" in rendered


# -- the whole run -------------------------------------------------------


def test_a_capsule_less_research_run_reaches_ready_for_human(tmp_path: Path) -> None:
    """The cuPDLP replay, end to end, with nothing invented."""

    root = capsule_less_repo(tmp_path / "solver")
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(
                        tasks=[
                            proposal_task(
                                title="Assess the solver repository",
                                goal=(
                                    "Say what state this repository is in and what "
                                    "the highest-value next question is."
                                ),
                            )
                        ],
                        summary="Assess the repository and rank what to do next.",
                    )
                ),
                ScriptedResponse(structured=assessment_payload()),
            ]
        },
    )
    controller = make_controller(provider)
    store, run = controller.start(
        project_path=root,
        goal="Assess this repository and identify the best next technical question.",
        budget=ResearchBudget(max_tasks=2, max_model_calls=6, max_write_tasks=0),
    )
    assert run.profile is not None
    assert run.profile.capsule_present is False
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    assert run.tasks[0].artifact_id is not None
    assert run.tasks[0].artifact_id.startswith("TA-")
    events = [item["event"] for item in store.iter_events()]
    assert "assessment_produced" in events
    assert "proposal_produced" not in events
    assert not (root / ".research").exists()


def test_a_capsule_project_still_uses_the_scientific_pipeline(tmp_path: Path) -> None:
    """The other half of the guarantee: nothing about capsule projects moved."""

    from tests.proposal_helpers import proposal_payload
    from tests.research_helpers import init_repo

    root = init_repo(tmp_path / "capsule", capsule=True)
    provider = FakeProvider(
        name="fake",
        family="fake-family",
        responses={
            str(Role.PLANNER): [
                ScriptedResponse(
                    structured=plan_payload(tasks=[proposal_task()]),
                ),
                ScriptedResponse(structured=proposal_payload()),
            ],
            str(Role.REVIEWER): [
                ScriptedResponse(
                    structured={
                        "verdict": "PASS",
                        "summary": "The proposal rests on what the project holds.",
                        "findings": [],
                    }
                )
            ],
        },
    )
    controller = make_controller(provider)
    store, run = controller.start(
        project_path=root,
        goal="Propose what would settle the open question.",
        budget=ResearchBudget(max_tasks=2, max_model_calls=8, max_write_tasks=0),
    )
    assert run.profile is not None
    assert run.profile.capsule_present is True
    run = controller.execute(store)
    assert run.state is ResearchState.READY_FOR_HUMAN
    events = [item["event"] for item in store.iter_events()]
    assert "proposal_produced" in events
    assert "assessment_produced" not in events
    assert run.tasks[0].artifact_id is not None
    assert run.tasks[0].artifact_id.startswith("PROP-")


def test_a_symbol_may_be_written_with_parentheses(tmp_path: Path) -> None:
    """Found by the delta review.

    `solve()` is a very common way to name a function and the prompt asks for
    "a function, class or module name" without forbidding it. The resulting
    failure was a shape error with nothing to reground, so a whole valid
    assessment was discarded over two characters with no correction offered.
    Parentheses are inert at the prompt boundary: the no-whitespace rule already
    prevents a symbol from standing alone on a line.
    """

    for symbol in ("solve()", "Solver::step(x)", "Vec<T>", "items[0]"):
        assert FileRef(path="a.py", symbol=symbol).symbol == symbol
    with pytest.raises(ValueError, match="not a symbol"):
        FileRef(path="a.py", symbol="the function that handles restarts")


def test_a_capsule_less_assessment_fits_a_budget_of_one(tmp_path: Path) -> None:
    """Found by the delta review.

    `MINIMUM_CALLS[PROPOSAL]` is three -- literature, the proposal, the
    assessment -- which is right for the scientific pipeline and wrong for a
    capsule-less project, whose whole assessment ceiling is two. Over-reserving
    is the safe direction, but the refusal stated a requirement that was false
    for the dispatch it refused.
    """

    from research_os.automation.profile import build_project_profile
    from research_os.research.controller import ResearchController
    from research_os.research.models import ResearchBudget, ResearchTask, TaskKind

    root = capsule_less_repo(tmp_path / "solver")
    profile = build_project_profile(project_path=root)
    run = ResearchRun(
        run_id="RR-20260914T101500Z-0a1b2c3d",
        project_path=str(root),
        goal="Assess it.",
        profile=profile,
        budget=ResearchBudget(max_model_calls=2),
        model_calls_used=1,
    )
    task = ResearchTask(
        task_id="T-001", kind=TaskKind.PROPOSAL, title="Assess", goal="Assess it."
    )
    # One call remains; an assessment needs one, so the plan is affordable.
    ResearchController._assert_budget_could_finish(run, [task])


def test_a_capsule_project_still_reserves_the_full_proposal_cost(
    tmp_path: Path,
) -> None:
    from research_os.automation.profile import build_project_profile
    from research_os.errors import ResearchPlanError
    from research_os.research.controller import ResearchController
    from research_os.research.models import ResearchBudget, ResearchTask, TaskKind
    from tests.research_helpers import init_repo

    root = init_repo(tmp_path / "capsule", capsule=True)
    run = ResearchRun(
        run_id="RR-20260914T101500Z-0a1b2c3d",
        project_path=str(root),
        goal="Propose.",
        profile=build_project_profile(project_path=root),
        budget=ResearchBudget(max_model_calls=2),
        model_calls_used=1,
    )
    task = ResearchTask(
        task_id="T-001", kind=TaskKind.PROPOSAL, title="Propose", goal="Propose it."
    )
    with pytest.raises(ResearchPlanError, match="at least 3 model calls"):
        ResearchController._assert_budget_could_finish(run, [task])
