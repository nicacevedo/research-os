"""Which model plans, who decides it, and what a failed plan must not change.

v1.1 changed one value: the shipped planner default. A thirty-call benchmark
over five archived fixtures, judged by the production validators, found that the
smaller model produced every structured-output exhaustion and every placeholder
plan in it while the stronger one produced neither and needed fewer calls
(``docs/V1_BUILD_RECORD.md`` §32). The default moved; nothing else did.

These tests exist because "nothing else did" is the part that can rot. The
planner role is read by four workers, a plan is an instruction to run commands,
and the one thing a *failed* plan must never buy is a different model. So the
invariants pinned here are the default itself, the researcher's authority to
override it, honesty about which model actually answered, and -- the safety
property -- that no rejection of any kind escalates the planner. There is no
fallback in this release, and these tests are what would notice one appearing.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.config import config_path, default_config, load_config
from research_os.automation.models import Access, Role
from research_os.automation.providers import ClaudeCodeProvider, InvocationRequest
from research_os.errors import ProviderInvocationError, ResearchPlanError
from research_os.research.models import ResearchBudget
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.research_helpers import (
    analysis_task,
    checkpoint_task,
    init_repo,
    make_controller,
    plan_payload,
    task,
)

# -- the default itself -------------------------------------------------------


def test_the_shipped_planner_plans_with_the_strongest_model() -> None:
    """The one value this release changed, pinned where it is authoritative."""

    assert default_config().role("planner").model == "opus"


def test_changing_the_planner_model_did_not_widen_what_a_planner_may_do() -> None:
    """A stronger model is still a model with no tools and no repository.

    The planner's authority is its position, not its size. If a future change to
    this default ever arrived alongside a tool or a write bit, that is the thing
    worth failing over -- not the model name.
    """

    planner = default_config().role("planner")
    assert planner.read_only is True
    assert planner.access is Access.CONTEXT_ONLY
    assert planner.tools == []


def test_the_reviewer_still_differs_from_the_implementer() -> None:
    """The property the defaults existed to hold, re-checked after the change."""

    config = default_config()
    assert config.role("coder").model != config.role("reviewer").model


# -- the researcher still decides ---------------------------------------------


def test_a_configured_planner_model_beats_the_shipped_default(
    automation_home: Path,
) -> None:
    config_path().write_text(
        "planner:\n"
        "  provider: claude\n"
        "  model: sonnet\n"
        "  effort: high\n"
        "  read_only: true\n",
        encoding="utf-8",
    )
    config = load_config()

    assert config.role("planner").model == "sonnet"
    assert config.explicit_roles == frozenset({"planner"})


def test_configuring_another_role_leaves_the_planner_default_alone(
    automation_home: Path,
) -> None:
    config_path().write_text(
        "reviewer:\n  provider: claude\n  model: sonnet\n  read_only: true\n",
        encoding="utf-8",
    )
    config = load_config()

    assert config.role("planner").model == "opus"
    assert config.explicit_roles == frozenset({"reviewer"})


# -- honesty about which model answered ---------------------------------------


def _envelope(**updates: Any) -> str:
    payload: dict[str, Any] = {"result": "done", "is_error": False}
    payload.update(updates)
    return json.dumps(payload)


class _Recorder:
    def __init__(self, stdout: str, returncode: int = 0) -> None:
        self.stdout = stdout
        self.returncode = returncode

    def __call__(self, argv: list[str], **kwargs: Any) -> Any:
        return subprocess.CompletedProcess(
            argv, self.returncode, stdout=self.stdout, stderr=""
        )


def _planner_request(model: str) -> InvocationRequest:
    return InvocationRequest(
        role=Role.PLANNER,
        prompt="plan this",
        cwd=Path("/tmp"),
        read_only=True,
        timeout_seconds=60,
        model=model,
        access=Access.CONTEXT_ONLY,
        tools=(),
    )


def test_the_run_records_the_model_that_answered_not_the_one_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Load-bearing now that the default names a specific model.

    Asking for ``opus`` and being answered by something else must appear in the
    ledger as the model that actually answered. A record that echoed the alias
    back would let a machine where the default is unreachable report runs
    planned by a model that never ran.
    """

    monkeypatch.setattr(
        subprocess,
        "run",
        _Recorder(_envelope(modelUsage={"claude-sonnet-5": {"outputTokens": 7}})),
    )
    result = ClaudeCodeProvider().invoke(_planner_request("opus"))

    assert result.resolved_model == "claude-sonnet-5"


