# The human gate: closing it, and what closing it found

State of the work at the point this report stops: three architecture-critical
lifecycle defects closed, two independent reviews acted on, the live runtime
reconciled without a row being edited by hand, and the system idle at a
scientific decision that is the researcher's to make.

Formal state: **AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE**, pending the human
decision in §I. The reasoning for that verdict, and the three things it is
explicitly *not* claiming, are in §N.

## A. Starting state, verified rather than assumed

| | |
|---|---|
| Research OS | `rc/thesis-pilot` at `08a2007`, in sync with origin, tree clean |
| science repo | `research/2026-reassessment` at `783d4d2`, clean, in sync |
| daemon | not running; left stopped by the previous session |
| runtime DB | dev cluster up at the state home, schema at `0016` |
| proposals | two for this project: `33ec307e` (11 items), `b9c26fcd` (12 items), none decided |
| capsule | 10 questions, 7 hypotheses, 0 claims, 2 evidence, 1 experiment; validates with 0 errors |
| frontier | `bcaff0a4…`, matching every run's recorded digest |

Containment was re-verified against the host rather than read out of the
previous report: `bwrap 0.12.0` at sha256 `1e2250f2605d7584…`, the installed
AppArmor profile byte-identical to `deploy/apparmor-bwrap`,
`apparmor_restrict_unprivileged_userns` still `1`, and `unshare -U` inside the
**production** flag set failing with `ENOSPC`. An ad-hoc `bwrap --unshare-all`
probe does *not* deny nested namespaces; only the production flags do, which is
why the first probe I ran looked like a contradiction and was not one.

Three rows carried the previous session's unfinished business:

```text
WORK-20260918T062621Z-662eacf3  continue_objective  LEASED, lease expired
                                payload: recommendation=START_NEXT_CYCLE
                                parent: RRUN-...cb4962f4, whose frontier said WAIT_HUMAN
WORK-20260918T062621Z-2465d751  run_cycle           PENDING, run already finished
3 unconsumed events, one of them RESEARCH_RUN_REQUESTED for a CANCELLED run
```

## B. Frontier context

**Defect.** `frontier@1` received the deterministic capsule frontier and the
current cycle's previous result. Nothing else. A real cycle ranked "audit
whether the outstanding proposals already cover the open questions" first and
then wrote, in the finding it recorded:

> Absent HYP-0006's pre-specified test, the correct answer would have been
> WAIT_HUMAN. This judgement rests on the quoted frontier alone, as all
> file-inspection tools were disabled and the underlying artifacts could not be
> read.

Its top-ranked action was one it structurally could not perform, and the
`START_NEXT_CYCLE` it gave instead was qualified on material it could not open.

**Implementation.** No file access. The material was structured scientific state
the runtime already held and already assembled for the planner — and one thing
it did not assemble: a proposal reached the prompt as its reservation row, five
operational fields, from which "is Q-0004 already in front of the researcher"
is not answerable. `sciencecontext.proposal_view` reads the proposal document by
the id the reservation gives; each item carries the capsule objects it
`addresses`, whether a person has promoted or declined it, and whether its basis
is `current`, `STALE` or `unchecked`.

**Authority boundary.** The frontier gained no path, no repository block and no
tool, and its instruction says there is no later turn in which it will. The
repository used for the basis check comes from the runtime's own context and
never from the `project_path` inside the proposal document, because that field
is model-written and a reader that dereferenced it would let the document choose
what gets opened.

**Result, measured on the real pilot.**

```text
instruction            3 431 chars
outstanding proposals  6 109 chars   both proposals, 23 of 23 items
whole prompt          11 081 chars   ~2 770 tokens
33ec307e               current
b9c26fcd               STALE: EXP-0001 specified->completed,
                       HYP-0001 active->supported, HYP-0005 active->rejected
```

The coverage question is answerable from the prompt, and the older proposal is
visibly not a live decision — which is the answer to "which items are obsolete".

## C. Frontier identity

**Defect.** A finding is deduplicated on its content digest. For a handler whose
result contains a model's prose that is wrong: two assessments over one state
reaching one recommendation differ in wording, so they differed in digest, so
twenty repetitions filled all twelve slots of the planner's window and pushed the
project's real findings out of it.

**Design.** `RuntimeFinding.semantic_key`, producer-authored. When set, identity
is the key plus project, kind and producing action. `assessment_identity` is the
one producer, over the frontier digest, the decision context, the recommendation,
and each ranked candidate's action and sorted targets — and deliberately not the
rationales, the qualitative scores, the artifact ids, the run, the cycle or the
clock. The shape `<producer>:v<n>:<64 hex>` is validated, so a constant is
refused rather than silently merging every finding of its kind; a structural test
asserts there is exactly one producer.

