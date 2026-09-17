# Two real proposals, reconciled

```text
Status:  READY_FOR_HUMAN_SCIENTIFIC_DECISION
Project: cg-sparse-regression
Capsule: /home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection
HEAD:    783d4d2e249acd8eb19229ec975ab2b814a63452
Audited: 2026-09-17
```

Two proposals are outstanding against this project. Neither has any item
promoted. Both are immutable and both stay on disk — nothing here retracts,
edits or supersedes a stored proposal, and no third proposal has been created.

This document is the crosswalk §12 asks for, plus the basis diff §13 asks for.
It recommends. It decides nothing.

## The fact that governs everything below

The two proposals were built on **different commits of the scientific
repository**, and one of them is stale.

```text
PROP-19700101T000000Z-b9c26fcd   created 2026-09-17T09:57:27Z   base 9d5474e89f8a
PROP-19700101T000000Z-33ec307e   created 2026-09-17T18:54:15Z   base 783d4d2e249a  == HEAD
```

Sixteen commits and 3221 insertions separate them. Among them:

| commit | what landed |
|---|---|
| `6094805` | pricing claims adjudicated on the duals the method actually reaches |
| `cdf90ae` | the unit-ball verdict, including the part that weakens it |
| `7c26f3d` | repairs from an independent mathematical review |
| `31ba443` | EXP-0001's verdict computed from its frozen rule |
| `4b93610` | **EXP-0001 completes: 30/30 cells** |
| `a038230` | acting on the adversarial review of the empirical case |
| `891724a` | two claims the completed run falsified, corrected |

New files: `docs/2026/SCIENTIFIC_REPORT.md` (729 lines),
`docs/2026/UNIT_BALL_VERDICT.md` (210 lines),
`scripts/adjudicate_pricing.py`, `scripts/analyse_benchmark.py`,
`src/cg2026/duals.py`, `results/2026/EXP-0001.jsonl`.

Capsule state now: `EXP-0001 status: completed`; `EVI-0002: "EXP-0001 complete
run, 30 of 30 preregistered cells"`.

**Proposal A's own rationale says why it did not interpret results:**

> I did not choose ... `interpret_results` because EXP-0001 is listed as
> pending rather than finished.

That premise is no longer true. Proposal A reasons throughout from "EXP-0001 is
specified, not run" (PR-001 says so in as many words). Its central open gate,
G1, asks whether any regime survives — a question EVI-0002 has since answered
on 30 of 30 preregistered cells.

> **Proposal A is not wrong. It is answered.** Most of its items were superseded
> by work the project itself completed in the nine hours after it was written.

## Item-by-item crosswalk

`A` = `PROP-…-b9c26fcd` (12 items) · `B` = `PROP-…-33ec307e` (11 items)

