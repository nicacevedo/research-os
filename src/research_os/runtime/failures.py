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

from enum import StrEnum


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
