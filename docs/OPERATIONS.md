# Operating Research OS

Three commands cover everything that is not research: finding out what this
machine can do, recovering a run that stopped, and reclaiming disk.

```
researchctl doctor [--verbose] [--json] [--no-storage]
researchctl storage [--reclaim] [--json]
researchctl research resume RUN_ID [--retry] [--force]
```

## doctor

`doctor` answers one question: if I ask Research OS to do something right now,
what happens? It inspects the environment, the agent providers and how the
roles would actually be assigned, the literature configuration and index, the
declared experiment commands and the scheduler, any run that is stuck or
waiting, and disk.

It makes no network request, submits no job, and invokes no model. A command
you run to find out whether things are set up has to be free, and must not fail
for reasons unrelated to setup.

Three outcomes, and the difference matters:

| | meaning | exit code |
| --- | --- | --- |
| `PASS` | works | 0 |
| `WARN` | a capability is absent or degraded | 0 |
| `FAIL` | something you asked for is broken | 1 |

A machine with one provider, no cluster and no declared experiments is a fine
machine for most research. If that exited non-zero, everyone would learn to
ignore the exit code, and then it would tell you nothing when something was
actually wrong.

Every `FAIL` carries an action, and any command in the advice is printed on its
own line so it can be copied:

```
WARN  runs
------------------------------------------------------------------------
  PASS  research runs waiting       none
  WARN  research runs interrupted   RR-20260912T101500Z-0a1b2c3d
        These stopped mid-flight and nothing will move them. Recover
        one, or end it with 'research cancel':
             researchctl research resume RR-20260912T101500Z-0a1b2c3d
```

`--json` emits the same report as a machine-readable object.

### Review independence

The check worth reading first is `review independence`. Research OS routes the
reviewer away from the implementer's model family when it can. When only one
provider is installed it still runs — a different model reading a frozen diff
catches real defects — but the report says plainly that it is **not** an
independent review, before the run rather than after.

## storage

`storage` measures every runtime store and says how much of it is reclaimable.

Reclaimable means exactly one thing: bulk that a *finished* run is still
holding and that can be rebuilt — an isolated worktree, a controller-owned
check environment. Run records, event ledgers, prompts, model outputs, reviews
and evidence packets are never counted, because they are how a run explains
itself, and no amount of disk pressure makes deleting them the right move.

`--reclaim` releases it, through each controller's own cleanup — the automation
controller for code and analysis runs, the experiment controller for experiment
runs — each of which knows which worktrees it created and refuses any path
outside them. A single run can also be released on its own with `auto cleanup`,
`paper cleanup`, or `experiment cleanup`.

Branches are kept, deliberately: a branch holds the exact tree the work ran in,
and an experiment's artifacts are recorded by path as well as content hash, so
removing it would be deletion rather than cleanup. Nothing scientific is
touched — scientific truth is in Git, not in runtime state.

## Recovering an interrupted run

If the process dies mid-run, the run's file still says `EXECUTING` and nothing
will ever move it. `researchctl research resume` is the only thing that can,
and it makes you decide what happens to the task that was in flight, because
the controller cannot tell from outside whether that task spent anything — a
worktree may exist, a cluster job may have been submitted, a provider may have
been invoked and charged.

- **default**: mark the interrupted task failed and continue with whatever does
  not depend on it. Nothing is repeated.
- **`--retry`**: put the task back in the queue. Fine for literature, analysis,
  proposal, code and paper tasks, each of which starts a fresh delegated run.
- **`--force`**: required to `--retry` an interrupted *experiment*. Check
  `researchctl experiment runs` first; re-running one is the mistake that costs
  real money.

A run interrupted while still *planning* is failed rather than resumed.
Planning is a single model call and produced nothing to salvage.

## Budgets

A research run's budget has separate counters, because the resources are not
interchangeable:

| counter | default | what exhausting it means |
| --- | --- | --- |
| `max_model_calls` | 20 | no more frontier calls |
| `max_write_tasks` | 2 | no more repository writes |
| `max_experiments` | 2 | no more executions |
| `max_cluster_submissions` | 2 | no more `sbatch` |
| `max_wall_clock_seconds` | 7200 | stop starting new tasks |
| `max_repair_attempts` | 1 | capped at 1 by the field itself |

Every one is checked *before* the spend, against the value persisted on disk.
A plan the run could never pay for is refused at planning time; a cluster
submission over budget is refused before `sbatch` rather than reported after;
and the wall-clock bound stops the loop from starting another task rather than
killing one mid-flight.

## Where state lives

All four directories honour an environment override, which is how the test
suite keeps off your machine:

| | default | override |
| --- | --- | --- |
| config | `~/.config/research-os` | `RESEARCH_OS_CONFIG_HOME` |
| data | `~/.local/share/research-os` | `RESEARCH_OS_DATA_HOME` |
| cache | `~/.cache/research-os` | `RESEARCH_OS_CACHE_HOME` |
| state | `~/.local/state/research-os` | `RESEARCH_OS_STATE_HOME` |

Experiment commands live in `~/.config/research-os/experiments.yaml`,
deliberately outside every worktree, so no model-written file can add one.
