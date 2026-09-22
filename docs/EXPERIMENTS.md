# Experiments and HPC

Deterministic execution of experiments a researcher declared, locally or on a
Slurm cluster, with provenance a result can be traced through.

## No model ever writes a command

This is the central design decision, and it is enforced by where a file lives.

Experiment commands are declared in `~/.config/research-os/experiments.yaml` —
**outside every automation worktree**. A write-enabled worker is confined to its
worktree, so it cannot reach that file. It therefore cannot add a command, widen
an existing one, or change what one runs. That is true by construction, not by
policy.

What a plan may do is choose a declared command *by name* and supply values for
its *declared parameters*:

```yaml
projects:
  my-project-id:
    commands:
      fit-model:
        name: fit-model
        argv: ["uv", "run", "python", "-m", "myproject.fit", "--seed", "{seed}"]
        parameters:
          - name: seed
            type: integer
            required: true
            minimum: 0
            maximum: 65535
        outputs: ["results/fit.json"]
        checks: ["outputs_exist", "outputs_are_json"]
        timeout_seconds: 1800
```

Every supplied value is checked against its declared type before it becomes an
argument:

| Type | Checked as |
|---|---|
| `integer`, `number` | parses, and inside `minimum`/`maximum` |
| `choice` | one of the declared `choices` |
| `path` | relative, no `..`, and resolves *inside* the worktree — a symlink that leaves is refused |
| `token` | a plain token: no whitespace, quoting, or shell syntax |
| `flag` | genuinely `true` or `false` |

A value that does not fit is **refused, never escaped**. Escaping would mean
guessing what the caller meant, and the caller may be a model.

Substitution is by whole token. `{seed}` as an entire argument becomes one
argument; `--seed={seed}` is refused, because building a token out of a supplied
value is string-building by another name. Nothing reaches a shell: the result is
an argv list for `subprocess.run`.

A parameter the researcher did not declare is refused rather than ignored — a
plan that thinks it controls something it does not would produce a run record
that does not describe what happened.

## Nothing runs without authorisation

By default, `researchctl experiment run` resolves the command, prints exactly
what would run, and stops. `--execute` is what makes it happen.

That default exists because a system where asking a literature question and
starting a cluster job look the same from the outside is a system that will
eventually start a cluster job by accident.

Three limits, all configurable per project:

```yaml
limits:
  max_submissions_per_run: 4       # cluster jobs one research run may submit
  max_local_runs_per_run: 8
  max_wall_clock_seconds: 21600    # the most any one command may ask for
  require_explicit_execute: true   # set false to let this project run unprompted
```

Every run records **how** it was authorised — the flag, or the configured
allowance — so "who said this could run" is answerable afterwards.

## Slurm

```yaml
slurm:
  enabled: true
  # ssh_host: cluster-login      # an alias from your own ~/.ssh/config
  partitions: [sched_mit_sloan, sched_mit_sloan_interactive, mit_normal]
  account: my-account
  default_time_limit: "01:00:00"
```

`partitions` is an allowlist **and** an ordering. The first entry is the default,
so preferring the Sloan partitions over `mit_normal` is a matter of listing them
first — there is no site-specific rule in the code deciding that for anyone. A
partition not in the list is refused.

**Remote clusters.** If this machine is not a submit host, set `ssh_host` to an
alias from your own `~/.ssh/config`. Every scheduler command then runs as
`ssh <alias> <command...>` — no user, no port, no identity file, no `-o` option.
Research OS never holds a credential; your SSH configuration and agent already
do, and duplicating that would be a liability with no benefit. An `ssh_host`
that is anything but a plain alias is refused.

**The scheduler is the authority on state.** `squeue` answers while a job is
live, `sacct` once it is not, both parsed with `--noheader --parsable2`. The raw
state string is kept verbatim beside the mapped one, because a mapping is an
interpretation. A state this build has not seen becomes `unknown` rather than
being rounded to "failed", and a job neither command knows about is `unknown`
too — it may have aged out of accounting, and calling a lost job failed would
misreport it.

**The job script is generated, not supplied.** Every `#SBATCH` line comes from a
validated setting, and the experiment's command goes in as a quoted argument
vector whose tokens were already refused if they contained shell syntax.

## What happened on this machine

No `sbatch`, `squeue`, or `sacct` — this is not a Slurm submit host. The adapter
reports that plainly:

```
$ researchctl experiment scheduler
  available       no
  detail          Slurm is not enabled in experiments.yaml. …
```

