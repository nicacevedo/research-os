"""Registering the portfolio's work with the control plane.

Importing this module is what makes ``researchd`` able to run a portfolio. The
daemon never imports it; the two composition roots do -- ``research_os.cli``
and ``research_os.service`` -- which is the whole mechanism keeping the runtime
free of a dependency on the layer above it.

Four work kinds, and the split between them is the architecture in one place:

```text
portfolio_tick          deterministic, no model, decides what to buy
portfolio_advance_idea  one bounded stage of one idea
portfolio_explore       one explorer call, producing candidates
portfolio_curate        the Curator, writing the bank to Git
portfolio_digest        a deterministic rendering of what happened
```

Only two of them spend anything, and neither of them decides what to spend on.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

# `track` and `runner` are imported inside their handlers, not here. Both
# reach LangGraph and the provider adapters, and importing this module must
# stay as cheap as importing `research_os.cli` -- which promises that a
# kernel-only install works with two dependencies, and which
# `tests/test_runtime_layering.py` checks by starting a subprocess.
from research_os.portfolio import allocation
from research_os.portfolio.config import PortfolioConfig, load_config
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.extensions import WorkContext, register_work
from research_os.runtime.failures import FailureClass
from research_os.runtime.models import Autonomy, RunKind, RunStatus, TerminalState
from research_os.runtime.store import RuntimeStore

LOG = logging.getLogger("research_os.portfolio.extensions")

PORTFOLIO_TICK = "portfolio_tick"
TICK_EVENT = "PORTFOLIO_TICK_DUE"


def _config(context: WorkContext) -> PortfolioConfig:
    return load_config()


def run_tick(context: WorkContext) -> dict[str, Any]:
    """One deterministic pass over one project's portfolio."""

    from research_os.portfolio import tick

    report = tick.tick(
        db=context.db,
        project_id=context.item.project_id,
        runtime_config=context.config,
        portfolio_config=_config(context),
    )
    return report.payload()


def run_advance_idea(context: WorkContext) -> dict[str, Any]:
    """Advance one idea by one stage.

    The stage the allocator chose is carried in the payload and is *not* used
    to dispatch: ``advance_idea`` selects the stage itself, deterministically,
    from the idea's current state. The payload's copy is provenance -- what the
    allocator believed when it bought this work -- and the two differing is
    information rather than a conflict, because state moves between a tick and
    the work item it enqueued.
    """

    idea_id = str(context.item.payload.get("idea_id") or "")
    if not idea_id:
        raise ValueError(f"{context.item.work_id}: advance_idea work with no idea")
    from research_os.portfolio import track

    result = track.advance_idea(
        runtime_config=context.config,
        portfolio_config=_config(context),
        db=context.db,
        project_id=context.item.project_id,
        idea_id=idea_id,
        models=context.models(
            _run_for(context, idea_id), context.item.project_id, context.item.work_id
        ),
        repo_path=context.repo_path,
        literature=None,
        can_execute=False,
    )
    payload = {
        "idea_id": result.idea_id,
        "stage": str(result.stage) if result.stage else None,
        "planned_stage": context.item.payload.get("stage"),
        "reason": result.reason,
        "ok": result.ok,
        "detail": result.detail,
        "disposition": str(result.disposition) if result.disposition else None,
        "cost_usd": str(result.cost_usd),
        "model_calls": result.model_calls,
    }
    if not result.ok and result.failure_class is not None:
        # Raised rather than returned, so the queue's retry policy sees it. A
        # handler that returned a failure as a successful result would take the
        # stage out of the retry machinery entirely -- and a provider outage
        # would become a permanent stop with no record of why.
        raise _as_error(result.failure_class, result.detail)
    return payload


