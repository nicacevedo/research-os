"""The failure taxonomy, and what each class earns.

The single most damaging thing an autonomous research system can do is mistake
a scientific result for a malfunction. An experiment that runs correctly and
refutes the hypothesis has *succeeded*: the code worked, the data were valid,
the answer was no. A system that retries it because it "failed" burns compute to
get the same answer, and -- far worse -- a system that retries it with different
parameters until it stops saying no has quietly become a machine for
manufacturing positive results.

So :data:`FailureClass` has no member for that case, and it never will.
Falsification is recorded through the scientific kernel as evidence, and never
reaches this module. The one thing this module could get catastrophically wrong
is a thing it is structurally unable to express.

Everything else is a genuine malfunction, and the classes exist because the
right response differs:

- a rate limit wants to wait, and waiting longer each time;
- a preempted Slurm job wants to be resubmitted, unchanged, immediately;
- a job that ran out of memory wants a person or a policy to give it more, not
  the same allocation again;
- a model that returned unparseable JSON wants *one* re-ask with the schema
  restated, then to be treated as broken;
- a deterministic test failure wants a repair attempt, not a retry -- running
  the same test again produces the same red;
- a missing scientific authority wants a human, and no amount of retrying will
  produce one.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from research_os.errors import ResearchOSError


class FailureClass(StrEnum):
    """Why something did not work.

    There is deliberately no ``SCIENTIFIC_FALSIFICATION`` member. See the module
    docstring: a refuted hypothesis is a result, is recorded as evidence, and
    must never be routed through retry policy.
    """

    # --- provider / model -------------------------------------------------
    PROVIDER_TRANSIENT = "provider_transient"
    PROVIDER_RATE_LIMIT = "provider_rate_limit"
    PROVIDER_UNAVAILABLE = "provider_unavailable"
    PROVIDER_TIMEOUT = "provider_timeout"
    MODEL_OUTPUT_INVALID = "model_output_invalid"
    MODEL_OUTPUT_INVALID_REPEATED = "model_output_invalid_repeated"

    # --- code / checks ----------------------------------------------------
    DETERMINISTIC_CHECK_FAILED = "deterministic_check_failed"
    CODE_EXCEPTION = "code_exception"

    # --- compute ----------------------------------------------------------
    SLURM_PREEMPTED = "slurm_preempted"
    SLURM_NODE_FAILURE = "slurm_node_failure"
    SLURM_OUT_OF_MEMORY = "slurm_out_of_memory"
    SLURM_TIMEOUT = "slurm_timeout"
    SCHEDULER_UNAVAILABLE = "scheduler_unavailable"
    EXECUTOR_FAILED = "executor_failed"

    # --- runtime / infrastructure -----------------------------------------
    DATABASE_TRANSIENT = "database_transient"
    WORKER_CRASH = "worker_crash"
    LEASE_LOST = "lease_lost"
    DERIVED_INDEX_CORRUPT = "derived_index_corrupt"
    GIT_CONFLICT = "git_conflict"
    ARTIFACT_MISSING = "artifact_missing"

    # --- policy / authority -----------------------------------------------
    BUDGET_EXHAUSTED = "budget_exhausted"
    MISSING_SCIENTIFIC_AUTHORITY = "missing_scientific_authority"
    CAPABILITY_DENIED = "capability_denied"
    POLICY_REFUSED = "policy_refused"

    # --- last resort ------------------------------------------------------
    UNKNOWN = "unknown"


class Response(StrEnum):
    """What the runtime does about a failure class."""

    RETRY = "retry"
    RETRY_AFTER_BACKOFF = "retry_after_backoff"
    REPAIR = "repair"
    RESCHEDULE_WITH_MORE_RESOURCES = "reschedule_with_more_resources"
    REBUILD_DERIVED_STATE = "rebuild_derived_state"
    INTERRUPT_FOR_HUMAN = "interrupt_for_human"
    FAIL_PERMANENTLY = "fail_permanently"


#: The policy table. Every class appears exactly once; the completeness test in
#: ``tests/test_runtime_failures.py`` fails if a new class is added without a
#: decision about what to do with it, which is the point.
POLICY: dict[FailureClass, Response] = {
    FailureClass.PROVIDER_TRANSIENT: Response.RETRY_AFTER_BACKOFF,
    FailureClass.PROVIDER_RATE_LIMIT: Response.RETRY_AFTER_BACKOFF,
    FailureClass.PROVIDER_UNAVAILABLE: Response.RETRY_AFTER_BACKOFF,
    FailureClass.PROVIDER_TIMEOUT: Response.RETRY_AFTER_BACKOFF,
    FailureClass.MODEL_OUTPUT_INVALID: Response.RETRY,
    FailureClass.MODEL_OUTPUT_INVALID_REPEATED: Response.FAIL_PERMANENTLY,
    FailureClass.DETERMINISTIC_CHECK_FAILED: Response.REPAIR,
    FailureClass.CODE_EXCEPTION: Response.REPAIR,
    FailureClass.SLURM_PREEMPTED: Response.RETRY,
    FailureClass.SLURM_NODE_FAILURE: Response.RETRY,
    FailureClass.SLURM_OUT_OF_MEMORY: Response.RESCHEDULE_WITH_MORE_RESOURCES,
    FailureClass.SLURM_TIMEOUT: Response.RESCHEDULE_WITH_MORE_RESOURCES,
    FailureClass.SCHEDULER_UNAVAILABLE: Response.RETRY_AFTER_BACKOFF,
    FailureClass.EXECUTOR_FAILED: Response.REPAIR,
    FailureClass.DATABASE_TRANSIENT: Response.RETRY_AFTER_BACKOFF,
    FailureClass.WORKER_CRASH: Response.RETRY,
    FailureClass.LEASE_LOST: Response.RETRY,
    FailureClass.DERIVED_INDEX_CORRUPT: Response.REBUILD_DERIVED_STATE,
    FailureClass.GIT_CONFLICT: Response.REPAIR,
    FailureClass.ARTIFACT_MISSING: Response.FAIL_PERMANENTLY,
    FailureClass.BUDGET_EXHAUSTED: Response.FAIL_PERMANENTLY,
    FailureClass.MISSING_SCIENTIFIC_AUTHORITY: Response.INTERRUPT_FOR_HUMAN,
    FailureClass.CAPABILITY_DENIED: Response.FAIL_PERMANENTLY,
    FailureClass.POLICY_REFUSED: Response.FAIL_PERMANENTLY,
    FailureClass.UNKNOWN: Response.FAIL_PERMANENTLY,
}

#: Responses that mean "put it back on the queue". Anything else is terminal for
#: the work item, whatever else it may trigger elsewhere.
_RETRYING: frozenset[Response] = frozenset(
    {
        Response.RETRY,
        Response.RETRY_AFTER_BACKOFF,
        Response.REPAIR,
        Response.RESCHEDULE_WITH_MORE_RESOURCES,
        Response.REBUILD_DERIVED_STATE,
    }
)

#: Base delay per class, in seconds. Multiplied by the attempt number by the
#: queue, which is linear rather than exponential on purpose: this is one
#: researcher's workstation talking to two or three providers, and an
#: exponential backoff that reaches an hour is a run that looks hung.
_BASE_DELAY: dict[FailureClass, float] = {
    FailureClass.PROVIDER_RATE_LIMIT: 60.0,
    FailureClass.PROVIDER_UNAVAILABLE: 30.0,
    FailureClass.PROVIDER_TRANSIENT: 10.0,
    FailureClass.PROVIDER_TIMEOUT: 15.0,
    FailureClass.SCHEDULER_UNAVAILABLE: 60.0,
    FailureClass.DATABASE_TRANSIENT: 5.0,
}
_DEFAULT_DELAY = 5.0


def response_for(failure_class: FailureClass) -> Response:
    return POLICY.get(failure_class, Response.FAIL_PERMANENTLY)


def is_retryable(failure_class: FailureClass) -> bool:
    return response_for(failure_class) in _RETRYING


def requires_human(failure_class: FailureClass) -> bool:
    return response_for(failure_class) is Response.INTERRUPT_FOR_HUMAN


def retry_delay_seconds(failure_class: FailureClass, *, attempt: int = 1) -> float:
    base = _BASE_DELAY.get(failure_class, _DEFAULT_DELAY)
    return base * max(1, attempt)


#: The classes that mean **the work did not run**, as opposed to "the work ran
#: and this is what it found".
#:
#: The distinction the live pilot of 2026-09-19 proved is load-bearing. A
#: planner call died on an OAuth refresh collision; the graph node caught the
#: unusable response, wrote it into ``plan_refusal``, and ``conclude`` mapped
#: any refusal to ``DONE_FOR_NOW``. Three research runs therefore finished as
#: ``SUCCEEDED / DONE_FOR_NOW`` and `researchctl runtime status` told the
#: researcher they had "finished cleanly" and that a scientific decision was
#: theirs to make. No scientific stage had executed at all.
#:
#: So this set exists to make one sentence checkable in code rather than
#: believed in prose:
#:
#:     a transient infrastructure failure must delay science, never impersonate
#:     it.
#:
#: Membership is decided by one question: *if work stopped for this reason, did
#: any scientific stage produce an answer?* For everything below, no -- the
#: provider never answered, the database blinked, the worker died, the lease
#: was lost, the scheduler was unreachable. Whatever the runtime says about the
#: science afterwards would be made up.
#:
#: Deliberately **excluded**, and each for a reason:
#:
#: - ``MODEL_OUTPUT_INVALID`` / ``..._REPEATED`` -- a model answered; the answer
#:   was unusable. That is a fact about the model, and the one re-ask the policy
#:   table grants it is a scientific retry, not an infrastructure wait.
#: - ``DETERMINISTIC_CHECK_FAILED`` and ``CODE_EXCEPTION`` -- the work ran. A
#:   red test is a result.
#: - the ``SLURM_*`` classes -- an experiment that was preempted or ran out of
#:   memory is a *job* that did not finish, handled by the external-job
#:   lifecycle and its own terminal state, not by pretending a cycle failed.
#: - ``BUDGET_EXHAUSTED``, ``MISSING_SCIENTIFIC_AUTHORITY``, ``CAPABILITY_DENIED``,
#:   ``POLICY_REFUSED`` -- policy answers. The system worked and said no.
#: - ``UNKNOWN`` -- the whole point of ``UNKNOWN`` is that nothing may be
#:   concluded from it, including that it was infrastructure.
INFRASTRUCTURE: frozenset[FailureClass] = frozenset(
    {
        FailureClass.PROVIDER_TRANSIENT,
        FailureClass.PROVIDER_RATE_LIMIT,
        FailureClass.PROVIDER_UNAVAILABLE,
        FailureClass.PROVIDER_TIMEOUT,
        FailureClass.SCHEDULER_UNAVAILABLE,
        FailureClass.DATABASE_TRANSIENT,
        FailureClass.WORKER_CRASH,
        FailureClass.LEASE_LOST,
    }
)

#: The subset of :data:`INFRASTRUCTURE` that is about a model provider, and so
#: the subset whose retry schedule must respect a provider cooldown. Separate
#: from the whole set because a database hiccup has no ``cooldown_until``.
PROVIDER_FAILURES: frozenset[FailureClass] = frozenset(
    {
        FailureClass.PROVIDER_TRANSIENT,
        FailureClass.PROVIDER_RATE_LIMIT,
        FailureClass.PROVIDER_UNAVAILABLE,
        FailureClass.PROVIDER_TIMEOUT,
    }
)


def is_infrastructure(failure_class: FailureClass) -> bool:
    """Whether this class means no scientific stage executed.

    Never a scientific conclusion, whatever else the runtime does about it.
    """

    return failure_class in INFRASTRUCTURE


class StageExecutionError(ResearchOSError):
    """A required stage of a cycle could not execute.

    Raised rather than returned, and that is the whole design. A node that
    *returns* an unusable model response hands the graph something to reason
    about, and the graph's vocabulary is scientific: every terminal state it
    can reach is a statement about the science. There is no way to say "no
    conclusion was reached because nothing ran" in that vocabulary, so the
    first build said ``DONE_FOR_NOW`` instead.

    An exception leaves the graph entirely. It reaches the work queue, which
    has exactly the right vocabulary -- a failure class, a retry policy and a
    schedule -- and the run stays un-concluded while the queue works on it.

    ``retry_at`` is when the blocking condition is known to clear, when that is
    known: a provider breaker records a ``cooldown_until``, and retrying before
    it is guaranteed to fail. ``attempted`` says whether a real invocation
    happened, which decides whether this costs an attempt: being turned away by
    an open breaker is not a failed try.
    """

    def __init__(
        self,
        message: str,
        *,
        failure_class: FailureClass,
        retry_at: datetime | None = None,
        attempted: bool = True,
    ) -> None:
        super().__init__(message)
        self.failure_class = failure_class
        self.retry_at = retry_at
        self.attempted = attempted
