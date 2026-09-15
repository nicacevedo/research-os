# Integration build record — autonomous runtime, vnext

What this integration cycle started from, what it converged, what it fixed, what
it could not verify here, and what remains. A record of a point in time, like
`docs/V1_BUILD_RECORD.md` and `docs/R5_BUILD_RECORD.md`; the live
specifications are `docs/RUNTIME.md` and `docs/CAPSULE.md`.

Historical records are not rewritten to agree with this one. Where the R5 build
record says something this cycle changed, the change is recorded here.

## 1. Starting state, verified rather than assumed

Established by inspection before anything was modified.

```text
working tree            /home/nicacevedo/research/research-os   clean
checked-out branch      r5/autonomous-runtime                   7e7bfc9649a4af5c59341a9823b29004954c7012
                        == origin/r5/autonomous-runtime

origin/main             b3fd03c549c329ad21efb7ae203a9acc7387efa0   (tag v1.0.0)
origin/release/v1.1.0-autonomy
                        847ddecd2f08f116240f2ac6def63142844211f4
local main              e35414f7178dbd3656a6081dc5b0af621b7cc924   (stale, 53 behind origin/main)
```

Ancestry, computed rather than recalled:

```text
merge-base(r5, v1.1)        = b3fd03c   (== origin/main == v1.0.0)
merge-base(r5, main)        = b3fd03c
merge-base(v1.1, main)      = b3fd03c

origin/main is an ancestor of r5    yes
origin/main is an ancestor of v1.1  yes
v1.1 is an ancestor of r5           NO
```

So the two lines are **siblings on v1.0.0**, not a chain. `main` does not
contain the v1.1 work, which settles the question §2 of the integration brief
left open: the current stable scientific/capability line is
`release/v1.1.0-autonomy`, and it had never met R5.

### Running processes

107 `cursorsandbox` wrapper processes belonging to this user, all in
`futex_wait_queue`, aged seven days, left over from an earlier editor session.
None was killed. Because they cannot be proven inert, **every change in this
cycle was made in a separate worktree** —
`/home/nicacevedo/research/research-os-integration` — rather than in the
checkout they might still hold. No `researchd`, no PostgreSQL server and no
pytest belonging to anyone else was running.

### Host capabilities, probed rather than assumed

```text
bwrap            /usr/bin/bwrap           present
systemd-run      /usr/bin/systemd-run     present
unshare          /usr/bin/unshare         present
podman                                    absent
docker                                    absent
apptainer / singularity                   absent
sbatch / squeue / sacct / srun            absent
psql / initdb / pg_ctl (system)           absent
```

PostgreSQL for tests and for `runtime dev-db` comes from the `pgserver` dev
wheel, as `docs/RUNTIME.md` §8 describes. Slurm is genuinely unavailable from
this host; see §5.

### Baseline verification

Each line run on its own checkout, with its own `.venv`, before the merge.

```text
release/v1.1.0-autonomy   uv run --frozen pytest -q      2605 passed
r5/autonomous-runtime     uv run --frozen pytest -q      3006 passed
```

`--extra runtime` does not exist on the v1.1 line, which is itself a fact about
the divergence: the runtime extra and the `researchd` entry point arrive with
R5.

## 2. Convergence

```text
integration branch   integration/autonomous-runtime-vnext
base                 release/v1.1.0-autonomy (847ddec)
merged               r5/autonomous-runtime   (7e7bfc9)   --no-ff, history preserved
```

Neither published history was rewritten, rebased or force-pushed.

**Textual conflicts: three, all documentation.** `CHANGELOG.md` (both lines
wrote an `## [Unreleased]` section), `README.md` (both added a Status
paragraph), and `ARCHITECTURE.md`, which merged cleanly. Both changelog
sections are preserved under one converged heading, demoted a level rather than
edited, because each records what its line actually did.

**Source conflicts: none.** The two changesets overlap in exactly three files,
all Markdown. R5 lives almost entirely in a new package; v1.1 changed the v1
layers underneath it.

That is a low *textual* conflict rate and says nothing about semantics, which
is audited separately (§3).

```text
uv run --frozen pytest -q                                3249 passed
uv run --frozen ruff check .                             clean
uv run --frozen ruff format --check .                    clean (270 files)
uv run --frozen pytest -q tests/test_runtime_*.py         359 passed
same files, reverse order                                 359 passed
```

3249 = 2605 + (3006 - 2362): every test from both lines, and no test from
either lost.

## 3. What the semantic audit found

Git reported no source conflict and the full suite passed at 3249, so what was
left to find was semantic: places where R5 calls into a v1 layer that v1.1
changed, which no test on either line exercised because neither line had the
other's code. An independent read-only reviewer audited every R5 → v1 call site
against the v1.1 diff. Seven findings, all verified against the code before
acting on any of them.

