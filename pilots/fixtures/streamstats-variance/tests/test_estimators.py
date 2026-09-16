"""What the three estimators must agree about, and where they must not.

The point of the suite is the second half. Two of these tests would pass for a
broken implementation of anything; the accuracy tests are the ones that fail if
somebody "simplifies" `shifted_variance` back into the naive formula.
"""

from __future__ import annotations

from streamstats import naive_variance, shifted_variance, welford_variance

SMALL = [2.0, 4.0, 4.0, 4.0, 5.0, 5.0, 7.0, 9.0]
#: The classic cancellation case: nine significant digits of offset, a variance
#: of 1. `naive_variance` returns something with no correct digits at all.
OFFSET = [1e9 + value for value in (1.0, 2.0, 3.0, 4.0, 5.0)]


def test_all_three_agree_on_well_scaled_data() -> None:
    expected = 4.571428571428571
    for estimator in (naive_variance, shifted_variance, welford_variance):
        assert abs(estimator(SMALL) - expected) < 1e-12


def test_fewer_than_two_observations_has_no_variance() -> None:
    for estimator in (naive_variance, shifted_variance, welford_variance):
        assert estimator([]) == 0.0
        assert estimator([3.0]) == 0.0


def test_the_shifted_estimator_survives_a_large_offset() -> None:
    """The property the package exists for."""

    assert abs(shifted_variance(OFFSET) - 2.5) < 1e-9


def test_welford_survives_a_large_offset() -> None:
    assert abs(welford_variance(OFFSET) - 2.5) < 1e-9


def test_the_naive_estimator_is_the_one_that_fails() -> None:
    """Recorded as a fact about the baseline, not as an aspiration."""

    assert abs(naive_variance(OFFSET) - 2.5) > 1e-3
