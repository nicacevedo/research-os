# State

Two estimators exist. `twopass.variance` is the reference. `welford.variance`
is named for Welford's method but does not yet implement it: it accumulates the
raw sum of squares, which is the form that cancels on offset data.

The test suite records the disagreement rather than hiding it. Nothing has been
measured across a range of offsets, and nothing is claimed.
