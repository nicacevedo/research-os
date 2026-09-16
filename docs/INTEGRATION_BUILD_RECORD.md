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

## 6. The closed-loop CCAO pilot

`pilots/run_closed_loop.sh`, against the real `ccao-covariance-regressivity`
capsule. What it adds to `pilots/run_pilot.sh` is the human scientific gate, and
because a promotion writes a capsule file it works on a **copy** of the capsule
inside the pilot sandbox: the researcher's repository is read once, hashed
before and after, and never written.

### The one thing in it that is not the real act

Phase 2 writes a capsule object through the capsule layout, exactly as
`researchctl propose promote` does, and does **not** invoke that command.
`AGENTS.md` forbids an automated agent from invoking it or answering its
confirmation prompt, and the command requires an interactive terminal for that
reason. So the promotion is a *stand-in for the human act*, labelled as one
everywhere it appears.

That is honest about what is being demonstrated. The promotion path is v1 code
with its own tests; what had never been shown is that the runtime *notices* a
promotion nobody told it about and continues on its own. Phase 3 is the claim
and phase 2 is its premise.

### What the first run produced, and the two defects it found

Real provider, real capsule. The run that *passed* -- the second, after the two
defects below were fixed -- cost **6 recorded model calls and 0.5242 USD**,
against a limit of 12 calls and 6 USD, and its own report says the figure is a
floor because one call reported no cost. Eleven assertions from the durable
record.

Two corrections to that number, both of which the number's own provenance
explains:

- An earlier revision of this document said "four model calls, 0.364 USD". That
  was the *first* run's figure, the one that found the two defects, carried
  forward when the rerun replaced it. `pilots/runs/.../costs.txt` is the source
  and it says 6 and 0.5242.
- Even 6 is low. Two more calls were made -- the proposal worker and its
  assessor, `INV-0001` and `INV-0002` in the proposal directory -- through the
  v1 controller, which at the time did not report to the runtime's ledger at
  all. The real count is **8**. That gap is a defect in its own right and is
  fixed; see §6b.

Ten of eleven checks passed on the first run. The two that mattered are worth
recording in full, because both are the kind of thing only a real run finds.

**The planner chose to propose, for the right reason.** Unprompted by anything
except the frontier and a finding count, `claude-opus-5` returned:

> One finding is available and no permitted action would tell this runtime
> anything new about the project. The frontier's only live items are the
> contested CLAIM-0001 and the open Q-0001, and both require canonical
> scientific writes (adjudication, retirement, promotion) that this runtime
> cannot perform — so re-reading the repository or the literature would return
> the same frontier at nonzero cost. [...] The remaining productive move is to
> convert the existing finding into a grounded, noncanonical capsule-change
> proposal [...] and place it in front of a human who can actually decide.

That is the reasoning `PLANNER@2` was rewritten to make available, arrived at on
a real capsule. The proposal cited `FIND-...65821c72`, quoted it, recorded a
scientific basis, and the stand-in promotion wrote `Q-0002` as `question/open`
— draft strength, which is the only strength promotion produces.

**Defect 1: the continuation refused at the exact moment the wait had ended.**
`CAPSULE_CHANGED` fired, `frontier_changed: True`, the advance work item ran —
and no successor cycle opened.

The cause is worth stating because it is a *semantic* error rather than a
mechanical one. `should_continue`'s progress check compared the parked cycle's
frontier digest with its **parent's**. For the ordinary continuation that is
right: a cycle that has just concluded is its own "now". For an advance it is
wrong — it asks "did that old cycle learn anything", and the answer is no,
which is precisely why it stopped and waited. The two digests were identical
(`43866175b23b` for both cycles), as they are for *every* real successor,
because the runtime cannot move the frontier its own planning is derived from.

`should_continue` now takes `observed_frontier`, the frontier as measured by
the caller, and the comparison becomes "has the frontier moved since this cycle
recorded one" — which is the question both callers actually mean. An *empty*
measurement is explicitly not a change: it means the capsule could not be read,
and treating unknown as changed opens a cycle over a frontier nobody measured.
`tests/test_runtime_capsule_watch.py` reproduces the pilot's exact shape.