def test_the_requested_model_is_recorded_when_the_provider_confirms_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        subprocess,
        "run",
        _Recorder(
            _envelope(
                modelUsage={
                    "claude-haiku-4-5-20251001": {"outputTokens": 2},
                    "claude-opus-5": {"outputTokens": 9},
                }
            )
        ),
    )
    result = ClaudeCodeProvider().invoke(_planner_request("opus"))

    assert result.resolved_model == "claude-opus-5"


def test_the_planner_model_reaches_the_provider_command_line(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[list[str]] = []

    def record(argv: list[str], **kwargs: Any) -> Any:
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, stdout=_envelope(), stderr="")

    monkeypatch.setattr(subprocess, "run", record)
    ClaudeCodeProvider().invoke(
        _planner_request(default_config().role("planner").model)
    )

    assert seen[0][seen[0].index("--model") + 1] == "opus"


# -- what a failed plan must not buy ------------------------------------------


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    mapping = {
        "RESEARCH_OS_CONFIG_HOME": root / "config",
        "RESEARCH_OS_DATA_HOME": root / "data",
        "RESEARCH_OS_CACHE_HOME": root / "cache",
        "RESEARCH_OS_STATE_HOME": root / "state",
    }
    for name, path in mapping.items():
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return mapping["RESEARCH_OS_STATE_HOME"]


def _planner_only(responses: list[ScriptedResponse]) -> FakeProvider:
    """A provider scripted for planning and nothing else.

    Every test below stops at or before ``PLAN_READY``, so a response for any
    other role would only hide a dispatch that should not have happened.
    """

    return FakeProvider(responses={str(Role.PLANNER): responses})


def _plan(tmp_path: Path, provider: FakeProvider, **overrides: Any) -> Any:
    repo = init_repo(tmp_path / "project")
    controller = make_controller(provider)
    return controller.start(
        project_path=repo,
        goal="Find out whether widget deformation stays linear above 10N.",
        budget=overrides.pop("budget", ResearchBudget()),
        **overrides,
    )


def _planner_models(provider: FakeProvider) -> list[str | None]:
    return [item.model for item in provider.requests_for(Role.PLANNER)]


def test_a_plan_accepted_first_time_spends_one_call_at_the_configured_model(
    research_home: Path, tmp_path: Path
) -> None:
    provider = _planner_only([ScriptedResponse(structured=plan_payload())])
    _, run = _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner"]
    assert run.model_calls_used == 1


def test_a_provider_failure_is_re_asked_of_the_very_same_model(
    research_home: Path, tmp_path: Path
) -> None:
    """The failure class an escalating router would most want to react to.

    ``error_max_structured_output_retries`` is exactly the failure the v1.1
    benchmark saw three times from the smaller model, and exactly the trigger a
    bounded escalation policy would have used. This release deliberately has no
    such policy: the re-ask is the same question to the same model, and the
    researcher's configured planner is the only thing that decides which.
    """

    provider = _planner_only(
        [
            ScriptedResponse(
                structured=None,
                exit_code=1,
                error="error_max_structured_output_retries",
            ),
            ScriptedResponse(structured=plan_payload()),
        ]
    )
    _, run = _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]
    assert run.model_calls_used == 2


