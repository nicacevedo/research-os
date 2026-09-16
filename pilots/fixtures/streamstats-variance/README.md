# streamstats-variance

Three single-pass variance estimators, and the question of which one to use.

`naive_variance` is the textbook `sum(x²) - n·mean²` formula. It loses every
significant digit when the data are far from zero, which is not a hypothetical:
sensor readings, timestamps and prices are all large numbers with small spreads.

`shifted_variance` subtracts the first observation before accumulating, which
costs one subtraction per element and moves the data to where the cancellation
is harmless. `welford_variance` uses Welford's recurrence and forms no sum of
squares at all, at the cost of a division per element.

The open question is whether shifting is enough — whether it matches Welford's
accuracy at Welford's cost or better — because if it is, the simpler estimator
is the one to use.

`scripts/bench_variance.py` measures both accuracy and time against
`statistics.variance` over the same values. It is declared as this project's
only experiment; see `experiments.yaml.example`.

## This is a pilot fixture

It was written to give the Research OS autonomous runtime a second, differently
shaped scientific frontier to act on: an actionable hypothesis with no
experiment recorded against it. The code and the measurements are real. It is
not an independent research programme, and it is not evidence that the runtime
produces good science on a capsule it did not anticipate.