| severity | finding | disposition |
|---|---|---|
| high | a paced literature provider became a permanent, *successful*, empty review | fixed |
| high | the runtime passed no `--model` and no `--effort` on any call | fixed |
| medium | the runtime coding path bypasses v1.1's check-profile resolution | reported, not closed |
| medium | a dead duplicate index rebuild that would raise `TypeError` | removed |
| low | `CapsuleError` classified as `UNKNOWN` | fixed |
| low | two "stop and ask a person" mechanisms; no live contradiction | recorded |
| — | the coding action's crash reconciler was dead code | found here, fixed |

Two are worth reading in full because the fix is less interesting than the
failure.

**The pacing deferral.** v1.1 added a persistent cross-process pacer that
reserves a slot *before* the network call and, when the reservation is refused,
returns an ordinary `ProviderOutcome(status=RATE_LIMITED, attempted=False)` with
no records. Its own docstring is explicit about why the `attempted` field
exists: *"'we did not ask' and 'there is nothing' are different facts about a
literature review."* The R5 adapter turned it into an empty result and — the
part that made it lasting — recorded that non-search in the **idempotency
ledger as COMPLETED**. The ledger key is per `(run, query)`, so every later
cycle of that run short-circuited on the same key and never asked that provider
again. A review that was never performed was recorded as one that found
nothing, for the life of the run, and the planner read it as a fact about the
literature.

Reachable two ways, both ordinary: a real 429 with a `Retry-After` beyond
v1.1's 60-second inline budget persists a `rate_limited_until` for up to a day;
and any search by another process in the preceding one to three seconds refuses
the reservation, where `poll_interval_seconds` defaults to 2.0 and arXiv's
minimum interval is 3.0.

**The dead reconciler.** `AutomationController.start` has taken
`reserved_run_id` since v1.0.0, added by a commit named "harden crash recovery
and orphan reclamation", precisely to close the window between creating a run
directory and the caller learning its id. `research/controller.py` uses it.
`runtime/actions/coding.py` did not — and its reconciler read
`plan["_reserved_run_id"]`, which nothing ever set. So it returned `None`
unconditionally, meaning "the effect did not happen", and a crash between the
worktree being created and the ledger recording it left an orphan branch and a
retry that started a second automation run for one logical task. The mechanism
to prevent it existed and was not wired.

The lesson generalised into the two new adapters: a reconciler that depends on
state the crashed attempt was supposed to have left behind is a reconciler that
does not work after a crash. Both `propose_capsule_change` and
`nominate_insight` **re-derive** their reserved identity from the cycle's own
`(run, cycle)` rather than reading a stashed field, and
`tests/test_runtime_proposals.py` asserts the key survives new findings landing
between attempts — which an earlier version of it did not, because the grounding
digest was in the key.

## 4. Correctness work, and what each fix is a fix *of*

Implemented in the order the integration brief set, because each depends on the
one before it.

### Experiment → result → interpretation identity

`interpret_results` answered "which experiment" with `order by finished_at desc
limit 1`. Association by temporal coincidence, and invariant 11 is not satisfied
by a scientific reading attached to whichever job the scheduler happened to reap
last. `experiment_interpretations` (migration 0006) makes it durable, unique per
`(job, interpreter_version)`, bound to the exact `spec_digest`, and crash-safe.

Sixteen tests in `tests/test_runtime_interpretation.py`, covering every case the
brief listed. The two that needed real processes:

```text
crash after the artifact      a child writes the interpretation artifact and is
                              killed with os._exit before the claim completes.
                              The retry reconnects the same artifact id.
daemon restart                the claim is ABANDONED by the recovery pass, and
                              the retry adopts the same logical interpretation.
```

The crash child runs the *production* handler and replaces only
`complete_interpretation`, so what is tested is the real artifact bytes and the
real claim rather than a reconstruction that could agree for the wrong reason.

### The closed loop

`docs/RUNTIME.md` §14a is the specification. Seventeen tests in
`tests/test_runtime_proposals.py`, fifteen in
`tests/test_runtime_capsule_watch.py`, sixteen in
`tests/test_runtime_nominations.py`.

The headline one is `test_the_closed_loop_from_finding_to_successor_cycle`,
which is driven entirely by `Daemon.tick()`:

```text
a runtime finding
  -> a cycle plans propose_capsule_change
  -> a grounded, noncanonical proposal citing that finding
  -> WAITING_FOR_SCIENTIFIC_DECISION, and no successor (the frontier cannot
     have moved: a proposal is not canonical science)
  -> a person promotes it, written through the capsule layout
  -> researchd observes the change
  -> CAPSULE_CHANGED, exactly once
  -> a successor cycle, new thread, recorded lineage, different frontier
```

No continue command, no resume command, and no notification from the scientific
kernel appears anywhere in it. The only human act is the promotion, which is the
one act that is supposed to be human.

### Prompt injection through a finding

A finding's `summary` is usually a model's sentence about a model's output, and
it reaches a prompt that is about to be used to ground a *scientific proposal* —
the strongest thing any fenced block in this system is used for.