def test_a_refused_scientific_checkpoint_does_not_escalate_the_planner(
    research_home: Path, tmp_path: Path
) -> None:
    """The case the benchmark hit most often, and the one that must never route.

    A planner labelling a checkpoint ``claim_acceptance`` in a project with no
    Claim is the controller's scientific-authority guard doing its job. It is a
    correct refusal of a real plan, not a model that could not answer, and
    reaching for a stronger model because a scientific boundary was inconvenient
    is the exact move this system must never make.
    """

    provider = _planner_only(
        [
            ScriptedResponse(
                structured=plan_payload(
                    tasks=[
                        checkpoint_task(
                            task_id="T-001", checkpoint_kind="claim_acceptance"
                        )
                    ]
                )
            ),
            ScriptedResponse(structured=plan_payload()),
        ]
    )
    _, run = _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]
    assert run.model_calls_used == 2


def test_a_budget_refusal_does_not_escalate_the_planner(
    research_home: Path, tmp_path: Path
) -> None:
    """A plan too big for its budget is a plan, not a provider failure."""

    oversized = plan_payload(
        tasks=[
            task(task_id="T-001"),
            analysis_task(task_id="T-002"),
            analysis_task(task_id="T-003"),
        ]
    )
    provider = _planner_only(
        [
            ScriptedResponse(structured=oversized),
            ScriptedResponse(structured=plan_payload()),
        ]
    )
    _, run = _plan(tmp_path, provider, budget=ResearchBudget(max_tasks=1))

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]
    assert run.model_calls_used == 2


def test_a_degenerate_plan_does_not_escalate_the_planner(
    research_home: Path, tmp_path: Path
) -> None:
    """The other failure an escalating router would have reacted to.

    Three of the thirty benchmark calls came back as placeholders. The guard
    refuses them and the controller re-asks -- of the same model, because which
    model plans is the researcher's decision and not a consequence of one bad
    answer.
    """

    provider = _planner_only(
        [
            ScriptedResponse(
                structured=plan_payload(
                    summary="test", tasks=[task(title="test", goal="test")]
                )
            ),
            ScriptedResponse(structured=plan_payload()),
        ]
    )
    _, run = _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]
    assert run.model_calls_used == 2


def test_the_planner_is_bounded_at_two_attempts_and_both_are_charged(
    research_home: Path, tmp_path: Path
) -> None:
    """Two attempts however they are spent, and no third at any model."""

    degenerate = ScriptedResponse(
        structured=plan_payload(summary="test", tasks=[task(title="test", goal="test")])
    )
    provider = _planner_only(
        [degenerate, degenerate, ScriptedResponse(structured=plan_payload())]
    )
    with pytest.raises(ResearchPlanError, match="placeholder"):
        _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]


def test_an_unreachable_planner_model_fails_the_run_and_says_so(
    research_home: Path, tmp_path: Path
) -> None:
    """A default naming a model this machine cannot reach must fail loudly.

    Not silently answered by whatever is available: the provider's own words
    reach the researcher, and the run ends rather than producing a plan nobody
    can attribute.
    """

    unavailable = ScriptedResponse(
        structured=None, exit_code=1, error="model 'opus' is not available"
    )
    provider = _planner_only([unavailable, unavailable])
    with pytest.raises(ProviderInvocationError, match="is not available"):
        _plan(tmp_path, provider)

    assert _planner_models(provider) == ["fake-planner", "fake-planner"]


def test_every_planner_attempt_is_ledgered_with_its_own_provider_and_model(
    research_home: Path, tmp_path: Path
) -> None:
    """Two attempts, two invocation records, each naming what answered it."""

    provider = _planner_only(
        [
            ScriptedResponse(
                structured=None,
                exit_code=1,
                error="error_max_structured_output_retries",
            ),
            ScriptedResponse(structured=plan_payload()),
        ]
    )
    store, _ = _plan(tmp_path, provider)

    records = sorted((store.path("invocations")).glob("*.json"))
    assert len(records) == 2
    for path in records:
        payload = json.loads(path.read_text(encoding="utf-8"))
        assert payload["role"] == "planner"
        assert payload["provider"] == "fake"
        assert payload["model"] == "fake-planner"