| A | B | current canonical objects | evidence now available | verdict | recommended action |
|---|---|---|---|---|---|
| **A/PR-001** the finding is provenance-only | B/PR-002 | FIND-…095202Z | — | **OBSOLETE** | None. Correct when written; it interprets an inspection finding that a later critique finding supersedes in usefulness. |
| **A/PR-002** two of four hypotheses analytically decidable, two unfalsifiable as worded | **B/PR-003**, B/PR-009 | HYP-0002/3/4/7, ASM-0001 | `UNIT_BALL_VERDICT.md`, `adjudicate_pricing.py` | **SUPERSEDED** | Prefer **B/PR-003**. Same insight, stated as the category error it is, and B/PR-009 isolates the one genuinely underdetermined part ("reachable") instead of calling two hypotheses unfalsifiable. |
| **A/PR-003** HYP-0003 restated as a trajectory-coincidence hypothesis | B/PR-004(b) | HYP-0003, Q-0003, Q-0004 | — | **COMPLEMENTARY** | **Keep A/PR-003.** It is the sharper *statement* of the mechanism claim. B/PR-004 folds the same content into a combined derivation item. See the note below. |
| **A/PR-004** HYP-0007 restated measurably | B/PR-006 | HYP-0007, ASM-0002/3 | `results/2026/profile.json` | **DUPLICATE** | Prefer **B/PR-006** — it is scoped to the frozen grid and names the three timing buckets. A/PR-004 is the same claim without the protocol. |
| **A/PR-005** analytic + numerical adjudication of HYP-0002 and HYP-0004 | B/PR-004, B/PR-005 | HYP-0002, HYP-0004 | `scripts/adjudicate_pricing.py`, `src/cg2026/duals.py`, `UNIT_BALL_VERDICT.md` | **SUPERSEDED (largely performed)** | Do not re-authorise as written. The project has since done substantially this work. Reconcile what `UNIT_BALL_VERDICT.md` establishes against **B/PR-004** and **B/PR-005** before authorising any remainder. |
| **A/PR-006** working-set equivalence trace vs a maximum-violation reference | B/PR-004(b) | HYP-0003, Q-0003, Q-0004 | `SCIENTIFIC_REPORT.md` §O, `LITERATURE.md` N4 | **CURRENT as a witness, not as the decisive instrument** | **Authorise if a witness is wanted.** The trace itself is unrun — the report says so in as many words. But the thing it was to establish has substantially been established by derivation and source reading, and the novelty half is closed. See the correction below before authorising. |
| **A/PR-007** cost-attribution rider on EXP-0001 | B/PR-006 | HYP-0007 | EXP-0001 has completed | **OBSOLETE as a rider** | The run it would have ridden on is finished. If the profiling is still wanted, it is **B/PR-006** (a separate instrumented re-run), not a rider. |
| **A/PR-008** consolidated gate G1: any competitive regime? | B/PR-001, B/PR-011 | Q-0001, Q-0008, Q-0009, HYP-0001 | **EVI-0002: 30/30** | **ANSWERED** | Do not open. EVI-0002 answers it on the preregistered grid; B/PR-001 gives the two-layer reading of *how* it is answered. |
| **A/PR-009** consolidated gate G2: does any theory remain? | B/PR-003, B/PR-008 | Q-0002, Q-0003, Q-0004, Q-0007 | partial | **CURRENT, narrowed** | Keep as the open theoretical gate, but note it now turns on A/PR-006's outcome plus a literature-priority determination, not on A/PR-005 (done). |
| **A/PR-010** consolidated gate G3: extensions, and should it open at all? | **B/PR-008** | Q-0005, Q-0006, Q-0010 | — | **SUPERSEDED** | Prefer **B/PR-008**: same gate, stated as a novelty question about Elastic Net rather than as a scheduling question, which is the form the literature can actually answer. |
| **A/PR-011** conditional negative close | **B/PR-011** | HYP-0001, HYP-0003, EXP-0001 | EVI-0002 | **SUPERSEDED** | Prefer **B/PR-011**. A/PR-011's antecedent is conditional on a G1 result that has since arrived; B/PR-011 states the verdict on evidence now in the capsule. **Both remain conditional — see the warning below.** |
| **A/PR-012** the two-branch decision | B/PR-011 | EXP-0001, Q-0001 | EVI-0002 | **OBSOLETE** | Branch A ("authorise the compute for EXP-0001") describes a run that has completed. The live decision is the one in §"What to decide" below. |
| — | **B/PR-001** 30/30 is mostly a certification failure | HYP-0001, HYP-0006, EVI-0002, DEC-0001 | EVI-0002 | **CURRENT** | **Consider promoting as an evidence interpretation.** It is the most load-bearing new reading in either proposal. |
| — | **B/PR-002** the critique's content is not visible to this proposal | HYP-0002/3/4/6/7 | FIND-…183904Z | **RESOLVED BY THE RUNTIME** | Do not promote as written. The architecture defect it reports is fixed (see below); its second half — that an alternative explanation cannot refute a mathematical proposition — is real and is carried by B/PR-003. |
| — | **B/PR-007** repairing the certificate converts 26 cells without changing the verdict | HYP-0006, HYP-0001, EXP-0001 | EVI-0002 | **CURRENT** | **Authorise as a hypothesis** if the certificate mechanism is to be settled. It is falsifiable and cheap. |
| — | **B/PR-010** instability tracks tolerance and conditioning, not support size | Q-0007, HYP-0006, EVI-0002 | EVI-0002 (71 ok / 12 time_limit / 7 SolverError) | **CURRENT** | Optional. Lowest decision value of B's open items. |

## Correction: the repository has moved further than either proposal knows

Both proposals treat the mechanism and novelty questions as open. At
`783d4d2` — proposal B's own base commit — the repository contains
`docs/2026/SCIENTIFIC_REPORT.md` and `docs/2026/LITERATURE.md`, which report
them as substantially closed. Neither proposal cites either document.

From `SCIENTIFIC_REPORT.md` §O, verbatim:

| object | what the report says |
|---|---|
| `HYP-0002` | supported, **derived and certified** on 30 visited duals |
| `HYP-0003` | supported **by source reading**; the trajectory trace the proposal asks for **is not run** |
| `HYP-0004` | **first clause proved**; existence clause supported on unstandardised data, not observed on standardised |
| `HYP-0006` | **still open** |
| `HYP-0007` | supported, at a corrected share |

And from `LITERATURE.md` N4, on the selection rule that HYP-0003 is about:

> **NOT NOVEL — this is the primary gate and it does not pass.**
> Osborne–Presnell–Turlach `doi:10.1093/imanum/20.3.389` add exactly the
> maximum-violation coordinate; strong rules screen on it; Gap Safe, Celer and
> Blitz build working sets from it.

The report's own recommendation is **no paper on the computational
contribution**, with the tracks assessed A–F.