Enabling it here without an `ssh_host` reports `not on PATH: sbatch, squeue,
sacct. This machine is not a Slurm submit host; set slurm.ssh_host to submit
from one that is.` **No job has ever been submitted to a real cluster from this
build.** The Slurm path is exercised end to end in tests against real fake
binaries producing byte-for-byte Slurm output.

## Two callers, one declaration

Three things reach these declared commands, and none of them can add one.

`researchctl experiment run` is a person, explicitly, with `--execute`.

The **objective cycle** (`runtime/actions/experiments.py`) designs against a
capsule Hypothesis, preregisters, submits through the runtime's own
executors, and interprets against criteria fixed beforehand.

The **discovery portfolio** (`portfolio/empirical.py`) does the same for an
idea version rather than a Hypothesis, and differs in two ways worth knowing
about here. It runs in a disposable Git worktree and removes it afterwards,
branch included, so the canonical checkout is byte-identical. And its
preregistration carries a *machine-checkable* decision rule -- one number in
one file the run writes, and two thresholds on it -- which ordinary code
applies once the result exists, so nothing is ever asked what the output
meant. `docs/AUTONOMOUS_DISCOVERY_ARCHITECTURE.md` §19 specifies it.

All three read the same `experiments.yaml`, which lives outside every
worktree. A command the researcher has not declared is not runnable by any of
them.

## Results: candidate packets, never Evidence

After an execution the controller locates the declared outputs, hashes each one,
runs the declared checks, and assembles an **evidence packet**.

The word *candidate* is load-bearing. R0's `Evidence` is a capsule object a
Claim may rest on and a human Review binds by digest. A packet is a structured
summary of what a process produced, for a human to read before deciding whether
any of it is Evidence at all. It carries **no verdict about any hypothesis** —
that judgement is exactly what this system reserves for a person.

A packet is `usable` only when the execution completed, exited zero, produced
every declared output, and passed every check. Otherwise it says why not:

```
candidate evidence packet (NOT capsule Evidence, NOT accepted)
  usable          no
  why it is not usable
    - declared outputs were not produced: results/fit.json
```

Two things are recorded even though they are inconvenient:

- **Undeclared writes.** A command that wrote somewhere its specification did
  not mention is not an error, but it is how a result quietly comes to depend on
  a file nobody tracked.
- **A missing declared output is a blocker, not a warning.** Exit zero is not a
  result if the file that was promised is not there.

Deterministic checks available: `outputs_exist`, `outputs_are_non_empty`,
`outputs_are_json`, `exit_code_zero`. A check this build does not implement
**fails** rather than being skipped — a guarantee nobody provides must not look
as though it held.

## Measurement honesty

Unobserved is `unknown`, never a number.

- Wall clock: monotonic, so a clock adjustment cannot produce a negative
  duration.
- CPU time and peak memory: `resource.getrusage(RUSAGE_CHILDREN)` locally,
  `sacct` on a cluster.
- GPU count, node count on a laptop, exit code of a queued job: `None`, rendered
  as `unknown`.

A report that guesses gets quoted later as a measurement.

## Isolation

An experiment runs in a worktree, and the worktree is scanned for outbound
symlinks **before** the process starts. Without that, a declared output path
that is a symlink out of the checkout would put a result outside it and Git
would report a clean run.

This is Git-level and filesystem-level containment, not an OS sandbox. Project
code runs with your permissions. See `SECURITY.md`.

## Commands

```bash
researchctl experiment commands PROJECT       # what is declared, and where
researchctl experiment scheduler [PROJECT]    # can this machine submit a job?
researchctl experiment run PROJECT TASK --param seed=7          # shows, runs nothing
researchctl experiment run PROJECT TASK --param seed=7 --execute
researchctl experiment show RUN_ID
researchctl experiment poll RUN_ID            # ask the scheduler
researchctl experiment cancel RUN_ID
researchctl experiment runs
researchctl experiment events RUN_ID
researchctl experiment example-config         # a commented starting point
```

## Where state lives

| What | Where |
|---|---|
| Declared commands, limits, scheduler settings | `~/.config/research-os/experiments.yaml` |
| Run records, logs, job scripts, packets | `~/.local/state/research-os/experiments/<XRUN-…>/` |
| The results themselves | the worktree the experiment wrote them in |

Results are referenced by digest rather than copied into runtime state: a second
copy doubles the storage of every experiment and can drift from the first.
