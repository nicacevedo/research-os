# R-FINAL: security closure, scientific-context completion, and the two gates

```text
Release status:  AUTONOMOUS_RUNTIME_BETA   (unchanged — see §7)
Stopped at:      HUMAN_ROOT_ACTION_REQUIRED
                 READY_FOR_HUMAN_SCIENTIFIC_DECISION
Date:            2026-09-17
Repository:      research-os-rc @ rc/thesis-pilot
```

Two legitimate human gates were reached. Everything not blocked behind them was
completed. This report says which is which, and does not claim readiness the
evidence does not support.

## 1. The pre-R-FINAL checkpoint

Captured before any change, and verified live rather than assumed.

```text
Research OS HEAD      11402f1423abf9075a6331bc6d310a50cdc450ca, tree clean
Science repo HEAD     783d4d2e249acd8eb19229ec975ab2b814a63452, tree clean
                      branch research/2026-reassessment
Baseline suite        3641 passed, 18 skipped, exit 0, in 569.87s
Daemon                PID 3840811, started 16:24, idle at the scientific gate
Runtime DSN           postgresql://…/research_os (devdb cluster under state_home)
Project spend         7.5709 USD of 300.00 (39 model calls)
Capsule digest        1a62908e5bfc, 1 change observed
```

**Cycles: 15 runs, 4 root chains, 11 parents — each appearing exactly once.**
The reported "one successor per parked objective, no duplicates" is confirmed
from the operational record, not from a claim:

```text
6e916d0b → 42c57607 → 3732f7da → 1ed3f353   WAITING_FOR_SCIENTIFIC_DECISION
7e6589aa → fdf37d31 → e51ae700 → 47a99872   DONE_FOR_NOW
60b6914f → 507c0115 → fb355620 → a0be7902   DONE_FOR_NOW
227d00d8 → 2b6d7477 → cd0b74fa              DONE_FOR_NOW
```

Exactly one `CAPSULE_CHANGED` event is in the ledger, and the four successors
requested after it are one per parked objective.

**Clean shutdown recorded.** `SIGTERM` to 3840807 and 3840811 at
`2026-09-17T16:57:36-03:00`; both gone within ~1s, which is the documented
"finish the current pass" path (`daemon.py:1436`). No `SIGKILL`.

## 2. Blocker audit — all twelve VERIFIED_FIXED, none modified

Classified from current code, current tests, and this session's own runs.

| | blocker | verdict | evidence |
|---|---|---|---|
| A | general test-state isolation | **VERIFIED_FIXED** | `tests/test_state_isolation.py` (9 tests) + session guard in `conftest.py`; and the byte-identical inventory below |
| B | proposal-store isolation | **VERIFIED_FIXED** | same inventory: `proposals=7` before and after |
| C | assessment/research-store isolation | **VERIFIED_FIXED** | same inventory: `assessments`, `research` unchanged |
| D | refusal feedback | **VERIFIED_FIXED** | `_previous_attempt`, `cycle.py:131`; `test_runtime_policy.py:192` |
| E | cross-cycle proposal deduplication | **VERIFIED_FIXED** | `0014_one_successor_per_run.sql`, 22 tests in `test_runtime_proposals.py`, plus the 11-parents-once record above |
| F | experiment/preregistration identity | **VERIFIED_FIXED** | migrations 0010/0012, 22 tests in `test_experiment_spec.py` |
| G | proposal decline lifecycle | **VERIFIED_FIXED** | 16 tests in `test_proposal_decline.py` |
| H | check-profile convergence | **VERIFIED_FIXED** | 12 tests in `test_unified_check_profile.py` |
| I | provenance-safe retention | **VERIFIED_FIXED** | `0015_effect_rows_outlive_their_run.sql`, 7 tests in `test_runtime_retention.py` |
| J | delegated hard-budget enforcement | **VERIFIED_FIXED** | 19 tests in `test_runtime_delegated_budget.py` |
| K | preregistered-design visibility (`planner@5`) | **VERIFIED_FIXED** | `sciencecontext.preregistration_view`, planner block |
| L | repeated-design suppression by hypothesis | **VERIFIED_FIXED** | `preregistration_view` carries `tests_hypothesis`; planner dispatches on it, not on the digest |

None was reimplemented.

**Zero-write invariant, measured.** A 690-line inventory of
`~/.local/state/research-os`, `~/.local/share/research-os`,
`~/.config/research-os`, `~/.cache/research-os` and the science capsule, taken
before and after a full suite run:

```text
diff state_before.txt state_after.txt   →   IDENTICAL
science repo git status                 →   clean
```

