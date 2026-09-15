"""A capsule, a fake router and a context, for the graph tests.

The router here is a stand-in for provider adapters, not for the routing logic:
:mod:`tests.test_runtime_routing` exercises the real
:class:`~research_os.runtime.routing.ModelRouter` against fake adapters. What
these graph tests need is a model that answers predictably so that what is being
asserted is the graph's control flow rather than a model's mood.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from research_os.runtime.artifacts import FilesystemArtifactStore
from research_os.runtime.budgets import BudgetLedger
from research_os.runtime.config import BudgetDefaults, RuntimeConfig, RuntimeSettings
from research_os.runtime.context import CycleContext
from research_os.runtime.db import Database
from research_os.runtime.idempotency import InvocationLedger
from research_os.runtime.interfaces import Independence, ModelRequest, ModelResponse
from research_os.runtime.kernel import ScientificKernelAdapter
from research_os.runtime.queue import WorkQueue
from research_os.runtime.store import RuntimeStore
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
    """Answers each role from a script, and records what it was asked."""

    answers: dict[str, dict[str, Any]] = field(default_factory=dict)
    requests: list[ModelRequest] = field(default_factory=list)
    fail_roles: set[str] = field(default_factory=set)

    def complete(self, request: ModelRequest) -> ModelResponse:
        self.requests.append(request)
        role = str(request.role)
        if role in self.fail_roles:
            return ModelResponse(
                provider="scripted",
                model="scripted-1",
                error="scripted failure",
                independence=Independence.NONE,
            )
        structured = self.answers.get(role)
        return ModelResponse(
            provider="scripted",
            model="scripted-1",
            text=None,
            structured=structured,
            tokens_in=10,
            tokens_out=5,
            cost_usd=0.001,
            latency_ms=1,
            independence=Independence.DIFFERENT_CONTEXT,
            independence_note="scripted",
        )

    def requests_for(self, role: str) -> list[ModelRequest]:
        return [r for r in self.requests if str(r.role) == role]


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
