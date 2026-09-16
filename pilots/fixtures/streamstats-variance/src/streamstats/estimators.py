"""Three single-pass variance estimators.

All three are mathematically identical and numerically are not. The difference
is what happens to the significant digits when the mean is large relative to
the spread: `sum(x*x) - n*mean*mean` subtracts two nearly equal large numbers,
which is catastrophic cancellation, and the other two do not form that
difference at all.
"""

from __future__ import annotations

from collections.abc import Iterable


def naive_variance(values: Iterable[float]) -> float:
    """The textbook one-pass formula. Accurate only near zero.

    Kept because it is the estimator whose failure the others are measured
    against; nothing should call it for real work.
    """

    count = 0
    total = 0.0
    total_squares = 0.0
    for value in values:
        count += 1
        total += value
        total_squares += value * value
    if count < 2:
        return 0.0
    mean = total / count
    return (total_squares - count * mean * mean) / (count - 1)


def shifted_variance(values: Iterable[float], *, shift: float | None = None) -> float:
    """The same formula, on data shifted by the first observation.

    Shifting does not change a variance, and it moves the data near zero, where
    the cancellation in `naive_variance` is not catastrophic. `shift=None` uses
    the first value, which needs no prior knowledge of the distribution.
    """

    count = 0
    total = 0.0
    total_squares = 0.0
    origin = shift
    for value in values:
        if origin is None:
            origin = value
        centred = value - origin
        count += 1
        total += centred
        total_squares += centred * centred
    if count < 2:
        return 0.0
    mean = total / count
    return (total_squares - count * mean * mean) / (count - 1)


def welford_variance(values: Iterable[float]) -> float:
    """Welford's recurrence. The reference for accuracy here.

    Forms no sum of squares of the raw data, so there is no large difference to
    cancel. Slower per element than the other two -- one division per
    observation -- which is the trade the experiment in `experiments.yaml`
    exists to measure.
    """

    count = 0
    mean = 0.0
    second_moment = 0.0
    for value in values:
        count += 1
        delta = value - mean
        mean += delta / count
        second_moment += delta * (value - mean)
    if count < 2:
        return 0.0
    return second_moment / (count - 1)
