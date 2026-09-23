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


class _IndexedLiterature:
    """A :class:`runner.LiteratureSource` over the shared literature index.

    The portfolio declares its need for retrieval as a one-method protocol and
    takes it by injection, so that a deployment with no literature access is a
    *missing capability the gate reports* rather than a crash. That design is
    right and it had nothing plugged into it: the composition root passed
    ``literature=None``, every deep novelty audit failed with
    ``capability_denied``, and since ``novelty_or_literature`` is the only
    route that can execute on this build, **no idea could reach VALIDATED in
    production at all**.

    Found by the 2026-09-21 dogfood, one stage after the version-scoped dedup
    key unblocked the ladder far enough to attempt an audit. It is the same
    shape as the twelve unroutable roles: a capability the system has, wired
    to nothing, behind a seam no test crossed because every test injects its
    own double.

    The store is opened per search and closed again. Searches are occasional,
    SQLite open is cheap, and holding a connection across a stage that makes
    provider calls would keep a file handle open for minutes to save
    microseconds.
    """

    def search(self, query: str, *, limit: int = 12) -> Any:
        from research_os.literature.packet import build_packet
        from research_os.literature.search import SearchOptions
        from research_os.literature.search import search as lit_search
        from research_os.literature.store import open_store

        with open_store() as store:
            results = lit_search(store, query, SearchOptions(limit=limit))
        return build_packet(query, results, max_works=limit)


def _literature() -> Any | None:
    """The shared index, or ``None`` when this deployment has none.

    ``None`` is a supported answer and must stay one. An index that does not
    exist yet, or cannot be opened, has to leave the audit reporting
    ``capability_denied`` -- which says "novelty cannot be established here"
    and stops the idea below VALIDATED -- rather than failing the stage with a
    stack trace or, far worse, letting novelty rest on model recall.
    """

    from research_os.literature.store import database_path

    try:
        if not database_path().exists():
            LOG.info("no literature index; deep novelty audits will report that")
            return None
    except OSError:
        return None
    return _IndexedLiterature()


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
        # A *factory*, not a provider: `advance_idea` opens its own run and
        # builds the router against it, so every call is attributed to the run
        # that made it.
        models=lambda run_id: context.models(
            run_id, context.item.project_id, context.item.work_id
        ),
        repo_path=context.repo_path,
        literature=_literature(),
        executors=_executors(context),
        work_id=context.item.work_id,
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


def _executors(context: WorkContext) -> dict[str, Any]:
    """What this machine can actually run an experiment on, for this project.

    The runtime's own builder, unchanged: it reads the project's
    ``experiments.yaml``, probes for a scheduler, and picks the containment
    mode -- ``required`` at high autonomy, because nobody is watching, and
    the researcher's configured mode otherwise. Reusing it is the point.
    Computing an executor set here would be a second answer to "can this host
    run something", and the two would eventually differ.

    ``can_execute=False`` was hard-coded in this function for two releases,
    so an empirical idea reported that the host could not measure anything on
    a host that could. Same shape as the literature source that was wired to
    ``None``: a capability the system has, behind a seam no test crossed.
    """

    from research_os.runtime.executors import build_executors
    from research_os.sandbox import SandboxMode

    return dict(
        build_executors(
            context.config,
            project_id=context.item.project_id,
            autonomy=context.config.autonomy,
            # Contained or it does not run, at every autonomy setting.
            # `build_executors` otherwise gives REQUIRED only at `high`, on
            # the stated grounds that at lower settings "a person is at the
            # keyboard". That holds for an objective cycle, which a person
            # starts. It does not hold here: the portfolio daemon runs
            # unattended by construction -- that is what it is for -- so the
            # dial's premise is false for this caller, and taking the
            # researcher's configured mode would mean a researcher lowering
            # autonomy to be more careful got model-parameterised commands
            # running uncontained with the full environment.
            sandbox_mode=SandboxMode.REQUIRED,
        )
    )


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