## 3. Gap A — bounded finding content now reaches proposals

**The defect, in the words of the system that hit it.** A
`critique_hypotheses` cycle wrote an 10,960-byte artifact holding six
substantive alternative explanations, recorded a finding, and linked the
provenance. The finding's only text was the handler's sentence:

```text
6 alternative explanation(s) for 5 target(s)
```

44 characters. The proposal grounded in it says so in its own `PR-002`:

> Only that summary is available to this proposal; the text of the six
> alternatives is not.

and lists `full text of FIND-20260917T183904Z-9b053d6e` under
`required_inputs`. The worker refused to reason from content it could not see —
correct behaviour, incomplete architecture.

**The fix.** A bounded, producer-authored excerpt travelling in the existing
structured result, not a parallel evidence system:

```text
handler builds its result
  → authors data[EXCERPT_KEY] from the structure it just built
  → _record_finding lifts it (reads no artifact, opens no file)
  → RuntimeFinding.excerpt, ≤ 2000 chars, in the digest when non-empty
  → runtime_findings.excerpt (migration 0016)
  → SuppliedFinding.excerpt → fenced, labelled NONCANONICAL, in the
    grounding digest
  → planner sees ≤ 400 chars; proposal worker sees ≤ 1500
```

Producers: `critique_hypotheses`, `propose_hypotheses`, `review_science`,
`search_literature`, `parse_literature`, `interpret_results`,
`derive_mathematics`. The generic prompt layer still reads no artifacts.

**Backward compatible by construction.** The excerpt enters the digest only
when non-empty, so every finding recorded before this keeps the digest it was
cited under — a proposal already resting on `FIND-…9b053d6e` still resolves to
it.

18 behavioural tests in `tests/test_runtime_finding_excerpts.py`, built on a
real-shaped critique rather than a trivial fixture.

## 4. Gap B — scientific targets route by how they could be settled

**The defect.** The live runtime designed **six experiments for HYP-0002**, a
biconditional about when an infimum is finite, at about four dollars over
ninety minutes. `planner@5` could not stop it: each design had a different spec
digest, so digest deduplication saw six distinct pieces of work. They were six
distinct pieces of the *wrong* work.

```text
unresolved hypothesis  ≠  empirical experiment required
```

**The design, and why it is the smallest one.** A new
`research_os.runtime.adjudication` module classifies a target from *the
project's own statement and falsification clause*, weighting the falsifier
2× — because the falsifier is the sentence in which the researcher already
wrote down what would settle the thing. Deterministic, never from a model, and
it carries the literal words it matched so a person can check it in one line.

Nothing canonical changes. Adding an `adjudication:` field to `Hypothesis`
would have meant a schema change, a migration of every capsule on disk, and a
new way for an automated system to write a scientific-sounding label into files
a person owns.

```text
EMPIRICAL              measurement decides it
MATHEMATICAL           derivation or counterexample decides it
NOVELTY_OR_LITERATURE  primary sources decide it
MIXED                  both, in order: theory or literature first
DIAGNOSTIC             observing this implementation, and only that
UNDETERMINED           not enough signal — blocks nothing
```

**Verified against the real capsule, with no tuning:** all seven thesis
hypotheses classify correctly on the text as committed.

```text
HYP-0001  empirical      wall time, repetitions, median, preregistered
HYP-0002  mathematical   if and only if, infimum, is exactly
HYP-0003  mixed          provably | published, known
HYP-0004  mathematical   prove, for every, there exist, upper/lower bound
HYP-0005  empirical      wall times
HYP-0006  diagnostic     the implemented, default tolerance, a run that
HYP-0007  empirical      profile, wall time, preregistered, instance regime
```

**Enforcement is deterministic, not advisory.** Two halves:

- `planner@6` carries an `ADJUDICATION` block so it routes correctly the first
  time;
- `validate_plan` refuses `design_experiment` when *nothing* the plan addresses
  could be settled by measurement, and refuses `derive_mathematics` for a
  target this project already holds a derivation finding for — by proposition,
  not by digest, which is the lesson `planner@5` had to learn.

Refusals name the action that *would* answer the question, because the refusal
text is what the successor cycle reads.

**Somewhere to route to.** `derive_mathematics` (A0, `READ_REPO`) with a
`deriver@1` prompt whose outcome enum is deliberately
`DERIVED | REFUTED_BY_COUNTEREXAMPLE | NOT_DERIVABLE_AS_STATED | INCOMPLETE` —
there is no `SUPPORTED`, because a deriver that could report partial support
would be reporting an experiment it did not run. The schema *requires* a
numerical witness to state what it would and would not establish.

