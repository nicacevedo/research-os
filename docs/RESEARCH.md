# Research runs

A research run is the layer that turns "find out whether X" into the things
Research OS already knows how to do, in an order a deterministic controller
chose, against a budget it cannot exceed, stopping wherever a person needs to
decide something.

It owns no worker of its own. Each task kind is dispatched to a controller that
was built and tested separately, so the guarantees those layers make are the
guarantees a research run makes.

```
researchctl research start PROJECT --goal "..."     plan, and stop
researchctl research run RUN_ID                     execute the plan
researchctl research answer RUN_ID --answer "..."   unblock a checkpoint
researchctl research status RUN_ID                  where it is
researchctl research report RUN_ID [--events]       everything it did
researchctl research list                           every run
researchctl research cancel RUN_ID                  stop an unfinished run
researchctl research cleanup RUN_ID                 release its worktrees
researchctl research resume RUN_ID                  recover an interrupted run
```

See [OPERATIONS.md](OPERATIONS.md) for `doctor`, `storage`, and recovery.

## The shape of a run

```
goal
  -> research planner (context-only, no tools)
  -> typed plan: a forward-only DAG of tasks
  -> deterministic dispatch, one task at a time
  -> READY_FOR_HUMAN, WAITING_FOR_HUMAN, FAILED, or CANCELLED
```

The planner sees the project's scientific state, the repository's shape, any
insights promoted from other projects, and the list of experiment commands the
researcher has declared. It returns a small plan. Everything the prompt asked
for is then re-checked locally, because the prompt is a request and the
validator is the rule.

## Task kinds

| kind | what runs it | writes? | can spend compute? |
| --- | --- | --- | --- |
| `literature` | the literature service | no | no |
| `analysis` | the automation controller, analyst role | no | no |
| `proposal` | the proposal controller | no | no |
| `code` | the automation controller, coder role | isolated worktree | no |
| `experiment` | the experiment controller | no | **yes** |
| `paper` | the paper controller | isolated worktree | no |
| `human_checkpoint` | nobody; the run stops | no | no |

The dispatch table is a dictionary keyed by task kind with no default branch. A
kind with no handler is refused when the plan is validated, and a test asserts
the table covers the enum exactly, so adding a kind without wiring it up fails
in the test suite rather than in somebody's run.

## What stops a run

**A human checkpoint.** The plan may place one anywhere. The run reaches it,
records the question, and moves to `WAITING_FOR_HUMAN`. Nothing after it runs.
`researchctl research answer` records the answer and lets the run continue;
`--stop` records a refusal and ends the run instead. A researcher who declines
has decided something, and the run honours it rather than going on.

**An unauthorised experiment.** Experiments do not execute unless the run was
started with `--execute-experiments`. Without it, an experiment task still
resolves its exact command from the researcher's declarations and then stops,
so the report says precisely what *would* have run:

```
T-003  [experiment]  skipped
  outcome  not executed: this run was not authorised to spend compute.
           It would have run: python3 fit.py --seed 7
```

**A budget.** Model calls, write tasks, experiments, cluster submissions and
wall clock have separate counters, because the resources are not
interchangeable. A plan the run could never pay for is refused at planning time
rather than discovered at the last task, and each spend is charged against the
value on disk before anything happens — so a crashed run's file is an accurate
account of what it actually spent. The wall-clock bound stops the run from
starting another task rather than killing one mid-flight, because a task killed
in flight is how a run ends up holding a worktree nobody knows about.

**A crash.** If the process dies, the run's state becomes `INTERRUPTED` rather
than a stale `EXECUTING`, and only `researchctl research resume` moves it — a
deliberate act in which a person decides what happens to the task that was in
flight.

## What a run cannot do

Everything the rest of Research OS refuses, a research run also refuses,
because it delegates rather than reimplements:

- No task may write under `.research/`. Canonical scientific files are written
  by `init-project`, by `review`, and by an explicit human promotion, and by
  nothing else. The refusal is on the task model itself, unconditionally.
- A write-capable task works in a dedicated Git worktree, never the canonical
  checkout, and its scope is enforced from the observed diff rather than from
  what the worker said it did.
- An experiment command the researcher has not declared in
  `~/.config/research-os/experiments.yaml` cannot be run. A plan naming one is
  refused before execution starts, and the refusal names the declared commands.
- One bounded repair per work item, capped by the budget field itself.
- Nothing a run produces is accepted science. Proposals are suggestions,
  evidence packets are candidates, drafts are prose. A Claim becomes accepted
  only through `researchctl review`, which requires an interactive terminal and
  a human.

## Where things live

The run directory holds the record, the plan, every prompt, every model output,
and an append-only event ledger:

```
$RESEARCH_OS_STATE_HOME/research/RR-<timestamp>-<digest>/
  run.json
  events.jsonl
  plan/plan.json
  prompts/INV-0001.txt
  model_outputs/INV-0001.txt
  invocations/INV-0001.json
```

Whatever a task produced lives in the store that owns that kind of thing, and
the research run keeps only the pointer:

| produced by | found with |
| --- | --- |
| `analysis`, `code` | `researchctl auto report RUN-...` |
| `proposal` | `researchctl propose show PROP-...` |
| `experiment` | `researchctl experiment show EXP-RUN-...` |
| `paper` | `researchctl paper show DRAFT-...` |

Deleting a research run directory loses an orchestration record. It loses
nothing scientific, because everything scientific is in Git.

## Trust boundary

Worktree isolation is Git isolation, not an OS sandbox. A `code` task's
acceptance commands execute project code — including code a model just wrote —
with your own operating-system permissions. Run research runs against
repositories you trust, on a machine where that is an acceptable risk.

## When a worker gets it slightly wrong

Two of the failures this system actually hit against a live provider were not
security problems and not bugs in the worker's judgment. They were mechanical:
output that did not match what the controller would accept.

**A plan that is not a plan.** A planner can return something that satisfies
the schema and says nothing: a one-task plan whose summary, title, goal and
query are all the word `test`. This happened against a live provider after
thirteen thousand output tokens of genuine planning, and everything downstream
then ran on it — a literature search for "test" reached three providers and
retrieved sixty works.

It is worth being precise about the cause, because the obvious answers are
wrong. Replaying the archived prompts establishes it: the prompt that first
produced an excellent four-task plan produces `test` on replay, and the prompt
that produced `test` produces an excellent plan on replay. The prompt is not
the variable. Neither is the schema, though it was made less demanding anyway —
requiring only `id`, `kind`, `title` and `goal`, since the rest have defaults
on the task model and the local validators give better messages than a
structured-output retry loop does.

What is actually true is that a provider's structured-output enforcement can
converge on the smallest object that validates, and it does so silently. So the
controller assumes it:

- `assert_plan_says_something` refuses a plan whose summary, title, goal, query
  or question is made entirely of placeholder tokens. Deliberately narrow — a
  terse but real goal is nobody's business but the researcher's, and a
  controller grading prose would be worse than one that does not.
- A refused plan gets one bounded re-ask carrying the validator's exact
  objection, charged against the same allowance everything else uses. A second
  failure ends the run.

**A report that cites a finding it never made.** An analyst may return evidence
whose `finding_id` matches nothing in its own `findings`. The validator refuses
it, and it should: analyst output reaches a downstream worker and a human as
evidence. But the failure is mechanical and the worker is the only thing that
can fix it, so the controller asks once more, quoting the rejected answer back
as data with the validator's exact message.

That re-ask is not leniency. The parser stays fail-closed; the second report is
validated by identical rules; a second failure ends the work order; and the
attempt spends the same bounded-repair allowance the coder has, so a run
configured for no repairs gets none.