def run_explore(context: WorkContext) -> dict[str, Any]:
    """One explorer call, producing candidate directions."""

    from research_os.portfolio import runner

    explorer = str(context.item.payload.get("explorer") or "blind_explorer")
    if explorer not in runner.EXPLORERS:
        raise ValueError(f"no such explorer: {explorer!r}")
    store = PortfolioStore(context.db)
    runtime = RuntimeStore(context.db)
    run = runtime.create_run(
        project_id=context.item.project_id,
        objective=f"portfolio-explore:{explorer}",
        autonomy=Autonomy(context.config.autonomy),
        run_kind=RunKind.IDEA_TRACK,
    )
    from research_os.runtime.artifacts import FilesystemArtifactStore

    runtime.set_run_status(run.run_id, RunStatus.RUNNING)
    outcome = runner.run_explorer(
        runner.ExplorerContext(
            config=_config(context),
            portfolio=store,
            runtime=runtime,
            models=context.models(
                run.run_id, context.item.project_id, context.item.work_id
            ),
            artifacts=FilesystemArtifactStore(
                context.config.artifacts_root, store=runtime
            ),
            project_id=context.item.project_id,
            run_id=run.run_id,
            charter=_charter(context.repo_path),
        ),
        explorer,
    )
    runtime.set_run_status(
        run.run_id,
        RunStatus.SUCCEEDED if outcome.ok else RunStatus.FAILED,
        terminal_state=(
            TerminalState.DONE_FOR_NOW
            if outcome.ok
            else TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY
        ),
        detail=outcome.detail[:500],
    )
    created = list(outcome.data.get("created", []))
    for seed in store.pending_seeds(project_id=context.item.project_id):
        if explorer == "seeded_explorer" and created:
            store.consume_seed(seed_id=seed.seed_id, consumed_by=run.run_id)
    if not outcome.ok and outcome.failure_class is not None:
        raise _as_error(outcome.failure_class, outcome.detail)
    return {
        "explorer": explorer,
        "created": created,
        "duplicates": outcome.data.get("duplicates", 0),
        "detail": outcome.detail,
        "cost_usd": str(outcome.cost_usd),
    }


def run_curate(context: WorkContext) -> dict[str, Any]:
    """Write the bank to the project's autonomous branch."""

    from research_os.portfolio.curator import curate

    if context.repo_path is None:
        return {"skipped": "this project has no resolvable repository"}
    result = curate(
        db=context.db,
        project_id=context.item.project_id,
        repository=Path(context.repo_path),
        artifacts_root=context.config.artifacts_root,
    )
    return result.payload()


def run_digest(context: WorkContext) -> dict[str, Any]:
    """Render what happened, deterministically."""

    from research_os.portfolio.digest import produce

    record = produce(
        db=context.db,
        project_id=context.item.project_id,
        config=_config(context),
    )
    return {"digest_id": record.digest_id, "counts": record.payload.get("counts", {})}


def _run_for(context: WorkContext, idea_id: str) -> str:
    """A placeholder run id for the router's provenance before a run exists.

    ``advance_idea`` opens its own run and builds its own router; this one is
    only used to satisfy the factory's signature for the *probe* it makes
    first. It never records a call, because the probe asks the selector and not
    a model.
    """

    return f"pending:{idea_id}"


def _charter(repo_path: Path | None) -> str:
    if repo_path is None:
        return ""
    charter = Path(repo_path) / ".research" / "CHARTER.md"
    try:
        return charter.read_text(encoding="utf-8")[:8_000]
    except OSError:
        return ""


def _as_error(failure_class: FailureClass, detail: str) -> Exception:
    """Turn a stage failure into the exception the queue classifies.

    The mapping is the runtime's own: a provider failure raises what the router
    raises, so the queue schedules against the breaker's cooldown rather than
    against a stopwatch, which is `docs/RUNTIME.md` §17 I5.
    """

    from research_os.errors import ResearchOSError
    from research_os.runtime.routing import ProviderCallFailedError

    if failure_class in {
        FailureClass.PROVIDER_UNAVAILABLE,
        FailureClass.PROVIDER_TIMEOUT,
    }:
        return ProviderCallFailedError(
            detail, failure_class=failure_class, attempted=True
        )
    return ResearchOSError(detail)


def register() -> None:
    """Register every portfolio work kind. Idempotent."""

    register_work(PORTFOLIO_TICK, run_tick, from_event=TICK_EVENT)
    register_work(allocation.ADVANCE_IDEA, run_advance_idea)
    register_work(allocation.EXPLORE, run_explore)
    register_work(allocation.CURATE, run_curate)
    register_work(allocation.DIGEST, run_digest)


register()

__all__ = ["PORTFOLIO_TICK", "TICK_EVENT", "register"]