43 tests across `tests/test_runtime_adjudication.py` (classifier, including
targets from an unrelated domain) and
`tests/test_runtime_adjudication_routing.py` (the §4.4 regression cases through
the real cycle graph).

**A defect found and fixed in this work.** The first implementation called
`kernel.object()` per target, and each of those is a full parse-and-validate of
the whole capsule — turning one planning call into N validations and roughly
tripling suite wall time. Now one `by_id()` per call, with a test that counts
validations.

## 4a. The science-context lifecycle, end to end

`tests/test_runtime_science_lifecycle.py` runs the whole path through the real
cycle graph rather than asserting its seams individually:

```text
action -> immutable artifact -> typed finding -> bounded excerpt
       -> next planner / proposal context -> grounded proposal
       -> human boundary -> canonical state only if a person acts
```

and pins the four failure modes this project actually exhibited as impossible:
work that was done being described as missing, a target that needs
adjudication being reported only as untested, a settled target being worked
again, and a critique reduced to a count.

The sharpest assertion in it is that a derivation reporting `DERIVED` — the
closest this system comes to an automated claim of proof — leaves the
hypothesis `active`, the frontier unchanged, and the capsule byte-identical.

**What it covers and what it does not.** Four action kinds run end to end
through the graph: critique, derivation, inspection and frontier assessment.
The literature and experiment-interpretation producers author excerpts and are
covered by unit tests, but they are *not* exercised end to end here — they need
a literature store and a completed external job respectively, and standing
those up inside this test would have made it a fixture exercise. That is a real
gap in this file's coverage rather than a claim about the architecture, and it
is worth closing when either path is next touched.

## 5. Sandbox security-eligibility gate

Four facts, no longer one boolean:

```text
executable present       the binary is on PATH
namespace capability     it ran, and got a namespace with a uid map
security-eligible        its version is at or above the known floor
containment validated    the adversarial suite ran against it, and held
```

`available_backend()` returns a backend only when namespaces **and**
eligibility both hold. Validation is deliberately excluded from that
conjunction — gating ordinary operation on it would mean a fresh host could
never run the suite that would validate it — and is reported separately, keyed
to the binary's **content hash**, so an upgrade correctly invalidates it.

On this host:

```text
bubblewrap   /usr/bin/bwrap   0.9.0-1ubuntu0.1, NOT setuid
             namespaces_ok = False   (AppArmor userns restriction)
             security_eligible = False  (CVE-2026-87766, floor 0.12.0)
             available = False
systemd-run  present; directives silently ineffective; never selected
podman       not installed, no backend implemented
docker       not installed, no backend implemented
```

23 tests in `tests/test_sandbox_eligibility.py`, covering all five host
configurations §7 names, plus a test that the floor logic mentions no
distribution by name.

## 5a. The vendor-backport follow-up, and what it caught

After this report was first written the host was upgraded to
`bubblewrap 0.9.0-1ubuntu0.3` on the reasonable belief that Ubuntu had fixed
CVE-2026-87766 in `0.9.0-1ubuntu0.2` and that a later version would therefore
also be fixed. The eligibility gate was to be taught about vendor backports so
it would stop reporting a false negative.

The backport support was the right thing to build, and it is built. What it
caught on the way is that the premise was inverted for this particular version:

```text
0.9.0-1ubuntu0.1   unfixed
0.9.0-1ubuntu0.2   FIXED     USN-8779-1
0.9.0-1ubuntu0.3   UNFIXED   "SECURITY REGRESSION: Incompatibility with
                             Flatpak (LP: #2167621) - debian: Drop
                             CVE-2026-87766"
```

Three independent confirmations: the changelog in
`/usr/share/doc/bubblewrap/changelog.Debian.gz` on this machine; the installed
binary at 72160 bytes with no `safe_openat`, byte-size identical to the
unpatched `0.9.0-1ubuntu0.1`; and the upstream and Flatpak issues
(`containers/bubblewrap#801`, `flatpak/flatpak#6830`) describing the CUPS
symlink regression that caused the revert.

A `>= 0.9.0-1ubuntu0.2` rule would have marked this host eligible. That is the
exact false positive the gate exists to prevent, and it would have been
produced by trusting a version ordering instead of a changelog — so the table
holds intervals and requires positive evidence per version range.

The practical position is unchanged and slightly worse than before: there is
now **no installable Ubuntu bubblewrap carrying the fix**, because
`0.9.0-1ubuntu0.2` has been superseded out of the archive. Upgrading does not
help; the options in `docs/CONTAINMENT_OPTIONS.md` still apply.