**Defect 2: a failed assessor lost a good proposal — or rescued it by accident.**
The assessor exhausted its structured-output retries
(`error_max_structured_output_retries`) *after* the proposal had been stored,
because `ProposalController` stores and then assesses. The work item was
recorded FAILED. The proposal survived only because the idempotency ledger
consults the reconciler on its FAILED path:

```text
WARNING invocation IVK-...  (cycle.propose_capsule_change) recorded a failure
        but the action had taken hold; reusing it
```

It worked, and it worked by accident of a mechanism built for a different
purpose. A `ProviderInvocationError` now looks for the reserved proposal before
failing: if it is there, the action *succeeds*, `assessed=False` is stated in
the data and in the detail, and the run is not reported as failed for producing
exactly what it was asked for. A proposal that reached a person without the
independent assessment is a weaker thing than one that passed, and nothing said
so where a reader would look.

## 6a. The second pilot, and the checksum guard catching its author

`pilots/run_second_pilot.sh` against `pilots/fixtures/streamstats-variance`.
Same machinery as the closed-loop pilot; a different frontier shape, which is
the reason it exists. Fourteen migrations applied from empty, the project's
declared `bench-variance` experiment visible to the runtime, one actionable
untested hypothesis and one open question.

### What the planner did with a frontier CCAO cannot present

Unprompted, `claude-opus-5` read the new shape correctly and said so:

> The frontier shows HYP-0001 actionable but without tests, Q-0001 open, zero
> pending experiments, zero claims (so nothing to interpret, review, referee, or
> audit), and zero validation errors [...] Because this runtime cannot write
> canonical scientific state -- it cannot register a test for HYP-0001 or record
> an experiment -- the only action that changes anything is to convert the
> existing finding into a noncanonical capsule change proposal.

The ten-item proposal that followed is worth reading for one item in
particular. `PR-001` is typed `evidence_interpretation` with basis `historical`,
and its statement begins: *"The single available finding records an inspection
of the project at HEAD a6a522b9 [...] It contains no measurement, no error
values, no offset sweep."* The worker was handed one finding and said, in the
durable record, that the finding establishes nothing empirical. `PR-003` then
proposes a *better* hypothesis than the capsule's own -- that the error is
governed by the offset-to-spread ratio rather than by the offset -- and `PR-010`
is a candidate claim explicitly marked not yet assertable.

**What it did not do**, recorded because §17 of the brief asked for structurally
different handlers: it did not select `design_experiment`, which the policy
permits at every autonomy level (`A0`, `READ_REPO`). Its reason was the one
quoted above, and it is defensible. So the second pilot demonstrates the loop on
a second frontier and does *not* demonstrate `design_experiment` end to end.

### Three attempts, and what each failure was

The completed run: all four phases, 12 recorded model calls, 2.3888 USD, a
nine-item proposal grounded in and quoting one finding with a checkable and
fresh basis, `Q-0002` promoted, `CAPSULE_CHANGED` exactly once
(`916823516a07 -> 937ea97dccd7`), a successor cycle with lineage, and a second
daemon over the same state ingesting nothing. **Ten of eleven assertions
passed.**

The eleventh -- "exactly one logical proposal" -- failed, and finding out why
turned up three defects in one mechanism:

1. **The cross-cycle dedup had never matched anything.** It compared
   `FindingPacket.digest` against the `finding_packet_digest` a proposal stores,
   and those are different functions over different material: the runtime hashes
   `(finding_id, RuntimeFinding.digest)`, v1's `supplied_findings_digest` hashes
   `(finding_id, kind, statement, rests_on)` under its own prefix. Never equal,
   so the check returned `None` every time. It had been written, reviewed by
   three audits, and was decoration. Only a second real cycle over one unchanged
   packet could show it, which is exactly what §17 asks a second pilot for.
2. **A promotion is not an answer to the items it did not touch.** With the
   comparison working, the predicate "has no promotions" was wrong: the
   researcher promoted one item of nine, so the proposal counted as answered and
   the successor re-asked the other eight.
3. **The dedup must exclude this cycle's own reserved id.** A crashed earlier
   attempt of the same cycle leaves exactly that directory; it has to reach the
   recovery path, which settles the reservation and writes the finding links,
   rather than being reported as somebody else's equivalent proposal. The
   crash-recovery test caught this the moment the comparison started working.

