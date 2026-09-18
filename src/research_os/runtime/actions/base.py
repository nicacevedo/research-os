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
from collections.abc import Callable, Mapping, Sequence
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


#: The key under which a handler puts its bounded excerpt in ``ActionOutcome.data``.
#:
#: ``data`` rather than a new field on :class:`ActionOutcome`, and that is the
#: whole design. ``data`` is already the handler's structured result, already
#: checkpointed, already carried through the idempotency ledger, and already the
#: thing :func:`research_os.runtime.graphs.cycle._record_finding` reads to build
#: a finding's references. An excerpt is one more piece of that same result. A
#: parallel channel would have needed its own persistence, its own replay story
#: and its own bound, to carry something the existing one carries for free.
EXCERPT_KEY = "finding_excerpt"

#: The key under which a handler puts the *semantic identity* of its result in
#: ``ActionOutcome.data``.
#:
#: Beside :data:`EXCERPT_KEY`, in ``data``, for the same reasons that field
#: gives: already structured, already checkpointed, already carried through the
#: idempotency ledger, already what
#: :func:`research_os.runtime.graphs.cycle._record_finding` reads.
#:
#: **What it is for.** A finding is deduplicated by a digest over its content,
#: and for most handlers that is exactly right: the content *is* the
#: observation. For a handler whose result contains a model's prose, it is not.
#: An assessment of the research frontier reached over identical scientific
#: state, concluding the identical thing, mints a new citable finding on every
#: cycle -- because the rationale is phrased differently, and because the
#: artifact holding that rationale hashes differently. Twenty repetitions
#: consume all twelve slots of the planner's bounded finding window, and
#: operational repetition has become what looks like scientific progress.
#:
#: So a handler that knows which part of its result is the *observation* may
#: say so, in one deterministic string, and identity is computed from that
#: instead. Producer-authored for the same reason the excerpt is: only the
#: handler knows which fields carry the substance and which carry the wording.
#:
#: **The obligation on a handler that sets it.** Two results with equal keys
#: are the same finding, permanently and citably. So the key must contain
#: everything a reader would consider material -- the state assessed, the
#: conclusion reached, the targets named -- and nothing that varies without
#: the observation varying: no timestamp, no run id, no cycle index, no work
#: id, no artifact id, no free prose.
SEMANTIC_KEY = "finding_semantic_key"


def bounded_excerpt(entries: Sequence[str], *, limit: int) -> str:
    """Join a handler's chosen lines into one bounded, deterministic excerpt.

    **Deterministic, given the same result.** No timestamps, no ids, no
    iteration over anything unordered: the same structured result yields the
    same string, so a replay that re-derives an excerpt from a ledger-preserved
    result re-derives *that* excerpt. The ledger is what makes a replay reuse
    the result at all; this is what makes the derivation from it stable.

    **Truncation is visible.** A clipped excerpt ends with a marker naming what
    was dropped, because an excerpt that silently stops mid-sentence reads like
    the finding stopped mid-sentence -- and a reader who cannot tell the
    difference will attribute the handler's completeness to the bound.

    The caller chooses the lines. That is the point of producer authorship:
    this function knows how to bound text and nothing about what matters in it.
    """

    lines = [" ".join(str(entry).split()) for entry in entries]
    lines = [line for line in lines if line]

    kept: list[str] = []
    used = 0
    for index, line in enumerate(lines):
        remaining = len(lines) - index
        marker = _omission_marker(remaining)
        if len(marker) + 1 > limit:
            # A limit too small to hold even the notice. Degenerate, and it
            # still must not silently look complete.
            marker = "[...]"
        # Room for this line *and* for the marker that would be needed if it
        # were the last one to fit. Reserving it up front is what keeps the
        # marker from being the thing the final clip removes -- an excerpt that
        # loses its own truncation notice reads as complete.
        if used + len(line) + 1 + len(marker) + 1 > limit and kept:
            kept.append(marker)
            break
        if used + len(line) + 1 > limit:
            kept.append(line[: max(0, limit - used - 1)])
            break
        kept.append(line)
        used += len(line) + 1
    return "\n".join(kept)[:limit]


def _omission_marker(count: int) -> str:
    """What a clipped excerpt says about what it dropped."""

    noun = "entry" if count == 1 else "entries"
    return f"[... {count} further {noun} omitted to stay within the excerpt bound]"


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

    The durable record is the answer. Artifacts carry the project they were
    produced for, on the link itself since ``0015``, with the run left-joined
    for links written before that. The project has to be reachable without the
    run, because a run is a unit of work that retention may prune and an
    artifact is not.
    """

    with context.db.tx() as conn:
        row = conn.execute(
            """
            select l.artifact_id
            from artifact_links l
            left join research_runs r on r.run_id = l.run_id
            join artifacts a on a.artifact_id = l.artifact_id
            where coalesce(l.project_id, r.project_id) = %(project_id)s
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
    authority: Any = None,
) -> None:
    """Write the provenance for a delegated controller's calls, and reconcile.

    Two jobs, and they became different jobs when
    :mod:`research_os.runtime.spend` arrived.

    **The provenance rows are this function's, always.** One ``model_calls``
    row per invocation, with the provider, the model, the tokens, the latency
    and the cost. The budget authority cannot write them: it sees an
    ``InvocationResult`` and not the ``ModelInvocation`` the controller builds
    from it, and the richer record is the one worth keeping.

    **The budget charge is now a reconciliation, not the mechanism.** Before
    the authority existed this was the whole of delegated accounting: record
    the spend afterwards, past the limit when it must. That made the ledger
    true and it was not a budget -- a cap that bites on the call *after* the
    overrun is a cap that authorised the overrun. Each delegated call now
    reserves before it happens and settles after, so by the time this runs the
    money is usually already accounted.

    What is left for it is the difference: calls the authority never saw. That
    is not a hypothetical residue -- a controller that invokes a provider it
    did not get from the wrapped registry, or a path that raises before
    ``settle``, produces exactly it -- and charging the difference rather than
    the total is what stops the two mechanisms double-counting. With no
    ``authority`` the difference is the total, which is the old behaviour and
    the right one for a caller that has no runtime budget.

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
    # Only what the authority did not already reserve and settle. `max(0, ...)`
    # rather than an assertion: an authority that settled *more* than the run
    # records -- a call whose invocation the controller failed to persist -- is
    # a reporting gap, and turning it into a negative charge would hand the run
    # back capacity it really spent.
    settled_calls = int(getattr(authority, "settled_calls", 0) or 0)
    settled_usd = Decimal(str(getattr(authority, "settled_usd", 0) or 0))
    uncharged_calls = max(0, len(records) - settled_calls)
    uncharged_cost = max(Decimal(0), cost - settled_usd)
    if uncharged_calls:
        context.budgets.charge_all(
            dimension=Dimension.MODEL_CALLS,
            amount=uncharged_calls,
            run_id=run_id,
            project_id=project_id,
            work_id=work_id,
        )
    if uncharged_cost > 0:
        context.budgets.charge_all(
            dimension=Dimension.MODEL_COST_USD,
            amount=uncharged_cost,
            run_id=run_id,
            project_id=project_id,
            work_id=work_id,
        )
