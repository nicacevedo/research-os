# Autonomous runtime — release-candidate closure report

Branch `integration/autonomous-runtime-vnext`. Structured as the integration
brief's §24 asks: A–J, evidence first.

This report is **not** the specification and not the build record.
`docs/RUNTIME.md` is what the runtime does; `docs/INTEGRATION_BUILD_RECORD.md`
is what this cycle did and why; this is the evidence a reader needs to decide
whether to accept it. Where a claim here is checkable, the check is named
instead of described.

---

## A. Starting state

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

**Ancestry, computed rather than recalled.**

```text
merge-base(r5, v1.1)        = b3fd03c   (== origin/main == v1.0.0)
merge-base(r5, main)        = b3fd03c
merge-base(v1.1, main)      = b3fd03c

origin/main is an ancestor of r5    yes
origin/main is an ancestor of v1.1  yes
v1.1 is an ancestor of r5           NO
```

The two lines were **siblings on v1.0.0, not a chain.** That settles the
question §2 of the brief left open: the current stable scientific/capability
line was `release/v1.1.0-autonomy`, `main` did not contain it, and it had never
met R5.

**Running related processes.** 107 `cursorsandbox` wrapper processes belonging
to this user, all in `futex_wait_queue`, aged seven days, from an earlier editor
session. None was killed, and because none could be *proven* inert, every change
in this cycle was made in a separate worktree,
`/home/nicacevedo/research/research-os-integration`, rather than in the checkout
they might still hold. No `researchd`, no PostgreSQL server and no other
person's pytest was running.

**Baseline tests**, each on its own checkout with its own `.venv`, before any
merge:

```text
release/v1.1.0-autonomy   uv run --frozen pytest -q      2605 passed
r5/autonomous-runtime     uv run --frozen pytest -q      3006 passed
```

`--extra runtime` does not exist on the v1.1 line, which is itself a measure of
the divergence: the runtime extra and the `researchd` entry point arrive with
R5.

---

## B. Integration

**Branch strategy.** One integration branch off the stable line, with the
runtime line merged into it. Not the reverse, and not a rebase of either.

```text
integration branch   integration/autonomous-runtime-vnext
base                 release/v1.1.0-autonomy (847ddec)
merged               r5/autonomous-runtime   (7e7bfc9)  --no-ff, history preserved
```

Neither published history was rewritten, rebased or force-pushed. Nothing was
merged into `main`.

**Textual conflicts: three, all documentation.** `CHANGELOG.md` (both lines
wrote an `## [Unreleased]`), `README.md` (both added a Status paragraph), and
`ARCHITECTURE.md`, which merged cleanly. Both changelog sections are preserved
under one converged heading, demoted a level rather than edited, because each
records what its line actually did.

**Source conflicts: none.** The changesets overlap in exactly three files, all
Markdown: R5 lives almost entirely in a new package and v1.1 changed the v1
layers underneath it. That is a low *textual* conflict rate and says nothing
about semantics.

**Semantic conflicts: seven, found by auditing every R5 → v1 call site against
the v1.1 diff.** Git reported no conflict and the merged suite passed, so these
were exactly the class of defect no test on either line could have caught —
neither line had the other's code. Two are worth reading in full:

- *A paced literature provider became a permanent, successful, empty review.*
  v1.1 added a cross-process pacer that reserves a slot before the network call
  and returns `ProviderOutcome(status=RATE_LIMITED, attempted=False)` when the
  reservation is refused; its docstring is explicit that *"'we did not ask' and
  'there is nothing' are different facts about a literature review."* The R5
  adapter turned that into an empty result **and recorded the non-search in the
  idempotency ledger as COMPLETED**. The key is per `(run, query)`, so every
  later cycle of that run short-circuited on it. A review never performed was
  recorded as one that found nothing, for the life of the run, and the planner
  read it as a fact about the literature. Reachable two ordinary ways: a 429
  with a `Retry-After` beyond v1.1's inline budget, or any search by another
  process in the preceding 1–3 seconds.
- *A crash reconciler that was dead code.* `AutomationController.start` has
  taken `reserved_run_id` since v1.0.0 — added by a commit named "harden crash
  recovery and orphan reclamation" — and `runtime/actions/coding.py` did not use
  it, while its reconciler read a `plan["_reserved_run_id"]` nothing ever set.
  It returned `None` unconditionally, meaning "the effect did not happen", so a
  crash between worktree creation and the ledger write left an orphan branch and
  a retry that started a second automation run for one logical task.