def run_follow_up(context: WorkContext) -> dict[str, Any]:
    """One frontier request turned into the new ideas it raises."""

    from research_os.portfolio import frontier
    from research_os.runtime.artifacts import FilesystemArtifactStore

    request_id = str(context.item.payload.get("request_id") or "")
    if not request_id:
        raise ValueError(f"{context.item.work_id}: a follow-up with no request")
    store = PortfolioStore(context.db)
    runtime = RuntimeStore(context.db)
    run = runtime.create_run(
        project_id=context.item.project_id,
        objective=f"portfolio-follow-up:{request_id}",
        autonomy=Autonomy(context.config.autonomy),
        run_kind=RunKind.IDEA_TRACK,
    )
    runtime.set_run_status(run.run_id, RunStatus.RUNNING)
    result = frontier.run_follow_up(
        frontier.FrontierContext(
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
        ),
        request_id,
    )
    runtime.set_run_status(
        run.run_id,
        RunStatus.SUCCEEDED if result.ok else RunStatus.FAILED,
        terminal_state=(
            TerminalState.DONE_FOR_NOW
            if result.ok
            else TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY
        ),
        detail=result.detail[:500],
    )
    if not result.ok and result.failure_class is not None:
        raise _as_error(result.failure_class, result.detail)
    return {
        "request_id": request_id,
        "created": list(result.created),
        "converged": list(result.converged),
        "detail": result.detail,
        "cost_usd": str(result.cost_usd),
    }


class _ProviderRetriever:
    """Discovery through the providers, into the shared index.

    `LiteratureService.retrieve` -- the existing A0 retrieval the objective
    cycle already uses -- behind the one-method protocol
    `litintel.LiteratureRetriever` declares, so the portfolio never imports a
    provider adapter and a deployment without one reports a capability
    rather than crashing.
    """

    def retrieve(self, query: str) -> dict[str, Any]:
        from research_os.literature.config import load_config as load_literature
        from research_os.literature.service import LiteratureService
        from research_os.literature.store import LiteratureStore

        store = LiteratureStore.open()
        try:
            report = LiteratureService(store=store, config=load_literature()).retrieve(
                query, limit=12
            )
        finally:
            store.close()
        return {
            "ingested": len(getattr(report, "keys", ()) or ()),
            "reached": list(getattr(report, "reached", ())),
        }


def _retriever() -> Any | None:
    """Provider discovery when literature is configured here, else nothing."""

    try:
        from research_os.literature.config import load_config as load_literature

        config = load_literature()
    except Exception:  # noqa: BLE001 - absent configuration is an answer
        return None
    if not any(
        getattr(item, "enabled", False)
        for item in getattr(config, "sources", {}).values()
    ):
        return None
    return _ProviderRetriever()


def run_literature_request(context: WorkContext) -> dict[str, Any]:
    """One idea's question answered from retrieved, verified sources."""

    from research_os.portfolio import frontier, litintel
    from research_os.runtime.artifacts import FilesystemArtifactStore

    request_id = str(context.item.payload.get("request_id") or "")
    if not request_id:
        raise ValueError(f"{context.item.work_id}: a literature request with no id")
    store = PortfolioStore(context.db)
    runtime = RuntimeStore(context.db)
    run = runtime.create_run(
        project_id=context.item.project_id,
        objective=f"portfolio-literature:{request_id}",
        autonomy=Autonomy(context.config.autonomy),
        run_kind=RunKind.IDEA_TRACK,
    )
    runtime.set_run_status(run.run_id, RunStatus.RUNNING)
    result = litintel.answer_request(
        frontier.FrontierContext(
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
        ),
        request_id,
        literature=_literature(),
        retriever=_retriever(),
    )
    runtime.set_run_status(
        run.run_id,
        RunStatus.SUCCEEDED if result.ok else RunStatus.FAILED,
        terminal_state=(
            TerminalState.DONE_FOR_NOW
            if result.ok
            else TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY
        ),
        detail=result.detail[:500],
    )
    if not result.ok and result.failure_class is not None:
        raise _as_error(result.failure_class, result.detail)
    return {
        "request_id": request_id,
        "claims": list(result.claims),
        "evidence": list(result.evidence),
        "raised": list(result.raised),
        "detail": result.detail,
    }


def run_literature_watch(context: WorkContext) -> dict[str, Any]:
    """A scheduled pass: targeted questions for the liveliest ideas. No model."""

    from datetime import UTC, datetime

    from research_os.portfolio import litintel

    raised = litintel.watch(
        PortfolioStore(context.db),
        project_id=context.item.project_id,
        bucket=datetime.now(UTC).strftime("%Y%m%d"),
    )
    return {"raised": raised}