Findings are deliberately excluded from the decision context. Including them
would change the identity every cycle — the churn back again. What is in the key
is what moves only when a person acts or the science does.

## D. WAIT_HUMAN lifecycle

**Root cause.** A cycle's conclusion had no durable home. It was returned in
`CycleResult`, which is memory, written into the `RESEARCH_CYCLE_FINISHED`
payload, which is a message, and copied again into the `continue_objective`
payload, which `should_continue` read. `RRUN-20260918T054218Z-cb4962f4` was told
`WAIT_HUMAN` by the frontier, recorded it in `FIND-20260918T054359Z-313e1ec9`,
and concluded `START_NEXT_CYCLE` — and that word was still on the queue when this
work began. Fixing how the conclusion is *reached* could not fix the frozen copy.

**Reconciliation.** `research_runs.next_recommendation` records what a cycle
concluded in the same row update that records that it concluded.
`_work_continue_objective` reads the run; the payload is advisory; a null column
means an older build finished the run, and the handler falls back and records
which source it used. It also gained the `has_successor` pre-check and the
`SuccessorExistsError` catch that `_work_advance_objective` has had since `0014` —
the two handlers had disagreed about one invariant.

**Observed on the live rows, with nothing edited by hand:**

```text
WORK-...662eacf3  lease reclaimed, re-run, REFUSED:
                  "already has a successor. A parent run has at most one, and a
                   successor that was cancelled stays cancelled: reopening it
                   would be this queue row undoing a decision a person made."
                  status SUCCEEDED, failure_class null  (before: 3 failures, dead letter)
run_cycle for the CANCELLED successor   skipped, "the run is already finished"
two run_cycle for succeeded runs        skipped
continue_objective for 001f8a0f         opened exactly one successor
```

## E. Independent reviews

Round 4, both read-only, both given the diff and told to attack it.

**Security review: 0 CRITICAL, 1 HIGH, 3 MEDIUM, 5 LOW.** The HIGH falsified the
headline property and is the most useful finding of the session: a proposal view
is *one* block entry, `prompt_safe_block` clips each entry at 2 000 characters,
and a twelve-item proposal serialises to 6 550 — so **three of twelve items
reached the model**, the JSON was cut mid-object, and the controller-authored
census reported all twelve. Both live proposals hold eleven and twelve items.
Verified by measurement before being fixed. The bounds test that should have
caught it asserted on the view object, one layer too high, while its docstring
said "so nothing is silently hidden".

The MEDIUMs: the identity fix had reached one of four handler exits, and the
three degraded ones embed the exception text — `BudgetExhaustedError` names the
run and the remaining budget, so every outage minted a fresh finding; the
identity covered less state than the assessment was made over; and
`items_unavailable` carried an absolute path under the state home into a prompt
that tells the role it has no filesystem, with a test asserting its arrival.

**Test audit: 2 RC blockers, 11 recorded gaps.** The blockers were the interrupt
branch's durable write (executed by three existing tests, asserted by none) and
the write-once-sticky `coalesce` semantics of the new column. It also found five
of the first version's tests vacuous or near-vacuous, one mislabelled, and three
of the twelve enumerated identity cases untested.

All CRITICAL, HIGH, MEDIUM and both blockers are fixed. One LOW is recorded
rather than fixed: `test_only_the_kernel_adapter_reaches_the_capsule` matches
direct imports only, and `sciencecontext` now reaches the capsule through
`proposal.basis` — one hop, read-only, and the same access `actions/proposals.py`
already had.

## F. Regression

```text
full suite                       3919 passed, 8 skipped, 0 failed
runtime suites, forward order      699 passed
runtime suites, reverse order      699 passed
sandbox + containment + isolation 1121 passed
ruff check                        clean
ruff format --check               323 files already formatted
schema                            0017, 0018 applied to the live DB
contamination audit               state roots byte-identical before/after
                                  four full suite runs
science repo                      clean, HEAD unmoved at 783d4d2
```

The 8 skips are every test in `tests/test_experiment_slurm_live.py`, each naming
its external prerequisite. No security or lifecycle test skips.

`researchctl runtime containment-audit` without `--repo` reports 36/36 and
**refuses to record**, because a run that attacked no canonical repository may
not write a validation. That is the guard `08a2007` added, working. This diff
does not touch `sandbox.py`, so the normal sandbox regression applies and the
existing keyed 38/38 record stands — both of its keys, the binary hash and the
policy digest, were independently re-verified above.

## G. Git

```text
9018a55  Know what is already in front of the researcher, and stay stopped
8c222c0  Record the three defects and what the reviews found in the fixes
894a094  Decide the two prerequisites this machine cannot satisfy
```

Local HEAD == remote HEAD == `894a094`, 0 ahead / 0 behind, tracked tree clean,
no force push. `.claude/agents/*.md` are left untracked, as they were found.

## H. Idle at the human gate

