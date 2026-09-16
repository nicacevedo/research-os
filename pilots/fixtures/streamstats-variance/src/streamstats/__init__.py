"""Streaming variance estimators.

Three ways to compute a variance in one pass, which do not agree in floating
point. The question this package exists to answer is which one stays accurate
when the data are far from zero, because that is where the textbook
sum-of-squares estimator loses all its significant digits.
"""

from streamstats.estimators import (
    naive_variance,
    shifted_variance,
    welford_variance,
)

__all__ = ["naive_variance", "shifted_variance", "welford_variance"]