**What this changes, and what it does not.**

It changes the *ordering* of the decision. The researcher's stated first
direction — settle the working-set equivalence — is not an open question
followed by a literature check; the literature gate has already closed against
novelty on a named 2000 paper, and the mathematics has been derived. What
remains of A/PR-006 is a preregistered **witness**: a trajectory that would
expose a divergence if one exists. That is worth running, and it is not the
thing that decides the question.

It changes nothing canonical. **The capsule holds zero Claims**, and the
report says it should:

> No Claim has been accepted. The capsule contains none and should not.

So the frontier is right to keep listing these hypotheses as active, and a
document in `docs/` is not capsule state however careful it is. This is the
distinction the whole architecture exists to hold, and it is holding.

**The consequence for B/PR-011.** Its recommended verdict was conditional on
two things. Condition 1 — the mechanism question resolving against novelty —
is now substantially met by `LITERATURE.md` N4 and `SCIENTIFIC_REPORT.md` §E.
Condition 2 — no genuinely novel Elastic-Net result surviving — is addressed by
the report's TRACK C ("the closed form is the soft-thresholding operator") and
TRACK D ("generic regularisation-based stabilisation of column generation is
long established"), though neither is in the capsule either.

That makes B/PR-011 materially more supportable than it was when written. It
does **not** make it automatic: promoting it asserts a closing verdict, and the
evidence for it currently lives in Markdown that no Review binds. The honest
options are to promote the underlying evidence interpretations first, or to
promote B/PR-011 with the report cited as its basis and a Review recorded
against it.

**What is genuinely still open**, on the report's own accounting:

```text
HYP-0006   termination is never certified        STILL OPEN
           → B/PR-007 is the sharpest form of it
HYP-0003   the trajectory witness                NOT RUN
           → A/PR-006 / B/PR-004(b)
HYP-0004   the existence clause on standardised data   NOT OBSERVED
           → B/PR-005, and B/PR-009 on what "reachable" means
atomic / nuclear-norm generalisation             NOT ASSESSED
           → in neither proposal and in no track A–F
```

The last line is worth noting: the researcher's stated direction includes the
atomic-norm generalisation, and it is the one direction on which this project
holds no documentary position at all.

### The note on A/PR-003 vs A/PR-006 vs B/PR-004

These three are the same scientific question — *is the CG working-set
trajectory the maximum-violation rule?* — at three different levels:

```text
A/PR-003   the hypothesis          what would be true
A/PR-006   the trajectory test     how to witness it
B/PR-004   derivation + witness    a proof, with the test as its witness
```

They are **complementary, not duplicates**, and B/PR-004's framing is the
correct one under the adjudication rule this runtime now enforces: HYP-0003 is
MIXED (a mathematical core plus a literature-priority component), so the
settling instrument is a derivation plus a literature determination, and the
trajectory trace is a *witness* that would expose a divergence — not a proof of
equivalence and not evidence of priority.

## Warning: two conditional statements that must not become Claims

Both proposals close on a conditional. Neither antecedent is fully satisfied.

**B/PR-011** recommends "no methodological contribution survives". Its own
statement keeps this conditional on two things that remain open:

1. the working-set / mechanism question resolving against novelty — **open**,
   it is exactly A/PR-006 / B/PR-004; and
2. no genuinely novel Elastic-Net result surviving a literature and theory
   audit — **open**, it is B/PR-008.

**B/PR-001** establishes a two-layer reading that must be preserved intact:

```text
26 of 30 cells   objective matches the best arm to 1.1e-14 .. 5.9e-10,
                 and the 1e-6 gap CERTIFICATE fails
 4 of 30 cells   fail outright: objective excess 2.3e-04, 8.8e-04,
                 1.7e-01, 9.4e-01, after exhausting the time limit
```

The defensible sentence is:

> the negative computational verdict survives removal of the certificate
> confound, while the mechanism of the certificate failure remains open.

Neither "the certificate failure explains the result away" nor "the method has
no remaining contribution" follows from what is in the capsule today.

## The architecture defect B/PR-002 reported is fixed

B/PR-002 is a proposal item that documents a defect in the system that produced
it:

> Only that summary is available to this proposal; the text of the six
> alternatives is not.

and lists `full text of FIND-20260917T183904Z-9b053d6e` under
`required_inputs`. The critique artifact
(`ad4b961aabb6…`, 10,960 bytes, six substantive alternatives) existed the whole
time; the finding carried a 44-character count of it.

Findings now carry a bounded, producer-authored excerpt alongside the summary.
A proposal worker grounded in that finding would now read the alternatives
themselves. **This does not retroactively repair
`PROP-…-33ec307e`** — it was built when the excerpt did not exist, and it is
immutable. What it means is that the *next* proposal against this project will
not have that hole, which is one argument for letting the runtime produce a
current-basis proposal rather than promoting from either of these.

## What to decide

The smallest set of decisions that authorises the next scientific programme.

**Decision 1 — how to get a current-basis packet.** Recommended: let the
runtime build one. Both existing proposals were written before the excerpt fix,
and A was written before EXP-0001 completed. A successor cycle would read the
completed EXP-0001, EVI-0002, the critique's actual content, and the new
adjudication routing — and would route HYP-0002/0003/0004 to derivation rather
than to a seventh experiment design. Promoting from B instead is defensible;
promoting from A is not, without the basis diff above in hand.

**Decision 2 — decide what to do about work already done outside the capsule.**
This is the decision the crosswalk did not expect to find. `SCIENTIFIC_REPORT.md`
and `LITERATURE.md` contain derivations, a closed novelty gate and a
recommended verdict, and none of it is capsule state. Either promote the
evidence interpretations that carry it (B/PR-001 is the best-formed), or
explicitly decide that the documentary record is where it stays. Leaving it
implicit is the one option that degrades over time.

**Decision 3 — authorise what is genuinely open**, in the order the evidence
suggests: B/PR-007 (HYP-0006, the termination certificate — the only hypothesis
the report calls still open), then the A/PR-006 trajectory witness, then
B/PR-005 / B/PR-009 on HYP-0004's existence clause. Note what the runtime will
now refuse: a plain `design_experiment` against HYP-0002 or HYP-0004, because
no measurement settles them.

**Decision 4 — the Elastic-Net and atomic-norm gates (B/PR-008).** Literature
and theory first, and note that EN already has a documentary position (TRACK C
and D) while the atomic/nuclear-norm generalisation has none. If either is to
be opened, the atomic-norm direction is the one on which this project currently
knows nothing.

**Decision 5 — the closing claim.** A/PR-011 is superseded. B/PR-011 is now
materially more supportable than when it was written, because the report has
closed the novelty gate its first condition names. It is still a *conditional*
close and its evidence is not yet capsule state, so promoting it today asserts
a conclusion ahead of the record that would support it. The clean sequence is
Decision 2 first, then this.

## Commands

Read-only, to check any of the above:

```bash
cd /home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection
uv run --frozen --project /home/nicacevedo/research/research-os-rc researchctl propose list
uv run --frozen --project /home/nicacevedo/research/research-os-rc researchctl propose show PROP-19700101T000000Z-33ec307e
uv run --frozen --project /home/nicacevedo/research/research-os-rc researchctl propose show PROP-19700101T000000Z-b9c26fcd
uv run --frozen --project /home/nicacevedo/research/research-os-rc researchctl status
```

Deciding — **interactive, and the researcher's alone.** No automated agent may
run these or answer their prompts:

```bash
# Promote a single item into a draft capsule object.
researchctl propose promote PROP-19700101T000000Z-33ec307e --item PR-001

# Decline an item, with a reason that is recorded.
researchctl propose decline PROP-19700101T000000Z-b9c26fcd --item PR-008 \
    --reason "answered by EVI-0002; EXP-0001 completed after this was written"

# Decline everything still undecided in the stale proposal.
researchctl propose decline PROP-19700101T000000Z-b9c26fcd \
    --reason "stale basis: written at 9d5474e8, before EXP-0001 completed"
```

### Expect the stale-basis guard on Proposal A

`researchctl propose promote` recomputes a proposal's basis before it writes
anything, and refuses when a scientific object the proposal cited has changed
since it was written. Proposal A cites `EXP-0001`, whose status moved from
`specified` to `completed`, and `EVI-0002`, which did not exist when A was
written. So a promotion from A will fail closed, and that is the guard working.

The flag that overrides it exists and should be used sparingly:

```bash
researchctl propose promote PROP-19700101T000000Z-b9c26fcd --item PR-XXX \
    --accept-stale-basis
```

**What changed, so that acceptance can be informed rather than blind:** the
sixteen commits in the table at the top of this document, of which the material
ones are that EXP-0001 completed with 30/30 cells, that EVI-0002 now records
it, and that the unit-ball and pricing questions A/PR-005 proposed to
adjudicate have been substantially adjudicated in `UNIT_BALL_VERDICT.md` and
`scripts/adjudicate_pricing.py`.

For most of A's items stale-basis acceptance is **not** safe, because the
staleness is the whole point — the item asks for work that has since been done,
or opens a gate that has since been answered. The one item where it is
defensible is **A/PR-006**, whose subject matter no commit in that range
touched: no trajectory comparison against a maximum-violation reference exists
anywhere in the repository. Even there, B/PR-004's framing is preferable,
because it routes the mathematical half to a derivation instead of to a test.

A promotion or a decline changes the capsule, which the observer sees, which
starts exactly one successor cycle per parked objective. Nothing needs to be
enqueued by hand.
