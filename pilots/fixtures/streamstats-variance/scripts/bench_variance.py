"""Measure accuracy and cost of the three estimators, and print JSON.

Declared in `experiments.yaml` as the project's only experiment. Deterministic:
the generator is seeded from the command line, so the same parameters produce
the same numbers and a spec digest means something.

Prints one JSON object to stdout and nothing else, so an automated reader does
not have to parse prose.
"""

from __future__ import annotations

import argparse
import json
import random
import statistics
import sys
import time
from pathlib import Path

# So the script runs from a checkout with nothing installed, which is what an
# experiment executor hands it: a copy of the repository, a working directory,
# and `python3`. Prepending rather than appending, so a stale installed copy of
# this package cannot shadow the code being measured.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

# Imported after the path is set, deliberately.
from streamstats import (
    naive_variance,
    shifted_variance,
    welford_variance,
)

ESTIMATORS = {
    "naive": naive_variance,
    "shifted": shifted_variance,
    "welford": welford_variance,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=100_000)
    parser.add_argument("--offset", type=float, default=0.0)
    parser.add_argument("--sigma", type=float, default=1.0)
    parser.add_argument("--seed", type=int, default=0)
    parser.add_argument("--repeats", type=int, default=3)
    args = parser.parse_args()

    rng = random.Random(args.seed)
    values = [args.offset + rng.gauss(0.0, args.sigma) for _ in range(args.n)]
    # The reference is the two-pass computation in the standard library, in
    # the same floating-point type. Not an analytic truth -- the sample
    # variance of *these* numbers, which is what every estimator here is
    # trying to compute.
    reference = statistics.variance(values)

    results = {}
    for name, estimator in ESTIMATORS.items():
        timings = []
        estimate = 0.0
        for _ in range(args.repeats):
            started = time.perf_counter()
            estimate = estimator(values)
            timings.append(time.perf_counter() - started)
        results[name] = {
            "estimate": estimate,
            "relative_error": abs(estimate - reference) / reference,
            "seconds_median": statistics.median(timings),
        }

    print(
        json.dumps(
            {
                "parameters": {
                    "n": args.n,
                    "offset": args.offset,
                    "sigma": args.sigma,
                    "seed": args.seed,
                    "repeats": args.repeats,
                },
                "reference_variance": reference,
                "estimators": results,
            },
            indent=2,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
