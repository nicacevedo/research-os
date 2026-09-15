# Pilots

One command, one real project, and a proof that the project was not modified.

```bash
pilots/run_pilot.sh <project-path> "<objective>" [cycles] [autonomy]
```

Everything the runtime writes goes into `pilots/runs/<stamp>/` — its own XDG
root, its own artifact store, its own literature index. A pilot cannot touch the
researcher's shared data, and the project repository is only ever read. The
script hashes the project's `.research/` tree and its Git HEAD before and after
and **fails** if either moved.

`autonomy` defaults to `low`: `READ_REPO` and `NETWORK_READ` and nothing else.
Raising it is a deliberate decision, and the levels are documented in
`docs/RUNTIME.md` §7.

## What a pilot demonstrates, and what it does not

It demonstrates the thing the architecture is for: **launch once, then no manual
choreography.** There is no line in `run_pilot.sh` that says "now run the
reviewer", "now check the cluster", or "now start the next cycle". The daemon
ingests the request, queues it, claims it, runs the cycle, and decides whether a
successor is warranted. The script's only job after `runtime start` is to run
`researchd` and then read the reports.

It does not demonstrate that the science was any good. That is what the human
scientific gates are for, and a pilot that ends in
`WAITING_FOR_SCIENTIFIC_DECISION` has succeeded operationally.

## Terminal states a pilot can legitimately reach

```text
DONE_FOR_NOW                       the frontier had nothing worth the next cycle
WAITING_FOR_SCIENTIFIC_DECISION    an A2 gate; the packet is in `runtime approvals`
WAITING_FOR_EXTERNAL_DEPENDENCY    a cluster job is outstanding
BUDGET_EXHAUSTED                   the configured limit was reached
FATAL_INFRASTRUCTURE_ERROR         something broke and named itself
```

## Prerequisites, and what happens without them

| prerequisite | without it |
|---|---|
| a Research Capsule in the project (`researchctl init-project`) | the frontier cannot be derived; the pilot cannot start |
| a model provider (`researchctl doctor`) | the cycle plans nothing and concludes immediately, honestly |
| two provider *families* | critical reviews run degraded, and say so |
| Slurm configured in `experiments.yaml` | `submit_cluster_experiment` is not offered |
| PostgreSQL | started disposably by the script if no DSN is set |

None of these is faked. A pilot with one provider family produces a run report
that says its review was not independent.