`tests/test_runtime_proposals.py` runs the attack twice. First against the
renderer: a finding whose summary closes `RUNTIME_FINDING_FENCE`, opens a forged
"THE COMPLETE SET OF IDENTIFIERS YOU MAY CITE" section and declares
`FIND-attacker-invented`. The delimiters are neutralised and the forged heading
arrives as one inert line. Then end to end: a hostile finding tells the worker
it may cite the invented id, the worker complies, and the deterministic
validator refuses the proposal — because the allowlist is controller-authored
text outside every fence, and it is the only thing that decides.

## 5. Production hardening, and the two things this host cannot do

### Containment

`research_os/sandbox.py`, `ARCHITECTURE.md` §12b, `docs/RUNTIME.md` §14c,
`SECURITY.md`. Implemented, wired into both command runners, three policy modes,
high-autonomy execution overriding to `required`, nineteen tests.

**Measured, not assumed, and the measurement is the finding.** Probing this host:

```text
bwrap 0.9.0          present; every namespace variant fails
                     `--unshare-user`  -> setting up uid map: Permission denied
                     `--unshare-net`   -> loopback: Failed RTM_NEWADDR
                     cause: kernel.apparmor_restrict_unprivileged_userns = 1
                            and /usr/bin/bwrap is not setuid
unshare -U           succeeds; writing /proc/self/uid_map is denied
systemd-run --user   -P -p ProtectHome=tmpfs -p PrivateNetwork=yes starts the
                     unit, reports success, and the process SEES the real home
                     directory and the real network. Verified by probe:
                     `systemd-run --user -P -p ProtectHome=tmpfs /bin/ls $HOME`
                     lists the researcher's home
podman / docker      not on PATH
apptainer            not on PATH
```

The systemd result is the dangerous one and the reason the probe *runs* each
technology rather than looking for its binary: a mechanism that accepts an
isolation directive and silently does nothing would have produced a deployment
that believed it was sandboxed. `bwrap --unshare-user-try` has the same shape
and is deliberately not used.

**Consequence, stated as a blocker.** High-autonomy execution of model-written
code is *refused* on this host — which is `required` behaving correctly, not a
defect. The ten escape attempts in `tests/test_sandbox.py` skip with the
probe's exact reason; the nine policy and argv tests run. The canonical-state
fingerprint from `docs/RUNTIME.md` §16 remains as defence in depth: it still
detects a capsule or Git-ref change after the fact, it still prevents nothing,
and it still sees nothing outside the repository.

A host administrator can lift it with any one of: permitting unprivileged user
namespaces, installing an AppArmor profile granting `bwrap` the `userns`
permission, or installing rootless Podman. None was done here: `AGENTS.md` rule
8 forbids `sudo` and host package changes without explicit authorisation, and
this integration had none.

### Slurm

Probed definitively and absent:

```text
sbatch squeue sacct scancel srun sinfo   none on PATH
/etc/slurm*                              does not exist
slurmd, munge                            inactive
```

So the submission path has still never met a scheduler, exactly as
`docs/R5_BUILD_RECORD.md` §8 item 3 said. What exists is the reproducible
harness the brief asked for: `tests/test_experiment_slurm_live.py`, behind
`-m slurm_live`, eight tests covering the ten steps §11 listed except node
failure and preemption — which are real states the runtime classifies and which
cannot be provoked without disrupting a shared cluster, so they keep their
deterministic mock coverage.

```bash
export RESEARCH_OS_SLURM_LIVE=1
export RESEARCH_OS_SLURM_ACCOUNT=<account>
export RESEARCH_OS_SLURM_PARTITION=<partition>
uv run --extra runtime pytest -q -m slurm_live tests/test_experiment_slurm_live.py
```

The bodies are written and have never been executed. They are built from the
same API `tests/test_experiment_slurm.py` exercises against a mock, and every
name they use was verified to resolve against the real modules — so the calls
are right by construction and whether the *cluster* behaves as they assert is
the open question.

### Provider independence

`review_independence: require` is implemented and tested four ways.
`runtime doctor` reports what this machine has:

```text
roles         analyst=claude/sonnet, coder=claude/opus, literature=claude/sonnet,
              planner=claude/opus, reviewer=claude/sonnet
independence  DEGRADED_SAME_PROVIDER_FAMILY: NOT an independent review:
              reviewer and implementer are different models (sonnet reviewing
              opus) from the same family (anthropic); no second model family is
              installed
```

**One provider family.** So `require` would refuse every critical scientific
review here, and `prefer` — the default — records the degradation instead. That
is a genuine external prerequisite. No second provider family was simulated,
and a same-family reviewer is not relabelled as cross-provider independence:
`docs/RUNTIME.md` §11 says multiple agents from one family may be useful
engineering reviewers and must not be called independent scientific review.

The `planner=claude/opus` line is itself the repair of an audit finding: before
it, the runtime passed no model at all.

### Capability tiers

`ProviderProfile.tier` is configured priority, not measured performance, and the
name had misled a reader. Every available provider is assumed tier 3. That is
now stated in the class docstring, in `docs/RUNTIME.md` §11, and by
`runtime doctor` on every run — rather than fixed by building a benchmark, which
the brief was explicit should wait for a measured routing failure.