The full table, including the one finding **reported rather than closed** (the
runtime coding path still bypasses v1.1's check-profile resolution), is
`docs/INTEGRATION_BUILD_RECORD.md` §3.

**Final integrated SHAs.**

```text
641863c  Converge the v1.1 autonomy line with the R5 autonomous runtime
f6707a8  Close the autonomous research loop, and repair what the merged tree broke
270677c  Repair what three independent adversarial audits found
b42897f  Make 0014 upgradeable over a database the race already happened in
```

---

## C. Correctness fixes

### Experiment → result → interpretation identity

The runtime used to interpret "the job that finished most recently". That is
association by temporal coincidence, and it produced three distinct errors: two
jobs finishing while a cycle planned gave the interpretation to whichever was
reaped second; a job interpreted in one cycle was interpreted again in the next;
and a reading could be compared against a preregistration belonging to a
different experiment.

Replaced by a durable relation, `experiment_interpretations` (schema 0006),
unique on `(job_id, interpreter_version)`, carrying the job's `spec_digest`.
Eligibility is "no *completed* interpretation at this reader version", oldest
first with `job_id` as tiebreak — a stable total order over a set that only
grows at one end, so two workers asking the same question get the same answer
and a backlog drains in the order it formed.

Two later repairs matter as much as the relation:

- **The criteria are outside `spec_digest`.** The digest covers the *execution*
  — argv, cwd, environment, seeds — and says nothing about the primary endpoint
  or the success criteria, which live beside it in the preregistration record.
  So two design passes over one declared command could store two
  preregistrations with the same digest and different endpoints, and a function
  returning "the" preregistration had to choose. It chose the newest. The
  sequence that exploits this is short: read an experiment, crash before
  recording it, design again with a different endpoint, and the retry compares
  the same result against the new criteria while reporting
  `criteria_were_fixed_before_results: true`. A post-hoc primary-endpoint change
  with no recorded Decision, which is the single thing this subsystem exists to
  prevent. The claim now *binds* the preregistration artifact (schema 0010), and
  an ambiguous set that disagrees is refused rather than resolved.
- **Selecting and claiming are one transaction.** They were two calls, so two
  workers both selected the oldest eligible job and both performed the whole
  reading; the unique constraint discarded the loser's work at the end.
  `claim_next_interpretation` does the select with `for update of j skip locked`
  and the claim in one transaction.

### The proposal handler

`PROPOSE_CAPSULE_CHANGE` is a **thin adapter over the existing v1
`ProposalController`**, as the brief required. It is roughly 200 lines of
reservation, packet assembly, reconciliation and payload; the proposal engine,
its schema, its validators, its grounding allowlist and its assessor are v1's,
unchanged. `tests/test_runtime_authority.py` asserts by parsing the package that
the adapter writes no capsule file, imports no promotion code, authors no Review
and accepts no Claim.

A cycle that produces a proposal concludes `WAITING_FOR_SCIENTIFIC_DECISION`,
not `DONE_FOR_NOW`. "Finished" is the wrong word for "waiting for you".

### Runtime-finding grounding

The v1 grounding allowlist has had a `finding_ids` field since v1.0.0 and has
always refused a citation it was not given. **Nothing ever supplied one**, so an
autonomous result reached the proposal layer as prose concatenated into a
natural-language goal, and "what is this proposal resting on" was answerable
only by reading a model's sentence.

`RuntimeFinding` (schema 0007) is typed, digested, immutable and explicitly
noncanonical, with provenance edges to artifacts, capsule objects, literature
keys and an experiment job. The allowlist now contains exactly the
`capsule_ids`, `literature_keys` and `finding_ids` actually supplied; the
finding *text* is rendered as fenced untrusted data through
`automation/promptdata.py`, never as prose; and the chain

```text
proposed change -> proposal item -> finding -> artifact -> bytes
```

is traversable without trusting anything a model wrote.

The link that had been missing in practice was smaller than any of that: **no
action recorded a finding at all**, so the allowlist was empty on every real
cycle. `graphs/cycle.py` now has a `FINDING_FOR_ACTION` table and a
`_record_finding` step.

### Proposal replay safety

Caller-reserved identity, not a reconciler alone.
`reservation_key_for(state)` is `propose_capsule_change:<run>:<cycle>` —
nothing per-attempt, and nothing that changes *between* attempts, which is why
the grounding digest is deliberately **not** in the key: new findings landing
between two attempts must not mint a second identity for one logical proposal.
`reserved_proposal_id` derives the whole proposal id from that key, so a retry
recomputes it, finds the directory the interrupted attempt created, and adopts
it.

The reservation row is written before the v1 controller is called, so a crash
between "the store has a directory" and "the runtime knows about it" leaves
something to reconcile rather than an orphan. `settle_proposal_reservation`
makes `CREATED` absorbing and `FAILED` correctable, because the controller
stores and *then* assesses: an assessor failure settles `FAILED` while the
directory exists, and the retry that finds the proposal has to be able to say so.

### Staleness protection

Promotion is the only door into canonical state, and before this it opened onto
a capsule that might have changed since the reasoning was written.
`proposal/basis.py` records a `ScientificBasisSnapshot` — the digests of the
objects the proposal actually cites, expanded one hop over evidence, hypotheses,
assumptions and any Review *of* a cited object — and `prepare_promotion` refuses
when that basis moved.

Explicitly **not** whole-repo `HEAD == base_commit`, which the brief ruled out
and which would be both too strict (any unrelated commit blocks) and too loose
(a capsule edit with no commit passes). `BasisStatus` distinguishes three
states, and the third is the one that took a second attempt to get right:
`unchecked` — "this proposal's findings were not re-verified" — is *reported*
and never blocks, because making it block would have made every runtime-authored
proposal unpromotable forever.

### Capsule-change observation and automatic continuation

Runtime-side observation, with no scientific-kernel dependency on PostgreSQL or
LangGraph. `capsulewatch.py` hashes what is canonical — every object's
`(id, status, subject_digest)`, the charter, the state document, the project
identity — and `_observe_capsules` records it in `capsule_observations` (schema
0008). A change emits one `CAPSULE_CHANGED` event, deduped on
`capsule-changed:{project}:{previous}->{digest}`, and
`_work_advance_objective` opens a successor cycle with recorded lineage. No
immortal thread: each cycle is a bounded run, and the watcher is one pass of the
daemon's tick.

Three repairs here are worth naming:

- **`status` is in the watch digest and out of the kernel's own digest, and both
  are right.** `semantic_projection` excludes `status` so a Review does not go
  stale when an object is moved administratively — the science it reviewed is
  unchanged. The watcher asks a different question, "did anything canonical
  change", and a Question moving `open -> answered` is the single most important
  thing a researcher does that it must not miss.
- **A `Review`'s own fields are hashed.** `Review` is the only
  `ScientificObject` absent from `Reviewable`, so a verdict change was invisible
  to the watch digest.
- **One validation, not two.** `observed_digests` claimed both digests came from
  one read and called `validate()` twice; a researcher's commit landing between
  them paired a capsule digest from before it with a frontier from after.

### Insight nomination

`NOMINATE_INSIGHT` reuses the v1 insight subsystem and leaves `scope`,
`assumptions` and `applicability` **empty**. Those three fields *are* the
judgement that a finding transfers to another project, so the runtime cannot
express it: `missing_for_promotion` tells the researcher what they must write.
The gap is not a field a future convenience can fill, it is two types in two
stores. "Not worth nominating" is an expressible and ordinary outcome.

---

## D. Production hardening

### Real Slurm

**Not validated. The dependency is genuinely absent and nothing here pretends
otherwise.** `sbatch`, `squeue`, `sacct`, `scancel`, `srun` and `sinfo` are all
absent from this host, `/etc/slurm*` does not exist, and `slurmd` and `munge`
are inactive. The submission path has never met a scheduler.

What exists instead: `tests/test_experiment_slurm_live.py`, eight tests behind a
`slurm_live` marker, deselected by default and enabled with
`RESEARCH_OS_SLURM_LIVE=1 -m slurm_live`. Their bodies are written and **have
never been executed.** That is stated in the file, in the build record, and here.

Two real defects on that path were found and fixed by reading rather than by
running, and both would have mattered on a first real submission:

- **`#SBATCH --export=NONE`.** sbatch's default is `--export=ALL`, which copies
  the submitting process's whole environment into the job. The submitting
  process is the runtime daemon, whose environment holds provider API keys and
  the PostgreSQL DSN. Nothing in the runtime ever asked for that propagation.
- **Choosing `executor: slurm` was a one-word bypass of `required`
  containment.** `build_executors` raises the local executor to `required` at
  high autonomy and passed the mode only to the local one. Nothing in this
  process can contain a process on a compute node, so at `required` the Slurm
  executor now *refuses*, as `WAITING_FOR_EXTERNAL_DEPENDENCY` rather than a
  repairable failure, because the missing thing really is external.

### Sandbox

**This host cannot contain, and that is reported as a release blocker for
high-autonomy execution rather than papered over.**

How each row below was established, because an earlier revision of this section
said "the probe *runs* each technology" and a final adversarial review checked:
`probe_bubblewrap` genuinely executes `bwrap` and is the row that matters;
`probe_ineffective_systemd_run` is a `shutil.which` plus a recorded measurement,
not a live run; `podman` and `docker` are binary-presence checks; and
**`apptainer` and `unshare` are not probed at all** — those two lines were
measured once by hand at a shell and are reported here as history, not as probe
output. `researchctl runtime doctor` prints four sandbox rows, not six.

```text
bubblewrap     /usr/bin/bwrap present, not setuid; every namespace variant fails
               `bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted`
               kernel.apparmor_restrict_unprivileged_userns = 1
systemd-run    present, and its user-scope sandboxing directives are silently
               ineffective: the unit starts and ProtectHome and PrivateNetwork
               do not bind. Not used, because containment that reports success
               without containing is worse than none
podman         absent
docker         absent
apptainer      absent
unshare        `unshare -U` succeeds; writing /proc/self/uid_map is denied
```

Consequence, enforced: `SandboxMode.REQUIRED` is set automatically at high
autonomy, so **high-autonomy execution of model-written code is refused on this
host.** Lifting the restriction needs root, which `AGENTS.md` rule 8 forbids
without explicit authorisation.

Where containment *is* available, `sandbox.py` builds bubblewrap with
`--unshare-all` (with `--unshare-user` re-asserted, because `--unshare-all` is
the `-try` form and would otherwise continue without a user namespace),
`--clearenv`, `--die-with-parent`, `--new-session`, an allowlist of read-only OS
paths, the caller's writable paths, and — bound *after* them, so they win —
`.git` and `.research` read-only. That last one is not hypothetical: an
adversarial review showed both were writable inside "containment", wrote a
`post-checkout` hook that produced zero drift from `canonical_fingerprint`, and
the next `git worktree add` ran it on the host.

Three containment findings from the audits are worth carrying into the report
because two of them made containment actively harmful:

- **Containment made the command vanish.** `uv` — the first token of every
  acceptance command in this repository — installs to `~/.local/bin`, which is
  in neither the sandbox PATH nor the read-only binds, and its cache lives under
  a home the sandbox replaces with an empty tmpfs. A contained check exited 127
  and was recorded as an acceptance failure *of the code the worker had just
  written*.
- **A uid-scoped rlimit presented as a sandbox control, twice.**
  `process_limit_preexec` was applied to uncontained children too, and
  `RLIMIT_NPROC` is counted per `(user namespace, uid)`, not per process tree.
  Against this user's existing 1119 threads a ceiling of 512 meant the child
  could not create its first thread: `uv` aborted with `SIGABRT` and 17 tests
  failed with "required acceptance commands failed". The same mistake as
  `LimitNPROC=256` in the systemd-run probe, in different code. There is no
  per-process-tree process limit in POSIX rlimits, and that sentence is now in
  the function.
- **`--share-net` is the host's network namespace, not a filtered one.**
  Bubblewrap has no packet filter and this build adds none, so a command granted
  network can reach 127.0.0.1 — the PostgreSQL port, any local dev server — as
  well as the internet. There is no middle setting; the only containment for
  what it can reach is not granting it.

### Provider independence

One provider family is authenticated here (`claude`); `codex` and `gemini` are
not installed. So:

- `review_independence: require` **refuses** every critical scientific review on
  this host rather than relabelling a same-family reviewer as independent;
- `prefer` — the default — records `DEGRADED_SAME_PROVIDER_FAMILY` and states it
  in the run report: *"NOT an independent review: reviewer and implementer are
  different models (sonnet reviewing opus) from the same family (anthropic); no
  second model family is installed."*

**No provider-benchmark platform was built**, as the brief required. Tiers are
configured priority and `runtime doctor` says so verbatim: *"provider tiers are
CONFIGURED priority, not measured performance: every available provider is
assumed tier 3 unless you set one. A report saying a call was answered at tier 3
means the configuration permitted it, not that anyone benchmarked it."*

### Budgets and failure behaviour

Reserve → execute → reconcile, with `reserved` money promised and `spent` money
certainly gone, so two concurrent workers cannot both be told there is room for
the last call — **for the calls the runtime's own router makes.** It makes none
of the calls inside a delegated action, and that gap is the most substantive
defect the second pilot found: `runtime run` reported one model call for a cycle
that made four, and a run started with `--max-cost-usd 6` reported a tenth of
what it had spent, because `propose_capsule_change` and the coding action hand
the work to v1 controllers that own their own providers and their own per-action
ceilings. Nothing was unbounded; the *cost* cap simply did not apply to those
calls. `BudgetLedger.charge_all` now records a delegated spend after the fact,
past the limit when it must, and reports which budgets it broke — so the cap
bites on the call after the overrun. That is weaker than a reservation and it is
the strongest thing that is true once the money is gone.

**Measured, on the second pilot's own database.** Twelve model calls, 2.3888
USD, of which five rows carry `prompt_version = delegated:propose_capsule_change`
and total **1.6478 USD**. Under the previous code those five were invisible, so
the run would have reported 0.7410 USD — a **3.2× under-report** of what the
researcher was actually charged, on an ordinary cycle with no failures. Failure classes drive routing: `POLICY_REFUSED` and
`CAPABILITY_DENIED` are terminal and are *not* repaired, because retrying a
refusal burns the attempt budget real transient failures need — and repairing
"this host cannot contain" would run the same escaping code again.

The wall-clock dimension had a defect worth recording: it charged from
`started_at`, which never advances, so every resume billed the whole run again,
and a `least()` clamp hid the overrun.

### Migrations

Fourteen forward-only, checksum-verified numbered files. An applied migration
that has been edited is refused, because two machines would otherwise diverge.
Exercised three ways: from empty, as an upgrade over the previous release's
schema with rows already in it, and in both suite orders.

0014 required a fifth consideration that the others did not. It creates a
partial unique index enforcing one successor per run — and the race it closes
was *real*, so an existing deployment may already hold rows that violate it.
Left alone the operator would have seen PostgreSQL's own `could not create
unique index`, on a forward-only migration they cannot edit, with no instruction.
The migration now finds the duplicates first and raises an error naming each
parent and its competing successors, explaining why the state was reachable and
what to do. It does not choose: deleting a research run is the operator's
decision.

### Observability

`runtime doctor` reports what this machine can actually do, with no network
call: schema version, action/handler coverage, resolved model roles, independence
status, every containment probe's *measured* result, and check-profile
resolution. `runtime status` shows parked objectives and the capsule-observation
count — a parked run is `SUCCEEDED`, so it appeared nowhere under RUNNING,
WAITING FOR YOU or FAILED, which made the state a researcher most needs to see
the hardest one to notice.

---

## E. Verification

### Commands, verbatim

```bash
cd /home/nicacevedo/research/research-os-integration
uv run --frozen ruff check .
uv run --frozen ruff format --check .
uv run --frozen pytest -q                                  # full suite
uv run --frozen pytest -q tests/test_runtime_*.py          # runtime suites, forward
uv run --frozen pytest -q $(ls -r tests/test_runtime_*.py) # runtime suites, reverse
RESEARCH_OS_SLURM_LIVE=1 uv run --frozen pytest -q -m slurm_live   # never executed here
./pilots/run_closed_loop.sh <project> "<objective>" 2
./pilots/run_second_pilot.sh pilots/fixtures/streamstats-variance "<objective>" 2
```

### Counts

```text
release/v1.1.0-autonomy, before the merge            2605 passed
r5/autonomous-runtime, before the merge              3006 passed
after the merge, before any new work                 3249 passed
after loop closure                                   3453 passed, 18 skipped
after the audit repairs and the budget fix           3510 passed, 18 skipped
after the final adversarial review's repairs         3521 passed, 18 skipped
runtime suites only, forward order                     487 passed
runtime suites only, reverse order                     487 passed
slurm_live                                            8 collected, 0 executed
```

3249 = 2605 + (3006 − 2362): every test from both lines, none lost. The 18
skips are the containment tests this host cannot run, and they are skipped with
the measured reason rather than passed.

### Order independence

The runtime suites share one PostgreSQL database, so a test that leaves state
behind is a test whose result depends on what ran before it. Both destructive
migration tests take a **throwaway** database, created and dropped per test,
because tampering with `schema_migrations` on the shared one leaked into every
later test that called `migrate()` — it broke five CLI tests, visible only once
the suites ran in a different order.

### Chaos outcomes

Real process death, not mocked exceptions. `tests/runtime_scripts/` holds
scripts that are launched as subprocesses and killed at a chosen point:

- `crash_after_interpretation.py` — killed between writing the interpretation
  artifact and marking the claim complete. The next attempt re-derives the
  *same* artifact bytes, because the inputs are the job row and the stored
  preregistration and both are immutable, so the content-addressed store returns
  the same id and the claim is completed against it. One logical interpretation,
  one artifact, whatever the process did. The script monkeypatches
  `RuntimeStore.complete_interpretation` and runs the **production** handler: an
  earlier version hand-built the criteria and produced a different artifact id,
  which proved nothing about determinism.
- `crash_after_proposal.py` — killed after the proposal directory exists and
  before the reservation is settled. The retry recomputes the reserved id,
  adopts the directory, writes the finding links it never got to write, and
  reports `reused: True`.
- A nonzero exit is recorded without a scientific opinion, and a coding run that
  reaches the canonical checkout is caught by the before/after fingerprint and
  failed as `POLICY_REFUSED` — so it is not repaired and retried, because
  repairing it would run the same escaping code again.

### Security outcomes

| property | evidence |
|---|---|
| a finding cannot inject instructions into a prompt | rendered through `promptdata.render_data_block`, which neutralises every known fence delimiter and *proves* the fence it produced is unique before returning |
| the citable id list is not reachable from inside a fence | `tests/test_proposal_grounding_correction.py::test_the_findings_catalogue_is_outside_the_fenced_block` checks every fence's span against the catalogue's offset |
| the stored statement is the text the worker read | `test_the_stored_statement_is_what_the_worker_actually_read`; the prompt clipped at 1500 and the record kept 4000, so a conclusion past the cut appeared in `propose show` and never reached the worker |
| a terminal cannot be repainted from a finding | `textsafe.terminal_safe` escapes every control character; the acceptance path also uses `start_new_session=True`, because an adversarial review opened `/dev/tty` from a check and repainted the terminal while stdout captured something innocuous |
| explicit bidi and invisible **controls** are made visible | `DECEPTIVE_CHARS` escapes bidi overrides and isolates, zero-width and invisible characters, separators, interlinear annotation, tag characters and variation selectors. Not control characters, handled correctly by every terminal, and able to change what a researcher reads. **Narrower than "a finding cannot read differently from what is archived"**, which an earlier revision of this row claimed: UAX#9 reorders a neutral run around any strong RTL *letter*, with nothing escaped and the bytes unchanged, so one legitimate-looking Hebrew corpus name can swap two numbers in a rendered line. Escaping letters would make honest content unreadable; a renderer that wants the strong guarantee must wrap each untrusted field in `U+2066`/`U+2069`, which is forgery-proof because those are in the set |
| the acceptance gate escapes its output | `cli.py::_render_packet` wraps the whole review packet. It did not until the final review: this module contained no use of `terminal_safe` at all, making the one screen that records human scientific authority the one unescaped screen in the system |
| git is not influenced by the researcher's config | `automation/gitutil.py` neutralises `core.fsmonitor`, `core.hooksPath`, `core.sshCommand`, `protocol.ext.allow` and `uploadpack.packObjectsHook`, points `GIT_CONFIG_GLOBAL/SYSTEM` at `/dev/null`, and injects `--no-ext-diff` per subcommand (measured: `-c diff.external=` makes git *run* the empty string) |
| a DSN password does not reach a log | `config.redact_dsn`, covering query-string `password` and `sslpassword` as well as the authority |
| a sandbox does not receive `os.environ` | `check_overrides` returns the controller's *deltas*; `check_environment` returns `dict(os.environ)` plus them and is used only on the uncontained path, where `subprocess.run` needs a complete environment |

### Live cluster results

**None. No Slurm on this host.** See D and H. The harness exists, is deselected
by default, and has never been executed.

### Pilot outcomes

**Pilot 1 — `ccao-covariance-regressivity`, a real research capsule.** Real
provider. The passing run recorded **6 model calls and 0.5242 USD** against a
limit of 12 and 6 USD, and its report says that is a floor because one call
reported no cost — and the true count is **8**, because two calls (the proposal
worker and its assessor) went through the v1 controller, which did not report to
the runtime's ledger at all. Eleven assertions from the durable record alone.

An earlier revision of this report said "four model calls, 0.364 USD", which was
the *first* run's figure carried forward when the rerun replaced it.
`pilots/runs/.../costs.txt` is the source of the corrected number, and the
under-count is the subject of the budget defect in §D.

The first run passed ten of eleven and found two defects that only a real run
finds:

1. *The continuation refused at the exact moment the wait had ended.*
   `CAPSULE_CHANGED` fired, `frontier_changed: True`, the advance item ran, and
   no successor opened. `should_continue`'s progress check compared the parked
   cycle's frontier digest with its **parent's** — right for an ordinary
   continuation, wrong for an advance, where it asks "did that old cycle learn
   anything" and the answer is no, which is *why* it stopped and waited. The two
   digests were identical (`43866175b23b` for both cycles), as they are for
   every real successor, because the runtime cannot move the frontier its own
   planning is derived from. `should_continue` now takes `observed_frontier`,
   and an empty measurement is explicitly not a change.
2. *A failed assessor lost a good proposal — or rescued it by accident.* The
   proposal was stored and then the assessor exhausted its structured-output
   retries, so the work item was recorded FAILED; the proposal survived only
   because the idempotency ledger consults the reconciler on its FAILED path. It
   worked by accident of a mechanism built for something else.

   The rerun returned **"closed loop demonstrated"** with all eleven checks
   passing, the source project byte-for-byte unchanged, and `EXP-0003` promoted
   as a *draft* with no `decision_rule` — the historical-experiment refusal
   holding on real content.

**Pilot 2 — `pilots/fixtures/streamstats-variance`, a second frontier shape.**
Fourteen migrations applied from empty, the project's declared experiment
visible to the runtime, and a frontier of one actionable-but-untested hypothesis
plus one open question where CCAO has one contested claim.

**It took three attempts and each failure was a real finding.** The run that
completed: all four phases, 12 recorded model calls, 2.3888 USD, a nine-item
proposal grounded in and quoting one runtime finding with a checkable and fresh
basis, `Q-0002` promoted as `question/open`, `CAPSULE_CHANGED` exactly once
(`916823516a07 -> 937ea97dccd7`), a successor cycle with recorded lineage, and a
second daemon over the same state producing no duplicate (`events_ingested: 0`).
**Ten of the eleven assertions passed.**

One qualification on how they were obtained, because it bears on what they
prove: the harness aborted at its project-untouched check (a defect of its own,
below) *before* reaching the verdict block, so the eleven assertions were run
afterwards by extracting that block and executing it against the pilot's own
still-live database. Same script, same data, same run — but not the harness
end to end, and this pilot's `verdict.txt` was written from that execution
rather than by `run_closed_loop.sh`.

The eleventh — "exactly one logical proposal" — failed, and it was right to:

- Two proposals existed over **one unchanged finding packet**, because the
  cross-cycle dedup had never matched anything. It compared the runtime's
  `FindingPacket.digest` against the `finding_packet_digest` v1 stores, and
  those are different functions over different material, so the comparison
  returned nothing every time. The mechanism was decoration for as long as it
  had existed, and only a real second cycle over one packet could show it.
- With it working, a second bug surfaced immediately: the predicate was "has no
  promotions", and the researcher had promoted one item of nine. A promotion is
  not an answer to the items it did not touch, so the other eight were re-asked.
- And a third, from the crash-recovery test the moment the comparison started
  working: the dedup must exclude *this cycle's own* reserved id, because a
  crashed earlier attempt leaves exactly that directory and it has to reach the
  recovery path rather than be reported as somebody else's proposal.

The three earlier attempts failed on: the migration checksum guard refusing this
session's own mid-pilot edit to `0014`; a legitimate planner decision (the
worker put a capsule id where a proposed item id belongs, the validator refused
it correctly) aborting the harness under `set -o pipefail`; and
`fingerprint_source` reporting the *enclosing* repository's Git state for a
vendored project, so a passing run was reported as having modified the
researcher's project while every `.research` hash was identical. All three are
fixed and all three are recorded in `docs/RUNTIME.md` §16.

What pilot 2 did **not** do, stated because §17 asked for structurally different
handlers and this is what actually happened: the planner did not select
`design_experiment`, although the policy permits it at every autonomy level
(`A0`, `READ_REPO`). It considered it explicitly and said why not:

> `design_experiment` is the closest read-only alternative, but an experiment
> this runtime designs cannot be recorded, so the design would be lost; the
> productive move is to carry that design inside the proposal instead.

That is a correct reading of its own constraints on this host, and it means
**the second pilot demonstrates the loop on a different frontier and does not
demonstrate `design_experiment` end to end.** That handler's coverage is unit
tests, and this report does not claim more. The proposal it wrote instead
carried the design: three of the nine items are experiment specifications with
stated pass/fail endpoints, and one is a candidate claim marked not yet
assertable.

---

## F. Scientific authority audit

Each row names the check, not a promise.

| claim | how to check it |
|---|---|
| the runtime cannot promote a proposal | `tests/test_runtime_authority.py::test_no_runtime_module_promotes_a_proposal` parses every file under `research_os/runtime` for an import of `research_os.proposal.promote` or a call to `write_promotion`/`prepare_promotion`/`record_promotion` |
| the proposal action writes no file at all | `test_the_proposal_action_writes_no_capsule_file` parses `actions/proposals.py` for a builtin `open` or a `write_text`/`write_bytes`/`mkdir`/`unlink`/`rmtree` attribute call |
| the runtime cannot author a human Review | `test_no_runtime_module_writes_a_human_review`; `research_os.review`'s writers are unreachable from the package |
| the runtime cannot accept a Claim | `test_no_runtime_module_reimplements_the_acceptance_rule`; the runtime may *call* `validate.claim_approval` and may not define anything shaped like it |
| exactly one module reaches a capsule | `test_only_the_kernel_adapter_reaches_the_capsule`; `runtime/kernel.py` is the sole importer of `research_os.capsule`/`validate`/`review` |
| that module has no write method | `test_the_kernel_adapter_exposes_no_write_method` inspects `ScientificKernelAdapter`'s public surface |
| no `A2` action has a handler | `tests/test_runtime_registry.py`; all eight remain human-executed |
| a nomination cannot become knowledge | `tests/test_runtime_nominations.py::test_a_nomination_is_written_with_the_transfer_judgement_left_blank` and `test_nothing_is_promoted_and_no_insight_exists_afterwards` |
| PostgreSQL cannot substitute for capsule truth | `sql/0001_runtime.sql`'s header states it and the schema contains no scientific object. Deleting the database loses the queue, the leases, the spend and the checkpoints — and loses no science. The literature index is rebuildable derived state and says so |
| human promotion stays explicit | `proposal/commands.py::_promote` and `insights/commands.py::_promote` both refuse a non-interactive terminal; `AGENTS.md` binds automated agents regardless of what the terminal permits |

Two things this release **added** to the boundary rather than preserving:

- **A proposal is refused if its scientific basis moved** (`proposal/basis.py`).
  Promotion was the only door and it opened onto a capsule that could have
  changed since the reasoning was written.
- **A nomination cannot express the judgement that a finding transfers.** The
  runtime leaves `scope`, `assumptions` and `applicability` empty and a
  `PromotedInsight` requires all three non-empty. The gap is two types in two
  stores, not a field a convenience can fill.

One correction to the boundary's own documentation: `refuse_scientific_authority`
advertised that "the list of refused actions is greppable". There is no such
list and there are no production callers — the enforcement is the *absence* of
write methods, the `human_executes` policy table, and the structural tests
above. The docstring now says that.

---

## G. Operational-autonomy audit

These are two different lists, and conflating them is how "the system needs
babysitting" and "the system asks me about science" get confused.

### Intentional human scientific decisions — these are the design, not a gap

```text
researchctl review <CLAIM-ID>                      accept a Claim; needs a TTY
researchctl propose promote <PROP> --item <PR-00N>  make a proposed item a DRAFT
researchctl insight promote <NOM>                   make a nomination cross-project knowledge
edit a manifest + record a Decision                 change a primary endpoint
create a new Experiment                             change a preregistration materially
merge a candidate branch yourself                   integrate to a canonical branch
submit or release it yourself                       publish
delete it yourself, in a Git commit                 delete scientific state
```

All eight `A2` actions are human-*executed*, which is a property rather than a
policy: each means writing canonical scientific state, merging to a canonical
branch, or publishing, and the runtime has no method for any of those.

### Remaining manual operational actions — the honest list

| action | why it is still manual |
|---|---|
| `researchctl runtime start <project> --objective ...` | starting an objective is the launch. Nothing should invent one |
| starting `researchd`, or installing `deploy/researchd.service` | enabling a persistent service on someone's machine is theirs to decide (`AGENTS.md` rule 13) |
| `researchctl runtime migrate` on upgrade | idempotent and called at every startup, so automatic in practice; listed because a DSN pointing at an unmigrated database is a real state, and because 0014 can legitimately refuse |
| `researchctl runtime dev-db start` | only on a machine with no PostgreSQL |
| `researchctl storage --reclaim` | 12 GB of finished worktrees on this machine. Reclaiming is destructive and bounded, so it asks |
| answering a surfaced approval with `runtime approve`/`decline` | this *is* the scientific gate. Operational only in that the verb is a CLI command |

**No longer manual, and was before this release:** continuing an objective after
a scientific change. That was a typed command and it is now `_observe_capsules`
plus `_work_advance_objective`.

---

## H. External blockers

Three. None is a defect in this code, none was worked around, and each has
evidence rather than an assertion.

**1. No Slurm.** `sbatch`, `squeue`, `sacct`, `scancel`, `srun`, `sinfo` all
absent; `/etc/slurm*` does not exist; `slurmd` and `munge` inactive. The
submission path has never met a scheduler.
*Reproducible harness:* `RESEARCH_OS_SLURM_LIVE=1 uv run --frozen pytest -q -m
slurm_live` (8 tests, written, never executed).

**2. No OS containment.** `kernel.apparmor_restrict_unprivileged_userns = 1`
and `/usr/bin/bwrap` is not setuid, so every bubblewrap variant fails at the uid
map or at loopback setup; `unshare -U` succeeds and writing
`/proc/self/uid_map` is denied; `systemd-run --user`'s sandboxing directives
start the unit and do not bind; `podman`, `docker` and `apptainer` are absent.
Lifting it needs root, which `AGENTS.md` rule 8 forbids without explicit
authorisation.
*Consequence, enforced rather than noted:* high-autonomy execution of
model-written code is **refused** on this host.
*Reproducible:* `researchctl runtime doctor`, which runs each probe.

**3. One provider family.** `claude` authenticated; `codex` and `gemini` not
installed. `review_independence: require` refuses every critical scientific
review here; `prefer` records `DEGRADED_SAME_PROVIDER_FAMILY` and says so in the
run report. A same-family reviewer is never relabelled as cross-provider
independence.

**Not a blocker, but adjacent and stated because it bounds pilot 2:** there is
one real research programme on this machine. `pilots/fixtures/streamstats-variance`
is a fixture — real code, real measurements, a real capsule, a frontier shaped
to reach different handlers — and it is not an independent second research
programme. Its hypothesis was written to be testable by a command that exists,
by the same hand that wrote the runtime.

---

## I. Final architecture verdict

```text
AUTONOMOUS_RUNTIME_BETA
```

Against the acceptance criteria the brief sets, item by item, because a verdict
that is checkable beats a verdict that is argued.

`AUTONOMOUS_RUNTIME_BETA` requires five things, and all five are met:

| requirement | met | evidence |
|---|---|---|
| a unified v1.x + R5 line | yes | §B; one integration branch, both histories preserved, 3249 tests at the merge with none lost |
| experiment identity fixed | yes | §C; schema 0006 and 0010, the claim binds one preregistration artifact, crash determinism shown with real process death |
| a grounded proposal loop working | yes | §C and §E; a proposal citing a runtime finding, quoting it, with a recorded basis, on two real capsules |
| automatic continuation after a human scientific change | yes | §E; `CAPSULE_CHANGED` once and a successor cycle with recorded lineage on both pilots. Asserted by the harness on pilot 1; on pilot 2 by running the same assertion block against the pilot's database after the harness aborted on an unrelated check of its own |
| a closed-loop real CCAO pilot | yes | §E, pilot 1; eleven assertions from the durable record, source project byte-for-byte unchanged |

`AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE` requires five more, and two are not met:

| requirement | met | why |
|---|---|---|
| real Slurm validation, if Slurm is in the intended production topology | **no** | no scheduler on this host; the harness is written and has never run (§H) |
| high-autonomy execution containment | **no** | no containment technology works here, so high-autonomy execution is *refused* rather than contained (§D) |
| a required critical-review independence policy | policy yes, capability no | `require` exists and refuses every critical review here, because one provider family is installed |
| docs / schema / release convergence | yes | fourteen migrations with a declared version the suite checks, both changelog lines preserved, historical build records left historical |
| an adversarial audit with no unresolved critical or high findings | yes | four independent audits; every finding resolved or explicitly reported, and the one left open is documented as a divergence, not a defect (§I below) |

**Why not `AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE`.** The loop is closed and was
demonstrated twice on real runs with a real provider, the authority boundary is
stronger than it was and is structurally enforced, crash recovery was shown with
real process death, and the suite is green. If the verdict were about the
*diff*, release candidate would be defensible.

It is not about the diff. Three of the production-hardening axes the brief lists
cannot be *validated at all* in this environment, and one of them is a stated
precondition of the autonomy level the release is for:

1. **No containment exists here, so high-autonomy execution of model-written
   code is refused.** That refusal is correct and it is also a statement that
   the release's headline capability is unexercised on this machine. The
   containment code path has unit coverage of its *construction*; it has never
   contained a process, because no technology on this host can. The brief's own
   instruction — "if the environment cannot support a defensible sandbox, report
   that as a release blocker rather than declaring prevention exists" — is the
   controlling sentence, and this is that report.
2. **The Slurm path has never met a scheduler.** Eight live tests are written
   and deselected. Two real defects on that path were found by reading, which is
   evidence that reading is not sufficient.
3. **Independent review is unavailable.** One provider family, so
   `require` refuses and `prefer` degrades. A release whose reviewer independence
   has never once been satisfied has not demonstrated it.

And two narrower gaps:

4. **The human scientific act in both pilots is a labelled stand-in.** Phase 2
   writes through the capsule layout exactly as `researchctl propose promote`
   does and deliberately does not invoke it, because `AGENTS.md` forbids an
   automated agent from answering that confirmation prompt. The promotion path
   is v1 code with its own tests; what the pilots demonstrate is that the
   runtime *notices* a promotion nobody told it about and continues. A human
   running the real command, once, is the missing observation.
5. **One semantic divergence is reported and not closed.** The runtime's coding
   action dispatches through `AutomationController` without a plan, so its
   acceptance commands come from the automation planner rather than from
   v1.1's `projects.<id>.check_profiles`. Same project, same goal, two different
   gates. Not a correctness or authority defect — every resolved argv passes the
   same command policy and nothing merges or pushes either way — and `runtime
   doctor` warns when a project declares profiles a runtime cycle would ignore.

**Why not `AUTONOMOUS_RUNTIME_FOUNDATION_COMPLETE`.** That was the starting
state and it understates what changed. At the start, the loop was open in
practice: no action recorded a finding, so the grounding allowlist was empty on
every real cycle; interpretation was "the most recently finished job";
continuation compared the wrong two digests, so a promotion was observed and no
successor opened; and promotion could open onto a capsule that had moved. Each
of those is now closed, and each closure is demonstrated on a real capsule
rather than asserted.

**Why not `BLOCKED`.** Nothing is stuck. Every blocker is an absent external
dependency with a reproducible harness waiting for it, and the work that does
not depend on those dependencies is finished.

**Why not `FULL_AUTONOMOUS_RESEARCH_OS_READY`.** That would require the three
axes above validated, at least one second real research programme, and a human
having actually used the gate. None of those is true.

### What moves this to `AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE`

In order of how much they are worth, and none of them is code in this
repository:

1. A host that can contain — unprivileged user namespaces permitted, an AppArmor
   profile granting `bwrap` the `userns` permission, or rootless Podman — and
   then `tests/test_sandbox.py`'s 18 skipped tests executed, plus one
   high-autonomy coding cycle run contained end to end.
2. A real Slurm cluster and `-m slurm_live` executed.
3. A second provider family installed and one critical review run at
   `require`.
4. One human promotion through `researchctl propose promote`, on a real
   proposal, with the runtime observing it.

---

## J. Highest-value remaining work

Substantive only. Nothing here is cosmetic and nothing here is speculative
infrastructure.

**1. Close the check-profile divergence.** Thread the project's resolved
`check_profiles` through `AutomationController` so a runtime coding cycle and
`researchctl research run` gate on the same commands. It is v1 work that this
integration deliberately did not take on, and it is the only known place where
two paths through the same system apply different acceptance criteria to the
same project.

**2. Make `design_experiment` worth choosing.** Pilot 2 found that a planner
facing an actionable, untested hypothesis reasoned its way to *not* designing an
experiment, because it cannot run one on this host and a design it cannot run
produces no new information. That reasoning is correct given the constraint, and
it means the design → preregister → execute → interpret arc is reachable only
where execution is possible. The work is not in the planner; it is in giving the
planner an executor it can actually use, which is item 1 of section I.

**3. Give the interpretation path a real experiment to read.** Everything from
`claim_next_interpretation` through the preregistration binding to the crash
determinism is tested against constructed jobs. It has never read a result that
came from a scheduler or a contained local run. The identity work is the part of
this release with the most mechanism and the least contact with reality.

**4. Decide what a second research programme is for.** Pilot 2's fixture proves
the loop handles a second *frontier shape*. It cannot prove the loop handles a
capsule it did not anticipate, because the same hand wrote both. That question
needs somebody else's project, and it is the highest-value *scientific*
validation remaining — as distinct from the highest-value engineering work,
which is item 1.

**5. A researcher cannot decline a proposal.** `ProposalStore` records
promotions and nothing else, so "I read this and I do not want it" has no
representation. The consequences are concrete now that the cross-cycle dedup
works: a declined proposal stays pending forever, so the runtime will keep
treating it as the answer to any cycle with the same grounding and will never
propose about those findings again. A decline record is small — a JSONL line
beside `promotions.jsonl`, and a `propose decline` command that refuses a
non-TTY exactly as `promote` does — and it is a human-authority surface, so it
needs the same care: a decline is a scientific decision and the runtime must
not be able to write one.

**6. Three cascade decisions 0013 did not make.** `tool_invocations` (the
idempotency ledger, whose header says every externally visible side effect is
claimed there before it happens), `model_calls` (which carries `cost_usd`) and
`artifact_links` (the only row pointing at content-addressed bytes on disk) all
still cascade from `research_runs`, so pruning a run destroys all three. 0013
narrowed `external_jobs` only, and an earlier revision of its comment wrongly
claimed it was the only cascading external-effect relation in the schema.
`artifact_links` is the consequential one: the preregistration guard scopes its
lookup through it, so a pruned run makes the guard refuse permanently. Fixing it
needs a primary-key migration — `run_id` is part of `artifact_links`' key and so
cannot be nullable — plus a backfilled `project_id`. Not done here; the refusal
message was corrected to stop asserting "no preregistration found" when the
truth is "no link is reachable".

**7. Two smaller things worth doing before they are load-bearing.**
`LEGACY_PREREGISTRATION_WINDOW` is a documented horizon that shrinks to nothing
on projects started from schema 0012 and does not shrink on older ones; and the
`runtime_findings` dedupe index makes a finding's identity permanent per
`(project, digest)`, which is right for citation and means a superseded finding
is never retracted, only outnumbered.

---

## What this report is not

It is not a claim that the science was any good. Two pilots ending in
`WAITING_FOR_SCIENTIFIC_DECISION` with a proposal in front of a person is an
*operational* success and says nothing about whether the proposal is worth
promoting. That judgement is the researcher's, which is the entire point of the
authority boundary this release spent most of its effort defending.