And one gap that is **not** fixed, recorded rather than guessed at: there is no
record of a decline. `ProposalStore` writes promotions and nothing else, so a
proposal somebody read and rejected stays pending forever, and now that the
dedup works the runtime will never propose about those findings again. An
earlier docstring asserted that "one they declined is answered" -- a state the
system cannot represent.

### The guard that reported the wrong repository

Attempt three passed all four phases and then failed with `!! FAIL: the pilot
modified the researcher's real project`. It had not. Every `.research` hash was
byte-for-byte identical; the diff was `git rev-parse HEAD` moving from `aee48c7`
to `8a56dba` and a dirty-file list emptying -- a commit this session made to the
*enclosing* repository while the pilot ran.

`fingerprint_source` ran `git rev-parse HEAD` and `git status --porcelain`
unconditionally, so for a project vendored inside another repository it
fingerprinted the outer one. Same root cause as the `git archive HEAD` bug
fixed earlier in the same script: both assumed `$SOURCE_PROJECT` is a Git work
tree root. Git state is now read only when it actually is, and the file hashes
cover the whole project rather than only `.research`.

A guard that cries wolf about the wrong repository is worse than no guard,
because the third time it fires nobody reads the diff.

### The guard that refused its own author

The first attempt at this pilot failed in phase 4, and the failure is worth
keeping:

```text
migration 0014 (one_successor_per_run) was applied with a different checksum.
An applied migration must never be edited; add a new one instead.
```

Phases 1 to 3 had run against `0014` as originally written. Between phase 3 and
phase 4, this session edited `0014` to add the duplicate-successor precheck --
and the pilot's own disposable database had already applied the original. The
checksum guard did exactly what it exists to do, to the person who wrote it,
inside their own sandbox.

Two things follow. The mechanism is not decorative: it caught an edit made
minutes earlier by someone who knew the rule. And the operational lesson is
narrow and real -- a long-running pilot holds a migrated database across its
phases, so a migration edited mid-pilot invalidates it. The rerun was on a
stable schema.

## 7. The scientific-authority audit, stated as things a reader can check

Not a promise. Each row names the file to read or the test to run.

| claim | how to check it |
|---|---|
| the runtime cannot promote a proposal | `tests/test_runtime_authority.py::test_no_runtime_module_promotes_a_proposal` parses every file under `research_os/runtime` for an import of `research_os.proposal.promote` or a call to `write_promotion`/`prepare_promotion` |
| the proposal action writes no file itself | `test_the_proposal_action_writes_no_capsule_file` parses `actions/proposals.py` for a builtin `open` or a `write_text`/`write_bytes`/`mkdir`/`unlink`/`rmtree` attribute call |
| the runtime cannot author a human Review | `test_no_runtime_module_writes_a_human_review`; `research_os.review`'s writers are unreachable from the package |
| the runtime cannot accept a Claim | `test_no_runtime_module_reimplements_the_acceptance_rule`; the runtime may *call* `validate.claim_approval` and may not define anything shaped like it |
| only one module reaches a capsule | `test_only_the_kernel_adapter_reaches_the_capsule`; `runtime/kernel.py` is the sole importer of `research_os.capsule`/`validate`/`review` |
| that module has no write method | `test_the_kernel_adapter_exposes_no_write_method` inspects `ScientificKernelAdapter`'s public surface |
| no `A2` action has a handler | `tests/test_runtime_registry.py` |
| a nomination cannot become knowledge | `tests/test_runtime_nominations.py::test_a_nomination_is_written_with_the_transfer_judgement_left_blank` and `test_nothing_is_promoted_and_no_insight_exists_afterwards` |
| PostgreSQL holds no science | `sql/0001_runtime.sql`'s header states it and the schema contains no scientific object; deleting the database loses the queue, the leases, the spend and the checkpoints, and loses no science |
| human promotion stays explicit | `proposal/commands.py::_promote` and `insights/commands.py::_promote` both refuse a non-interactive terminal, and `AGENTS.md` binds agents regardless of what the terminal permits |

Two things this release *added* to the boundary rather than merely preserving:

**A proposal is refused if its scientific basis moved.** Promotion is the only
door, and before this it would open onto a capsule that had changed since the
reasoning was written. `research_os/proposal/basis.py`.

