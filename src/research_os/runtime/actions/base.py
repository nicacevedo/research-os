"""What every action handler returns, and the one distinction it must get right.

:class:`ActionOutcome` has both ``ok`` and ``failure_class``, and the
relationship between them is the thing to understand:

- ``ok=True`` means the action did what it was asked to do. That includes an
  experiment that ran correctly and **refuted** its hypothesis. The code worked,
  the data were valid, the answer was no. ``failure_class`` is ``None``.
- ``ok=False`` with a ``failure_class`` means something malfunctioned, and the
  class decides whether it is retried, repaired, rescheduled or escalated.

There is no third option, and that is deliberate: a handler cannot express "the
science came out negative so this failed", because
:class:`~research_os.runtime.failures.FailureClass` has no member for it. A
system that retried refutations until they stopped refuting would be a machine
for manufacturing positive results, and this is where that would have to start.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Protocol

from research_os.errors import ResearchOSError
from research_os.runtime.budgets import Dimension
from research_os.runtime.context import CycleContext
from research_os.runtime.failures import FailureClass
from research_os.runtime.interfaces import ArtifactRef
from research_os.runtime.models import ModelCallStatus

LOG = logging.getLogger("research_os.runtime.actions")


@dataclass(frozen=True, slots=True)
class ActionOutcome:
    """The result of one action.

    ``data`` is small and structured -- the thing the next node reasons about.
    Anything large is in ``artifacts`` as a reference, because ``data`` ends up
    in a checkpoint.
    """

    ok: bool
    detail: str = ""
    data: dict[str, Any] = field(default_factory=dict)
    artifacts: tuple[ArtifactRef, ...] = ()
    failure_class: FailureClass | None = None

    def __post_init__(self) -> None:
        if self.ok and self.failure_class is not None:
            raise ValueError(
                "an outcome that succeeded has no failure class. A scientifically "
                "negative result is a success; see actions/base.py."
            )
        if not self.ok and self.failure_class is None:
            raise ValueError(
                "an outcome that failed must name its failure class, so the retry "
                "policy has something to decide from"
            )

    @classmethod
    def succeeded(
        cls,
        detail: str,
        *,
        data: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ActionOutcome:
        return cls(ok=True, detail=detail, data=data or {}, artifacts=artifacts)

    @classmethod
    def failed(
        cls,
        detail: str,
        *,
        failure_class: FailureClass,
        data: dict[str, Any] | None = None,
        artifacts: tuple[ArtifactRef, ...] = (),
    ) -> ActionOutcome:
        return cls(
            ok=False,
            detail=detail,
            data=data or {},
            artifacts=artifacts,
            failure_class=failure_class,
        )


class ActionHandler(Protocol):
    """One action, performed once.

    Called from inside the idempotency ledger, so a handler does **not** need to
    be idempotent itself -- it needs to be *correct when called once*. The
    ledger is what guarantees it is.
    """

    def __call__(
        self,
        state: Mapping[str, Any],
        context: CycleContext,
        plan: Mapping[str, Any],
    ) -> ActionOutcome: ...


Handler = Callable[[Mapping[str, Any], CycleContext, Mapping[str, Any]], ActionOutcome]


def latest_artifact(
    context: CycleContext,
    *,
    role_prefix: str,
    project_id: str,
) -> str | None:
    """The most recent artifact of a kind, across every cycle of one project.

    Handlers used to look for their input in ``state["action_result"]``, which
    is always empty at the start of a cycle: one action runs per cycle, and a
    successor cycle is a new LangGraph thread seeded only with identity. So the
    citation audit and the results interpretation could never find a draft or a
    job and always returned "nothing to do" -- which meant the pipeline could
    write prose and never audit it.

    The durable record is the answer. Artifacts are linked to their run when
    produced, and runs belong to projects.
    """

    with context.db.tx() as conn:
        row = conn.execute(
            """
            select l.artifact_id
            from artifact_links l
            join research_runs r on r.run_id = l.run_id
            join artifacts a on a.artifact_id = l.artifact_id
            where r.project_id = %(project_id)s
              and a.role like %(prefix)s
            order by l.created_at desc
            limit 1
            """,
            {"project_id": project_id, "prefix": f"{role_prefix}%"},
        ).fetchone()
    return str(row["artifact_id"]) if row else None


def charge_delegated_spend(
    state: Mapping[str, Any],
    context: CycleContext,
    invocations: Any,
    *,
    action: str,
) -> None:
    """Record, after the fact, what a delegated v1 controller spent.

    The runtime's router reserves ``MODEL_CALLS`` and ``MODEL_COST_USD`` before
    every call it makes itself. It makes none of the calls in here: this handler
    delegates to a v1 controller which owns its own providers, bounds itself
    with its own per-action ceiling, and has never touched the runtime's ledger.

    So a cycle that made four model calls reported one, and a run started with
    ``--max-cost-usd 6`` reported a tenth of what it had spent. The budget was
    not wrong about what it had *reserved*; it was silent about what the run had
    *cost*, which is the number a researcher reads. Found by reading a real
    pilot's run report next to its provider invocations, not by a test --
    nothing asserted that the two agreed.

    This cannot refuse, because the calls already happened. What it does is make
    the ledger and the run report true, so the *next* reservation sees the
    spend: the cap bites on the following call rather than on the one that
    overran. `BudgetLedger.charge_all` says the same thing at more length.

    A provider that reports no cost is charged for the call and not for the
    money, and the model-call row records ``None`` rather than zero -- a spend
    recorded as zero because the number was unavailable is the accounting error
    that compounds.
    """

    records = list(invocations or ())
    if not records:
        return
    run_id = str(state["run_id"])
    project_id = str(state["project_id"])
    work_id = state.get("work_id")
    cost = sum(
        Decimal(str(item.total_cost_usd))
        for item in records
        if getattr(item, "total_cost_usd", None) is not None
    )
    for item in records:
        try:
            context.store.record_model_call(
                provider=str(getattr(item, "provider", "unknown")),
                role=str(getattr(item, "role", action)),
                status=(
                    ModelCallStatus.OK
                    if getattr(item, "ok", False)
                    else ModelCallStatus.FAILED
                ),
                run_id=run_id,
                work_id=work_id,
                model=getattr(item, "model", None),
                # Not the runtime's own prompt versions: these are v1 worker
                # prompts and saying otherwise would make `runtime run`'s
                # prompt column a guess.
                prompt_version=f"delegated:{action}",
                tokens_in=getattr(item, "input_tokens", None),
                tokens_out=getattr(item, "output_tokens", None),
                cost_usd=getattr(item, "total_cost_usd", None),
                latency_ms=getattr(item, "duration_ms", None),
                error=getattr(item, "error", None),
            )
        except ResearchOSError as exc:
            # Recording the spend is not worth failing the action over; the
            # budget charge below is the part that must happen.
            LOG.warning("could not record a delegated model call: %s", exc)
    context.budgets.charge_all(
        dimension=Dimension.MODEL_CALLS,
        amount=len(records),
        run_id=run_id,
        project_id=project_id,
        work_id=work_id,
    )
    if cost > 0:
        context.budgets.charge_all(
            dimension=Dimension.MODEL_COST_USD,
            amount=cost,
            run_id=run_id,
            project_id=project_id,
            work_id=work_id,
        )
