"""The failure taxonomy is complete, and says nothing about scientific results.

The second property is the one that matters most. An experiment that runs
correctly and refutes its hypothesis has succeeded; a system that classifies
that as a failure will retry it, and a system that retries a refutation until it
stops refuting has become a machine for manufacturing positive results. The
protection is structural: there is no member of :class:`FailureClass` that could
express it.
"""

from __future__ import annotations

import pytest

from research_os.runtime.failures import (
    POLICY,
    FailureClass,
    Response,
    is_retryable,
    requires_human,
    response_for,
    retry_delay_seconds,
)


def test_every_failure_class_has_a_decision() -> None:
    """Adding a class without deciding what to do about it is the bug this catches."""

    assert set(POLICY) == set(FailureClass)


def test_no_failure_class_describes_a_scientific_result() -> None:
    """Falsification is evidence. It must be unrepresentable here."""

    forbidden = (
        "falsif",
        "refut",
        "negative_result",
        "hypothesis_rejected",
        "null_result",
    )
    names = [member.value for member in FailureClass]
    for token in forbidden:
        assert not any(token in name for name in names), (
            f"{token!r} appears in the failure taxonomy; a scientific outcome is "
            f"not a malfunction and must never be routed through retry policy"
        )


@pytest.mark.parametrize(
    ("failure_class", "expected"),
    [
        (FailureClass.SLURM_PREEMPTED, Response.RETRY),
        (FailureClass.SLURM_NODE_FAILURE, Response.RETRY),
        (FailureClass.SLURM_OUT_OF_MEMORY, Response.RESCHEDULE_WITH_MORE_RESOURCES),
        (FailureClass.SLURM_TIMEOUT, Response.RESCHEDULE_WITH_MORE_RESOURCES),
        (FailureClass.DETERMINISTIC_CHECK_FAILED, Response.REPAIR),
        (FailureClass.MODEL_OUTPUT_INVALID, Response.RETRY),
        (FailureClass.MODEL_OUTPUT_INVALID_REPEATED, Response.FAIL_PERMANENTLY),
        (FailureClass.MISSING_SCIENTIFIC_AUTHORITY, Response.INTERRUPT_FOR_HUMAN),
        (FailureClass.BUDGET_EXHAUSTED, Response.FAIL_PERMANENTLY),
        (FailureClass.DERIVED_INDEX_CORRUPT, Response.REBUILD_DERIVED_STATE),
    ],
)
def test_the_response_for_a_class_is_the_documented_one(
    failure_class: FailureClass, expected: Response
) -> None:
    assert response_for(failure_class) is expected


def test_a_preempted_job_retries_but_an_out_of_memory_job_does_not_retry_unchanged() -> (
    None
):
    """Resubmitting the same allocation after an OOM just wastes the queue slot."""

    assert is_retryable(FailureClass.SLURM_PREEMPTED)
    assert is_retryable(FailureClass.SLURM_OUT_OF_MEMORY)  # with more resources
    assert response_for(FailureClass.SLURM_OUT_OF_MEMORY) is not Response.RETRY


def test_budget_exhaustion_does_not_retry() -> None:
    """A budget that retries is not a budget."""

    assert not is_retryable(FailureClass.BUDGET_EXHAUSTED)


def test_only_missing_authority_asks_for_a_human() -> None:
    asking = {c for c in FailureClass if requires_human(c)}
    assert asking == {FailureClass.MISSING_SCIENTIFIC_AUTHORITY}


def test_rate_limits_wait_longer_than_ordinary_hiccups() -> None:
    assert retry_delay_seconds(FailureClass.PROVIDER_RATE_LIMIT) > retry_delay_seconds(
        FailureClass.PROVIDER_TRANSIENT
    )


def test_the_delay_grows_with_the_attempt() -> None:
    first = retry_delay_seconds(FailureClass.PROVIDER_TRANSIENT, attempt=1)
    third = retry_delay_seconds(FailureClass.PROVIDER_TRANSIENT, attempt=3)
    assert third > first