**A nomination cannot express the judgement that a finding transfers.** The
runtime leaves `scope`, `assumptions` and `applicability` empty, and a
`PromotedInsight` requires all three non-empty. The gap is not a field a future
convenience can fill; it is two types in two stores.

## 8. Operational autonomy: what is still manual, and which of it is intentional

The distinction the whole release rests on. These are different lists and
conflating them is how "the system needs babysitting" and "the system asks me
about science" get confused.

### Intentional human scientific decisions — these are the design

```text
researchctl review <CLAIM-ID>          accept a Claim; needs a TTY
researchctl propose promote <PROP> --item <PR-00N>
                                       make a proposed item a DRAFT object
researchctl insight promote <NOM>      make a nomination cross-project knowledge
edit a manifest + record a Decision    change a primary endpoint
create a new Experiment                change a preregistration materially
merge a candidate branch yourself      integrate to a canonical branch
submit or release it yourself          publish
delete it yourself, in a Git commit    delete scientific state
```

All eight `A2` actions are human-*executed*, which is a property rather than a
policy: each means writing canonical scientific state, merging to a canonical
branch, or publishing, and the runtime has no method for any of those.

### Remaining manual *operational* actions

Honest and short.

| action | why it is still manual |
|---|---|
| `researchctl runtime start <project> --objective ...` | starting an objective is the launch. Nothing should invent one |
| starting `researchd`, or installing `deploy/researchd.service` | enabling a persistent service on someone's machine is theirs to decide; `AGENTS.md` rule 13 |
| `researchctl runtime migrate` on upgrade | idempotent and called at every startup, so in practice automatic; listed because a DSN pointing at a database the operator has not migrated is a real state |
| `researchctl runtime dev-db start` | only on a machine with no PostgreSQL |
| `researchctl storage --reclaim` | 12 GB of finished worktrees on this machine. Reclaiming is destructive and bounded, so it asks |
| answering a surfaced approval with `runtime approve` / `decline` | this *is* the scientific gate. Operational only in the sense that the verb is a CLI command |

**What is no longer manual, and was before this release:** continuing an
objective after a scientific change. That was a typed command, and it is now
`_observe_capsules` plus `_work_advance_objective`.

## 8a. The three independent adversarial audits

Three audits ran against the branch after the first closed-loop pilot, one per
area, each told to try to break a specific claim rather than to review the diff.
All three findings tables are in `docs/RUNTIME.md` §16. What is worth recording
here is the *shape* of what they found, because it was not what the mission
brief anticipated.

**Not one of them found a missing feature.** Every finding was a place where
this code, or a docstring in it, or this document, asserted a property the
implementation did not have. Seven of the twenty-one were assertions that had
been true when written and had stopped being true; the rest had never been true.

Four are worth naming as classes, because each one recurs:

**A lock held for less time than the thing it protects.** `lock_run` took
`pg_advisory_xact_lock` inside its own `with self._db.tx()`, which commits
before the function returns. The docstring described the race precisely and the
implementation released the lock before the caller's next statement. The same
shape nearly went into `claim_next_interpretation` — the first draft of the fix
for the interpretation race was `for update skip locked` on the eligibility
query, in its own transaction, which would have been exactly as useless. The
lesson is mechanical: a transaction-scoped lock is only a lock for callers
inside that transaction, so either the whole check-then-act moves into the
transaction, or the invariant moves into the schema. Both were used here — the
first for the interpretation claim, the second (a partial unique index) for run
succession, because the protected region there is a multi-minute LangGraph
cycle and no database transaction should be open across one.

**A guard proven in one direction.** `test_each_status_constraint_matches_its_
python_enum` is parametrized over `ENUM_CONSTRAINTS`, so it proves that every
constraint *named in the dictionary* matches its enum, and says nothing about a
constraint in the schema that nobody added to the dictionary. Two had escaped.
The same asymmetry produced the grounding check that verified quoted ⊆ citable
and not the reverse. A test whose domain is a hand-maintained list is a test
whose coverage is a hand-maintained list.

**Containment that breaks the thing it contains.** The sandbox's PATH is four
system directories and its binds are a fixed allowlist. `uv` — the first token
of every acceptance command in this repository — is in neither, so a contained
check exits 127 and is recorded as an acceptance failure of the code the worker
had just written. This is worse than no containment, because it is containment
that reports a false scientific-adjacent result. Turning containment on had
never been tried end to end on this host, which is exactly why: it cannot be.

