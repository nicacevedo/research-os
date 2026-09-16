# Pilot fixtures

Projects that exist so the autonomous loop has something to act on, vendored
here rather than left in a scratchpad so a pilot is reproducible from this
repository.

## What a fixture is, and is not

A fixture is **real code with real measurements and a real capsule**. It is
**not** a research programme, and a pilot against one is not evidence that the
runtime produces good science on a capsule it did not anticipate — the same hand
wrote the runtime and the hypothesis, and the hypothesis was written to be
testable by a command that exists.

The reason to have one at all is the *frontier shape*. A capsule with one
contested claim and one open question reaches `propose_capsule_change` and
nothing else; a capsule with an actionable, untested hypothesis reaches
`critique_hypothesis` and `design_experiment`. Two shapes exercise two halves of
the graph, and the second shape did not exist on this machine.

## `streamstats-variance`

Three single-pass variance estimators, and the question of whether shifting the
data by the first observation is enough to fix the textbook formula's
catastrophic cancellation, or whether Welford's recurrence is needed.

- **Frontier:** `Q-0001` open, `HYP-0001` draft and addressing it, no experiment
  recorded — so the hypothesis is both *actionable* and *untested*.
- **Code:** `src/streamstats/estimators.py`, with a test suite whose accuracy
  cases fail if the shifted estimator is "simplified" back into the naive one.
- **Experiment:** `scripts/bench_variance.py`, parameterised and deterministic,
  printing one JSON object with accuracy and per-element cost for all three
  estimators against `statistics.variance` over the same values.
- **Declaration:** `experiments.yaml.example`. Copy it to
  `~/.config/research-os/experiments.yaml`, or let `run_closed_loop.sh` install
  it into the pilot's own disposable config home. It is never read from the
  repository: an experiment's argv is the researcher's to declare, and a worker
  confined to a worktree must not be able to add one.

The capsule objects were authored in an earlier session; the code was rewritten
and extended here. `docs/INTEGRATION_BUILD_RECORD.md` §9 records both facts.

## No nested Git repository

These directories are plain files. A capsule needs a Git repository, and the
pilot creates one *in its sandbox copy* — so the fixture does not carry a
`.git` of its own, which a clone of this repository would not fetch anyway.
