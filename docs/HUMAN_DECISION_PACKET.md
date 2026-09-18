# THE MINIMUM HUMAN SCIENTIFIC DECISION

## Recommended: promote one item

  proposal    PROP-19700101T000000Z-33ec307e
  item        PR-009
  kind        question        basis  prospective
  target      HYP-0004, Q-0002
  importance  medium          confidence  high
  basis       current -- the objects it rests on are unchanged
  base commit 783d4d2  (= science repo HEAD; nothing has moved under it)

  title
    What is the set of 'reachable' dual points that HYP-0004 quantifies over?

## Evidence for

HYP-0004's statement in the capsule reads "There exist **reachable** dual points
at which it exceeds the primal optimum", and its falsification clause reads "for
every dual point the algorithm **can reach**". The capsule defines "reachable"
nowhere. I checked the file.

Three readings give materially different claims:
  (a) the iterates the historical loop produces  -> about this implementation
  (b) any optimal dual of a restricted master   -> about the decomposition
  (c) any dual-feasible point of some master    -> close to vacuous

So PR-005's counterexample search is uninterpretable until one is fixed: a null
result over (a) says nothing about (b). The proposal lists this as blocking
PR-005 and PR-009.

## Contrary evidence / what argues against

Nothing argues that the gap is not real. What argues against *promoting it* is
that it is the smallest of the eleven items and does not advance the science by
itself -- it makes a later test interpretable. That is the reason it was chosen:
the mission asked for the smallest decision that creates a clean causal test.

## Completed work bearing on it

EXP-0001 is complete (30/30 cells). HYP-0002 was DERIVED autonomously at
13:36Z today, in 9 steps, recorded as FIND-20260918T133641Z-882ffcf4 and
reviewed under different-context independence. That finding is **uncited by any
proposal**, so promoting anything also unblocks the runtime to carry it to you.

## Effects

  promote  writes .research/questions/Q-0011.yaml at status `open`
           (verified by a dry run that wrote nothing)
           -> capsule digest moves; frontier `open_questions` gains Q-0011
           -> researchd observes within capsule_observe_seconds (30)
           -> CAPSULE_CHANGED -> advance_objective
  decline  records the decision and the reason, writes nothing to the capsule;
           PR-005 stays uninterpretable; the item stops being offered
  defer    nothing happens; the runtime stays idle, correctly

## Recommendation and confidence

Promote PR-009. Confidence high that it is correct and safe: it records a gap
that demonstrably exists and asserts nothing about the science.

## The command

  cd /home/nicacevedo/Documents/Github/column-generation-for-large-scale-feature-selection
  researchctl propose promote PROP-19700101T000000Z-33ec307e --item PR-009
  git add .research/questions/Q-0011.yaml
  git commit -m "Record what HYP-0004 quantifies over"

`promote` writes the file and does not commit. researchd observes files rather
than commits, so it reacts to the promote; the commit is what makes it canonical.

## Expected causal successor -- MEASURED, and read this before deciding

This project has **five distinct parked objectives**:

  RRUN-20260918T133221Z-182e75fc  cycle 1  DONE_FOR_NOW
  RRUN-20260917T185643Z-cd0b74fa  cycle 2  DONE_FOR_NOW
  RRUN-20260917T184836Z-1ed3f353  cycle 3  WAITING_FOR_SCIENTIFIC_DECISION
  RRUN-20260917T190031Z-47a99872  cycle 3  DONE_FOR_NOW
  RRUN-20260917T190433Z-a0be7902  cycle 3  DONE_FOR_NOW

`parked_objectives` is `distinct on (objective)` -- one successor per objective,
which is the documented rule and not a duplication bug. So one promotion is
expected to open **up to five** successor cycles, each ~$0.70, not one.

If you want the causal test to be unambiguously one-decision-one-successor,
cancel the four stale objectives first. That stops operational work and writes
no science:

  researchctl runtime cancel RRUN-20260917T185643Z-cd0b74fa
  researchctl runtime cancel RRUN-20260917T184836Z-1ed3f353
  researchctl runtime cancel RRUN-20260917T190031Z-47a99872
  researchctl runtime cancel RRUN-20260917T190433Z-a0be7902

That is your call. Either way I will record the exact count.