def run_synthesize(context: WorkContext) -> dict[str, Any]:
    """Write and referee one synthesis of the reviewed evidence."""

    from research_os.portfolio import frontier, synthesis
    from research_os.runtime.artifacts import FilesystemArtifactStore

    store = PortfolioStore(context.db)
    runtime = RuntimeStore(context.db)
    run = runtime.create_run(
        project_id=context.item.project_id,
        objective="portfolio-synthesize",
        autonomy=Autonomy(context.config.autonomy),
        run_kind=RunKind.IDEA_TRACK,
    )
    runtime.set_run_status(run.run_id, RunStatus.RUNNING)
    result = synthesis.synthesize(
        frontier.FrontierContext(
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
        )
    )
    runtime.set_run_status(
        run.run_id,
        RunStatus.SUCCEEDED if result.ok else RunStatus.FAILED,
        terminal_state=(
            TerminalState.DONE_FOR_NOW
            if result.ok
            else TerminalState.WAITING_FOR_EXTERNAL_DEPENDENCY
        ),
        detail=result.detail[:500],
    )
    if not result.ok and result.failure_class is not None:
        raise _as_error(result.failure_class, result.detail)
    return {
        "synthesis_id": result.synthesis_id,
        "raised": list(result.raised),
        "detail": result.detail,
    }


def run_curate(context: WorkContext) -> dict[str, Any]:
    """Write the bank to the project's autonomous branch."""

    from research_os.portfolio.curator import UnexpectedBankTipError, curate

    if context.repo_path is None:
        return {"skipped": "this project has no resolvable repository"}
    try:
        result = curate(
            db=context.db,
            project_id=context.item.project_id,
            repository=Path(context.repo_path),
            artifacts_root=context.config.artifacts_root,
        )
    except UnexpectedBankTipError as exc:
        # Recorded and *not* retried. This is the compensating control for the
        # reserved ref namespace the coding pipeline's escape check ignores, so
        # it is the one refusal in this layer a person has to see -- and
        # letting it fail three times and disappear into a work item's last
        # error is not seeing it.
        context.store.record_event(
            project_id=context.item.project_id,
            kind="PORTFOLIO_BANK_TIP_UNEXPECTED",
            payload={
                "work_id": context.item.work_id,
                "repository": str(context.repo_path),
                "detail": str(exc),
            },
        )
        LOG.error("%s", exc)
        return {"refused": True, "detail": str(exc)}
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

    **Everything else states its class too**, and until 2026-09-22 it did
    not: a bare `ResearchOSError` carries no class, `Daemon._classify` falls
    through its type table to `UNKNOWN`, and so every portfolio stage
    failure in the operational record read `unknown`. Twenty-seven of them
    did. That cost three things at once -- `portfolio status` could not say
    *why* anything failed, the retry policy could not tell a provider outage
    from a policy answer, and the allocator could not tell a refusal that
    will be identical next time from a transient worth retrying.

    `StageExecutionError` exists for exactly this and `_classify` believes a
    declared class before consulting its table.
    """

    from research_os.runtime.failures import StageExecutionError
    from research_os.runtime.routing import ProviderCallFailedError

    if failure_class in {
        FailureClass.PROVIDER_UNAVAILABLE,
        FailureClass.PROVIDER_TIMEOUT,
    }:
        return ProviderCallFailedError(
            detail, failure_class=failure_class, attempted=True
        )
    return StageExecutionError(detail, failure_class=failure_class)


def register() -> None:
    """Register every portfolio work kind. Idempotent."""

    register_work(PORTFOLIO_TICK, run_tick, from_event=TICK_EVENT)
    register_work(allocation.ADVANCE_IDEA, run_advance_idea)
    register_work(allocation.EXPLORE, run_explore)
    register_work(allocation.FOLLOW_UP, run_follow_up)
    register_work(allocation.LITERATURE_REQUEST, run_literature_request)
    register_work(
        "portfolio_literature_watch",
        run_literature_watch,
        from_event="LITERATURE_WATCH_DUE",
    )
    register_work(allocation.SYNTHESIZE, run_synthesize)
    register_work(allocation.CURATE, run_curate)
    register_work(allocation.DIGEST, run_digest)


register()

__all__ = ["PORTFOLIO_TICK", "TICK_EVENT", "register"]