## 6. HUMAN_ROOT_ACTION_REQUIRED

`docs/CONTAINMENT_OPTIONS.md` has the full audit. In short:

- **Ubuntu noble has no fix.** Only `0.9.0-1ubuntu0.1` is published; the Ubuntu
  CVE page lists noble as *Vulnerable*; `noble-backports` carries nothing.
- **No other backend exists** in this build.
- **Debian has the fix**: trixie `0.12.0-1~deb13u1`, sid/testing `0.12.0-1`.
  noble's libc6 2.39, libcap2 2.66 and libselinux1 3.5 satisfy its constraints.
- Upstream `v0.12.0` tarball, sha256
  `9760d007363e3abba7c747489910f9f82d9fca53ba3bd3282e396fa3c97a3314`, signed by
  the upstream maintainer.

Three options with exact commands, expected output and rollback: rebuild
Debian's source package on noble (recommended), build upstream from verified
source, or wait. **No `sudo` was executed.**

**The ordering matters and is now enforced in three places.** Installing
`deploy/apparmor-bwrap` *first* would grant the vulnerable binary the namespace
it needs and leave the escape intact — strictly worse than today. The probe
declines it, the doctor reports `PRESENT_BUT_UNACCEPTABLE`, and the profile's
own header refuses the install order. The profile's acceptance test was also
corrected: it used `unshare --user --map-root-user true`, which tests a
different binary under a different profile and can pass while bwrap still
fails.

## 6a. What reconciling the proposals actually found

`docs/PROPOSAL_CROSSWALK.md` has the item-by-item table. Three findings are
worth pulling out here, because none was anticipated.

**The two proposals rest on different commits, and the older one is stale in a
way that matters.** Proposal A was written while `EXP-0001` was still pending —
its own rationale says "I did not choose ... `interpret_results` because
EXP-0001 is listed as pending rather than finished". Sixteen commits and 3221
insertions later, EXP-0001 has completed 30/30 and `EVI-0002` records it. Most
of A's items are not wrong; they are *answered*.

**The repository has moved further than either proposal knows.** At proposal
B's own base commit, `docs/2026/SCIENTIFIC_REPORT.md` reports HYP-0002 as
derived and certified on 30 visited duals, HYP-0004's first clause as proved,
HYP-0003 as supported by source reading with the trajectory trace not run, and
HYP-0006 as still open. `docs/2026/LITERATURE.md` closes the novelty gate on
the selection rule against a named 2000 paper: "NOT NOVEL — this is the primary
gate and it does not pass". Neither proposal cites either document.

**And none of it is capsule state.** The capsule holds zero Claims, and the
report says it should: "No Claim has been accepted. The capsule contains none
and should not." So the frontier is right to keep listing those hypotheses as
active, and a careful Markdown document in `docs/` is not scientific state.
That is the distinction the whole architecture exists to hold, and on this
project it is holding — which is the most load-bearing thing this audit
observed, and it was observed rather than arranged.

The practical consequence is that the decision now in front of the researcher
is not the one either proposal framed. It is: *what should happen to a body of
derivation and literature work that lives outside the capsule?* The crosswalk
puts that first.

## 7. What is blocked, and why the release status does not move

| mission section | status |
|---|---|
| §10 prove actual containment | **BLOCKED** — needs a patched backend, which needs root |
| §14–15 human→automatic continuation | **BLOCKED** — needs a genuine human scientific decision |
| §16–23 the scientific programme | **BLOCKED** — §15 forbids performing it manually; it must follow the human decision |
| §24 durable daemon | **BLOCKED** — §24 gates it on human-continuation acceptance |
| §25 Slurm / cross-provider | **BLOCKED** — downstream of the above |

`AUTONOMOUS_RUNTIME_BETA` is retained. Containment is unproven and real
human→automatic continuation is untested, and §29 makes both prerequisites for
the release candidate. Nothing here upgrades that.

## 8. The two gates

**`HUMAN_ROOT_ACTION_REQUIRED`** — `docs/CONTAINMENT_OPTIONS.md`.

**`READY_FOR_HUMAN_SCIENTIFIC_DECISION`** — `docs/PROPOSAL_CROSSWALK.md`.

The headline of the second: the two outstanding proposals rest on **different
commits**, and the older one is stale in a way that matters. It was written
while `EXP-0001` was still pending — its own rationale says so — and sixteen
commits later EXP-0001 has completed 30/30 and the pricing and unit-ball
questions it proposed to adjudicate have been substantially adjudicated.

Both proposals remain immutable and on disk. No third proposal was created. No
item was promoted or declined.
