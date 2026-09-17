# Architecture acceptance ledger

The brief's §32, item by item. **Unavailable external capability is never
PASS.** Where something was demonstrated only partly, the row says which part.

Recorded 2026-09-17 against `rc/thesis-pilot`, driving the sparse-regression
project at `/home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection`.

| # | item | verdict | evidence, and what is missing |
|---|---|---|---|
| 1 | a genuine second scientific project | **PASS** | A 2023 master's thesis with eight commits of its own history, nine source documents, and data that is partly lost. Not a fixture: nobody in this session chose its frontier, its failures or its missing files. Materially different from CCAO in subject, in shape and in the fact that its most important finding is a negative one |
| 2 | archival / private-source provenance | **PASS** | `docs/2026/PROVENANCE.md`: nine artifacts with SHA-256, role and metadata; two named files recorded as **absent** rather than worked around; one alias pair identified by decompressed content; four disagreements preserved unresolved |
| 3 | Git-history scientific archaeology | **PASS** | `docs/2026/TIMELINE.md`: every commit classified, and three findings that change what the project is — the model is the LASSO, the whiteboard already proved the central result, and both 2025 ideas were measured by their author and are 4× slower |
| 4 | current literature discovery | **PASS with a stated limit** | 12 topic retrievals and 8 identifier fetches through the literature subsystem, 1 124 works indexed, every entry with a DOI or arXiv id. **No forward citation expansion** — the subsystem exposes search and fetch, not citation traversal — and OpenAlex rate-limited after 1 000 credits, which the tool reported rather than hid |
| 5 | runtime-grounded findings | **PASS** | `FIND-20260917T095202Z-f63a6014`, typed, digested, with provenance edges to eight capsule objects, cited by the proposal |
| 6 | substantive proposal | **PASS** | `PROP-19700101T000000Z-b9c26fcd`: twelve items, three experiments with pass/fail endpoints, a consolidation of ten questions into three gates, and a stated negative close with its antecedents. Its first item says the finding it rests on bears on nothing substantive, which is the opposite of what an eager system writes about its own output |
| 7 | real human promotion / decline | **NOT TESTED** | The run is parked at `WAITING_FOR_SCIENTIFIC_DECISION` and nothing here promoted or declined it. `AGENTS.md` forbids an automated agent answering that prompt, and doing so would have invalidated the test |
| 8 | automatic continuation | **PASS** | Cycle 0 → `DONE_FOR_NOW` → successor `RRUN-20260917T095239Z-42c57607` with recorded parent, opened with no manual command |
| 9 | decline lifecycle | **PASS (mechanism), NOT TESTED (by a human)** | `DeclineRecord`, `declines.jsonl`, `decided_item_ids`, `propose decline`; 16 tests including the runtime's deduplication releasing a declined proposal. No human has run it. **And it is forgeable** by appending to a file outside any repository — closed under required containment, open here |
| 10 | unified check profile | **PASS** | 12 tests across direct automation, runtime coding, resumed coding after process death, and worktree coding, asserting the argv actually executed |
| 11 | hard delegated budget | **PASS with a stated residual** | Reserve before the call; the provider is asked exactly twice under a cap authorising two; six concurrent workers produce one spend. Residual: no provider quotes before it bills, so one call can exceed its ceiling, which then ratchets |
| 12 | provenance surviving pruning | **PASS** | Schema 0015 and 7 tests, including the preregistration lookup through the production reader |
| 13 | real experiment preregistration | **PASS** | `EXP-0001`, frozen and committed before any comparative number existed, bound to its plan by digest |
| 14 | real execution | **PASS** | `researchctl experiment run --execute`, isolated worktree, 210 cells in progress, 61 complete at the time of writing |
| 15 | preregistration → interpretation identity | **NOT TESTED on this project** | The runtime's `run_local_experiment` correctly **refused** — "no preregistered design to run; design_experiment must come first" — and the successor cycle repeated the same choice, so the chain never completed here. The guard working is itself evidence; the identity mechanism's coverage remains the unit tests of the previous cycle |
| 16 | crash / recovery in real project work | **NOT TESTED on this project** | Exercised in the suite with real process death; no crash was induced during the live sparse-regression work |
| 17 | contained execution | **FAIL (environment)** | No containment technology works on this host. High-autonomy execution is **refused**, which is correct and is also the reason this is not a PASS |
| 18 | live Slurm | **BLOCKED** | No scheduler. Eight tests written, never executed |
| 19 | cross-provider review | **BLOCKED** | One provider family. Two adversarial reviews were run in fresh read-only contexts on the **same** family; that is engineering diversity and is not counted as independence |
| 20 | claims traceable to evidence | **PASS** | No Claim is accepted. Every hypothesis in `docs/2026/SCIENTIFIC_REPORT.md` §O carries its status and the measurement behind it, including the one that was falsified |
| 21 | manuscript traceability | **NOT APPLICABLE** | No manuscript, deliberately. §31's gate requires a contribution to survive four audits; none did |
| 22 | scientifically justified pivot / stopping | **PASS** | Three of them. The novelty gate closed on Osborne–Presnell–Turlach 2000; `HYP-0006` was **rejected by its own preregistered rule** after this session wrote it; and the recommendation is TRACK F, no paper. None was steered |

## Summary

```text
PASS                 13
PASS with a limit     2   (4, 11)
PASS in part          1   (9)
NOT TESTED            3   (7, 15, 16)
FAIL (environment)    1   (17)
BLOCKED               2   (18, 19)
NOT APPLICABLE        1   (21)
```

Every NOT TESTED row is a thing that requires either a human or an environment
this host does not have, except row 15, which is a genuine gap in the runtime's
feedback path and is recorded in `docs/RUNTIME.md` §15b.