**The daemon did not start idle, and this report does not claim it did.** An
unconsumed `RESEARCH_CYCLE_FINISHED` carrying `START_NEXT_CYCLE` was already on
the queue from a cycle that concluded before the fix. The runtime honoured it —
correctly, because it was that run's own conclusion — and opened exactly one
successor, `RRUN-20260918T133221Z-182e75fc`.

That cycle did real work. The planner chose `derive_mathematics`, which is the
correct routing for HYP-0002 under the adjudication layer, and the deriver
returned `DERIVED` in nine steps, fixing the Lagrangian convention that
`ASM-0001` leaves undefined and saying so rather than assuming one. Three model
calls, $0.70, recorded as `FIND-20260918T133641Z-882ffcf4`.

Then it stopped, and the reason is the point:

> the frontier is unchanged from the previous cycle. The runtime cannot alter
> canonical scientific state, so repeating the cycle would repeat its cost
> without adding information. What is outstanding needs a person.

with `parent_recommendation_source: "run record"` — the first live cycle to read
its own durable conclusion rather than a message about it.

```text
idle before the restart   ~25 min, no work claimed, no model call
graceful stop             SIGTERM -> "finishing the current pass"
                          work_failed 0, capsule_changes 0, exit 0
idle after the restart    ~120 polling intervals, nothing logged
counts across both        19 runs, 5 findings, 38 work items, 49 model calls,
                          0 unconsumed events -- unchanged
a second researchd        found the lock held, said so, exited 0
proposals                 still two; none authored because time passed
```

## I. The decision that is waiting

See the packet delivered with this report. In short: promote `PR-009` of
`PROP-19700101T000000Z-33ec307e`, which records what HYP-0004's "reachable dual
points" quantifies over — a gap that demonstrably exists in the capsule and that
makes PR-005's counterexample search uninterpretable until it is fixed. A dry
run confirms it would write `.research/questions/Q-0011.yaml` at status `open`,
which moves the frontier.

**One measurement to read before deciding.** Five distinct objectives are
parked, and `parked_objectives` is `distinct on (objective)` — one successor per
objective. So one promotion is expected to open **up to five** successor cycles,
not one. That is the documented rule rather than a duplication defect, and it
makes "one decision, exactly one successor" ambiguous as the project stands.
Cancelling the four stale objectives first would make the causal test
unambiguous; it stops operational work and writes no science. That is the
researcher's call.

## J. Limitations, stated rather than left to be discovered

**Each human decision licenses a bounded burst, not an open-ended chain.**
`should_continue`'s progress check compares a run's frontier digest with its
parent's, and the runtime cannot move the frontier — so a cycle with a parent
always stops. Observed today: a cycle produced a nine-step derivation that did
not exist before and was stopped by a check that cannot see findings. The check
is right about the seven-cycle pilot and blind to a productive cycle, because it
measures canonical state and the work is noncanonical. Making progress mean "a
finding that did not exist before" is now cheap, because finding identity is
deterministic — but it changes how much autonomous work each human act licenses,
which is a governance parameter the researcher owns, not a defect to fix
unilaterally.

**Two invocations stay abandoned.** `IVK-20260918T062714Z-407cfa53`
(`cycle.propose_capsule_change`) and `…-23b2d05a` (`proposal.create`) belong to
the cancelled run. The ledger refuses to guess their outcome and nothing will
retry them, so every daemon shutdown reports them as needing reconciling. The
note is accurate and no proposal directory was left behind; deleting the rows to
quiet it would be the one thing this work is about not doing.

**No production-path evidence for the frontier's behaviour.** The context is
present, correctly scoped and sufficient to compute coverage from the prompt
text — asserted by decoding the rendered prompt. What has not happened is a
`frontier@2` call on a real model, because the planner chose the derivation
instead. The audit was right to distinguish those, and this report does not
claim the second.

## N. Verdict

**AUTONOMOUS_RUNTIME_RELEASE_CANDIDATE.**

Satisfied: full regression green; containment proven locally and its record's
keys re-verified; the contained coding pipeline green in the suite; frontier
context correct and measured on the real pilot; finding identity deterministic
across restart, reconnect, reordering, rewording and the degraded paths;
`WAIT_HUMAN` durable on the run's own row and authoritative over a stale
message; the daemon demonstrably idle at a human gate across a restart; no
unresolved local CRITICAL or HIGH.

Not claimed, and each is a gate rather than an opinion:

- **FULL** is not claimed. A real human decision has not yet triggered a
  successor, no scientific chain has run end to end without the outer agent, and
  the durable-daemon work is not done.
- **Slurm** is implemented and unvalidated. `ROADMAP.md` now states that as a
  decision.
- **Independent review** is degraded and labelled as such on this host. One
  provider family is installed, and the system refuses to call that independent.
