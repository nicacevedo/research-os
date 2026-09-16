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

## The closed-loop pilot

```bash
pilots/run_closed_loop.sh <project-path> "<objective>" [cycles]
pilots/run_second_pilot.sh <project-path> "<objective>" [cycles]   # same thing
```

The two entry points run identical machinery. `run_second_pilot.sh` exists so
that "we ran the loop against two differently shaped frontiers" is a command
rather than a claim; see `pilots/fixtures/README.md` for the second frontier and
for what a fixture is and is not.

If the project ships an `experiments.yaml.example`, the script installs it into
the *pilot's* disposable config home so the project's declared experiments are
reachable. It is never read from the repository: a worker confined to a worktree
must not be able to add an experiment command or widen one.

`run_pilot.sh` demonstrates "launch once, then no manual choreography" and
**fails if the project moved**. This one has to exercise the human scientific
gate, and a promotion writes a capsule file — so it works on a **copy** of the
capsule inside the pilot sandbox. The researcher's repository is read once,
hashed before and after, and never written.

```text
phase 1   autonomous cycles -> findings -> a grounded proposal
          -> WAITING_FOR_SCIENTIFIC_DECISION, with no successor
phase 2   the human scientific act, stood in for
phase 3   researchd observes -> CAPSULE_CHANGED, once -> a successor cycle
phase 4   a second daemon over the same state: no duplicate cycle
```

Then it asserts eleven properties from the durable record alone and prints a
verdict: one `CAPSULE_CHANGED`, no run with two successors, one objective, a
successor that exists, a frontier that moved, every cycle terminal, exactly one
logical proposal, exactly one canonical promotion, grounding in runtime
findings, those findings quoted in the proposal, and a recorded scientific
basis.

### Phase 2 is a stand-in, and is labelled as one

It writes a capsule object through the capsule layout, exactly as `researchctl
propose promote` does. It does **not** invoke that command: `AGENTS.md` forbids
an automated agent from invoking it or answering its confirmation prompt, and
the command requires an interactive terminal for that reason.

The promotion path is v1 code with its own tests. What had never been shown is
that the runtime *notices* a promotion nobody told it about and continues on its
own — that is phase 3, and phase 2 is its premise.

### What a pilot on this host cannot reach

`edit_in_worktree` and `run_local_experiment` execute code a model wrote or
chose, and at `high` autonomy the runtime requires OS-level containment for
both. This host can provide none, so both are refused. A pilot that lowered the
policy to reach them would be demonstrating that the release runs model-written
code with the researcher's credentials when its declared containment is absent.
