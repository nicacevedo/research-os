"""A capsule, a fake router and a context, for the graph tests.

The router here is a stand-in for provider adapters, not for the routing logic:
:mod:`tests.test_runtime_routing` exercises the real
:class:`~research_os.runtime.routing.ModelRouter` against fake adapters. What
these graph tests need is a model that answers predictably so that what is being
asserted is the graph's control flow rather than a model's mood.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.config import BudgetDefaults, RuntimeConfig, RuntimeSettings
from research_os.runtime.context import CycleContext
from research_os.runtime.db import Database
from research_os.runtime.failures import FailureClass
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.interfaces import Independence, ModelRequest, ModelResponse
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.queue import WorkQueue
from research_os.runtime.routing import (
    IndependenceUnavailableError,
    ProviderCallFailedError,
)
from research_os.runtime.store import RuntimeStore
from tests.automation_helpers import commit_all
from tests.fs_helpers import (
    claim_data,
    evidence_data,
    hypothesis_data,
    make_git_repo,
    question_data,
    write_minimal_capsule,
    write_yaml,
)


@dataclass
class ScriptedRouter:
    """Answers each role from a script, and records what it was asked.

    It models the two provider outcomes the real router distinguishes, and
    keeps them distinct, because conflating them is the defect this double is
    most likely to hide:

    ``fail_roles`` -- the provider *answered* with something unusable. A
    returned response with an error, exactly as :class:`ModelRouter` returns
    for malformed structured output.

    ``unavailable_roles`` / ``unavailable`` -- the provider **did not answer**.
    A raised :class:`ProviderCallFailedError`, exactly as the real router now
    raises, carrying the breaker deadline a queue would schedule against. A
    double that returned here instead would let a test pass against a runtime
    that folds an outage into a scientific conclusion, which is what shipped.
    """

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: Answers keyed by *prompt identity* rather than by role, consulted
    #: first.
    #:
    #: Two templates legitimately share one role -- the portfolio's explorers
    #: reuse the runtime's, and its replication designer reuses
    #: ``replicator`` -- because a role is a routing and provenance bucket
    #: while the template is the question. A double keyed only by role cannot
    #: tell them apart, which would make a test of one silently exercise the
    #: other.
    answers_by_prompt: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)
    fail_roles: set[str] = field(default_factory=set)
    #: Roles the provider cannot serve at all. Empty set plus ``unavailable``
    #: true means every role.
    unavailable_roles: set[str] = field(default_factory=set)
    unavailable: bool = False
    #: What the breaker would publish as ``cooldown_until``.
    cooldown_until: datetime | None = None
    #: Whether an invocation was actually made before failing. False models
    #: routing turning the call away because every provider is cooling, which
    #: must not cost the work item an attempt.
    attempted: bool = True
    #: When set, every answered call writes a real ``model_calls`` row and the
    #: response names it.
    #:
    #: The real router does this and the discovery portfolio depends on it:
    #: ``idea_reviews.call_id`` has a foreign key, and the independence check
    #: compares a review's call against the call that produced the work under
    #: review. A double that returned no call id would let a test pass against
    #: a review whose independence could not be established.
    store: Any = None
    #: Which provider answers each role, so a test can arrange a board with
    #: one family or with three. Falls back to ``scripted``.
    providers: dict[str, str] = field(default_factory=dict)
    models_by_role: dict[str, str] = field(default_factory=dict)
    #: Roles for which no second provider family can be found. The real
    #: router raises `IndependenceUnavailableError` here, and this double
    #: could not, which is how the portfolio's missing handler for it
    #: survived ~4,400 tests: every independence assertion in the portfolio
    #: suite reads a value `providers` was configured to hand out.
    independence_unavailable_roles: frozenset[str] = frozenset()

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        role = str(request.role)
        if role in self.independence_unavailable_roles:
            raise IndependenceUnavailableError(
                f"no second provider family is installed for {role}"
            )
        if self.unavailable or role in self.unavailable_roles:
            raise ProviderCallFailedError(
                f"no healthy provider offers {request.capability} for {role}",
                failure_class=FailureClass.PROVIDER_UNAVAILABLE,
                retry_at=self.cooldown_until,
                attempted=self.attempted,
            )
        if role in self.fail_roles:
            return ModelResponse(
                provider="scripted",
                model="scripted-1",
                error="scripted failure",
                independence=Independence.NONE,
            )
        structured = self.answers_by_prompt.get(
            request.prompt_version, self.answers.get(role)
        )
        provider = self.providers.get(role, "scripted")
        model = self.models_by_role.get(role, "scripted-1")
        call_id = None
        if self.store is not None:
            from research_os.runtime.models import ModelCallStatus

            call_id = self.store.record_model_call(
                provider=provider,
                model=model,
                role=role,
                status=ModelCallStatus.OK,
                prompt_version=request.prompt_version,
                cost_usd=0.001,
            ).call_id
        return ModelResponse(
            provider=provider,
            model=model,
            text=None,
            structured=structured,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
            latency_ms=1,
            call_id=call_id,
            independence=Independence.DIFFERENT_CONTEXT,
            independence_note="scripted",
        )

    def requests_for(self, role: str) -> list[ModelRequest]:
        return [r for r in self.requests if str(r.role) == role]

    def requests_for_prompt(self, prompt_version: str) -> list[ModelRequest]:
        return [r for r in self.requests if r.prompt_version == prompt_version]


def plan_answer(
    action: str, *, rationale: str = "because the frontier says so"
) -> dict[str, Any]:
    return {
        "action": action,
        "rationale": rationale,
        "addresses": ["Q-0001"],
        "expected_information_gain": "medium",
    }


def review_answer(verdict: str = "sound") -> dict[str, Any]:
    return {
        "verdict": verdict,
        "methodological_validity": "adequate for the question",
        "claim_support": "supported",
        "alternative_explanations": [],
        "evidence_gaps": [],
        "overclaiming": [],
        "required_changes": [],
    }


def make_capsule(
    root: Path, *, project_id: str = "alpha-project", empty: bool = False
) -> Path:
    """A real Git repository with a real capsule.

    Not a fixture double: the frontier is derived from files, so a test that
    stubs the capsule would be testing the stub.
    """

    repo = make_git_repo(root)
    write_minimal_capsule(repo, project_id=project_id)
    if empty:
        commit_all(repo, "capsule")
        return repo
    research = repo / ".research"
    for name in ("questions", "hypotheses", "claims", "evidence"):
        (research / name).mkdir()
    write_yaml(research / "questions" / "Q-0001.yaml", question_data())
    write_yaml(
        research / "hypotheses" / "HYP-0001.yaml",
        hypothesis_data(status="active", addresses=["Q-0001"]),
    )
    write_yaml(research / "evidence" / "EVI-0001.yaml", evidence_data())
    write_yaml(
        research / "claims" / "CLAIM-0001.yaml",
        claim_data(status="evidence_linked", supporting_evidence=["EVI-0001"]),
    )
    # Committed, because that is what a real project looks like and because an
    # uncommitted capsule has no HEAD for the repository-inspection action to
    # report on.
    commit_all(repo, "capsule")
    return repo


def make_config(dsn: str, artifacts_root: Path, **overrides: Any) -> RuntimeConfig:
    settings = RuntimeSettings(**overrides.pop("settings", {}))
    budget = BudgetDefaults(**overrides.pop("budget", {}))
    return RuntimeConfig(
        dsn=dsn,
        artifacts_root=artifacts_root,
        settings=settings,
        budget=budget,
        autonomy=overrides.pop("autonomy", "high"),
        source=None,
    )


def make_context(
    *,
    db: Database,
    repo: Path,
    artifacts_root: Path,
    dsn: str,
    models: Any,
    permitted: tuple[str, ...],
) -> CycleContext:
    store = RuntimeStore(db)
    return CycleContext(
        config=make_config(dsn, artifacts_root),
        db=db,
        store=store,
        queue=WorkQueue(db),
        ledger=InvocationLedger(db),
        budgets=BudgetLedger(db),
        artifacts=FilesystemArtifactStore(artifacts_root, store=store),
        kernel=ScientificKernelAdapter(repo),
        models=models,
        permitted_actions=permitted,
    )


def make_router(
    *,
    db: Database,
    artifacts_root: Path,
    run_id: str,
    project_id: str,
    answers: dict[str, dict[str, Any]],
    families: dict[str, str] | None = None,
    work_id: str | None = None,
) -> Any:
    """The *real* :class:`ModelRouter`, over fake provider adapters.

    Used where the property under test is provenance, routing or independence
    rather than graph control flow. :class:`ScriptedRouter` replaces the router
    and therefore records nothing, which is fine for asserting that a node ran
    and wrong for asserting that a call was written down.

    ``answers`` is keyed by the *automation* role the runtime role maps onto --
    ``planner``, ``reviewer``, ``analyst``, ``coder``, ``literature`` -- because
    that is what the v1 adapter contract takes.
    """

    from research_os.runtime.artifacts import FilesystemArtifactStore
    from research_os.runtime.budgets import BudgetLedger
    from research_os.runtime.routing import ModelRouter, ProviderProfile
    from tests.fake_providers import FakeProvider, ScriptedResponse

    family_for = families or {"fake": "fake-family"}
    adapters = {
        name: FakeProvider(
            name=name,
            family=family,
            responses={
                role: [ScriptedResponse(structured=payload)]
                for role, payload in answers.items()
            },
        )
        for name, family in family_for.items()
    }
    profiles = tuple(
        ProviderProfile(name=name, family=family, model=f"{name}-1", tier=3)
        for name, family in family_for.items()
    )
    store = RuntimeStore(db)
    return ModelRouter(
        adapters=adapters,
        profiles=profiles,
        store=store,
        artifacts=FilesystemArtifactStore(artifacts_root, store=store),
        budgets=BudgetLedger(db),
        run_id=run_id,
        project_id=project_id,
        work_id=work_id,
    )


def declare_experiment_command(
    *,
    project_id: str = "alpha-project",
    name: str = "demo-test",
    argv: tuple[str, ...] = ("true",),
    outputs: tuple[str, ...] = (),
) -> Path:
    """Write an ``experiments.yaml`` declaring one experiment command.

    The runtime will only run a command the *researcher* declared, so a test
    that exercises the experiment path has to declare one -- which is the point
    of the design rather than an inconvenience of the test.
    """

    import yaml

    from research_os.paths import config_home

    path = config_home() / "experiments.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {
                "schema_version": 1,
                "projects": {
                    project_id: {
                        "default_executor": "local",
                        "commands": {
                            name: {
                                "name": name,
                                "description": "a declared demo command",
                                "argv": list(argv),
                                "parameters": [],
                                "outputs": list(outputs),
                                "timeout_seconds": 60,
                                "executor": "local",
                            }
                        },
                    }
                },
            },
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return path