**A threat model borrowed from the wrong reader.** `terminal_safe` escapes
control characters because a *terminal* interprets them. A researcher reading a
proposal is not a terminal. `\u202e` is not a control character, is handled
perfectly correctly by every terminal, and reverses the displayed order of the
sentence a person is about to make a scientific decision from. The fix is
narrow on purpose — bidi, zero-width, separators, tag characters — because
escaping the whole of Unicode category Cf would make a reviewer's Hebrew title
unreadable in order to prevent an attack the narrow set already prevents.

**What the audits did not find, and it matters:** no authority escape. No path
by which the runtime records a Review, a promotion, an acceptance or a Decision;
no way for a prompt to reach a capsule writer; no action that moves a proposal
to promoted. The structural tests that assert this were tightened
(`record_promotion` added to the forbidden import set) and not weakened.

## 9. External blockers, with the evidence

Three. None is a defect and none was worked around.

**No Slurm.** `sbatch`, `squeue`, `sacct`, `scancel`, `srun`, `sinfo` are all
absent; `/etc/slurm*` does not exist; `slurmd` and `munge` are inactive. The
submission path has never met a scheduler. `tests/test_experiment_slurm_live.py`
is the harness, behind `-m slurm_live`; its bodies are written and have never
been executed.

**No OS containment.** `kernel.apparmor_restrict_unprivileged_userns = 1` and
`/usr/bin/bwrap` is not setuid, so every bubblewrap variant fails at the uid map
or at loopback setup; `unshare -U` succeeds and writing `/proc/self/uid_map` is
denied; `systemd-run --user`'s sandboxing directives start the unit and do not
bind; `podman`, `docker` and `apptainer` are absent. Lifting it needs root,
which `AGENTS.md` rule 8 forbids without explicit authorisation. Consequence:
high-autonomy execution of model-written code is **refused** on this host.

**One provider family.** `claude` is authenticated; `codex` and `gemini` are
not installed. So `review_independence: require` would refuse every critical
scientific review here, and `prefer` — the default — records
`DEGRADED_SAME_PROVIDER_FAMILY` instead. A same-family reviewer is not
relabelled as cross-provider independence.

**One real research programme.** `ccao-covariance-regressivity` is the only
capsule on this machine that is somebody's actual research. The second pilot
therefore runs against `pilots/fixtures/streamstats-variance`, and what that is
needs stating precisely.

Its *capsule* -- one open question about whether a one-pass variance estimator
survives a large additive offset, and one draft hypothesis addressing it -- was
authored in an earlier session and had been living in that session's scratchpad
under `/tmp`, registered from there. That is not durable and it is not
reproducible from this repository, so both the capsule objects and a rewritten,
extended version of the code are vendored here: three estimators rather than
two, a test suite whose accuracy cases fail if the shifted estimator is
"simplified" back into the naive one, and `scripts/bench_variance.py`, a
parameterised experiment that measures accuracy *and* per-element cost against
`statistics.variance` over the same values. `experiments.yaml.example` is the
declaration the researcher copies to `~/.config/research-os/experiments.yaml`;
it is not read from the repository, because an experiment's argv is the
researcher's to declare.

Its frontier is the reason it is here: one actionable, untested hypothesis and
one open question, where CCAO has one contested claim and one open question.
`critique_hypothesis` and `design_experiment` are reachable on this frontier and
not on that one, which is what "structurally different" means for the
engineering.

**What it is not:** a second research programme, or evidence that the loop
produces good science on a capsule it did not anticipate. The hypothesis was
written to be testable by a command that exists, by the same hand that wrote the
runtime. The CCAO run is the one with a real scientific frontier behind it, and
the two runs are reported separately for that reason.

One note on this document's own accuracy. An earlier revision said the fixture
was "authored as a pilot fixture in an earlier session", which was true; a later
revision of *this section* asserted the path was empty and the claim false,
which was wrong -- `find /home/nicacevedo -maxdepth 6 -type d -name .research`
does not reach `/tmp`, and the registry did. Both statements are left recorded
rather than replaced, because a build record that edits away its own mistakes is
not a record of anything.
