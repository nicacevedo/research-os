# Research OS — Design Invariants

These invariants are architectural constraints, not implementation suggestions.

Any proposed change that violates an invariant requires explicit human review and
an intentional update to this document before implementation.

## Core invariants

1. **Local computation before LLM reasoning.**
   Deterministic parsing, indexing, filtering, validation, ranking, caching,
   numerical computation, and bookkeeping should run locally whenever practical.

2. **No continuously thinking agents.**
   Background autonomy consists primarily of deterministic or inexpensive
   monitoring, indexing, validation, and scheduling.

3. **Expensive reasoning is event-triggered.**
   Frontier-model reasoning is invoked only when a task crosses an explicit
   relevance, uncertainty, novelty, scientific-judgment, or review threshold.

4. **Scientific state is project-isolated.**
   Hypotheses, assumptions, claims, experiments, decisions, and interpretations
   belong to their individual research projects.

5. **Literature objects and caches may be globally shared.**
   Papers, normalized metadata, parsed text, embeddings, and citation information
   may be reused across projects.

6. **Git-tracked human-readable files are scientific truth.**
   Important scientific state must remain inspectable, diffable, reviewable, and
   reconstructible from project files and Git history.

7. **SQLite indexes and caches are rebuildable.**
   Databases accelerate lookup and orchestration; they are not the canonical
   scientific record.

8. **No agent approves its own scientific work.**
   Generation, execution, validation, and scientific approval are distinct roles.

9. **External reviewers start from clean context.**
   Independent review should use frozen review packets rather than inheriting the
   generating agent's conversational context.

10. **Every scientific claim must be traceable to evidence.**
    Accepted claims must reference literature evidence, experimental evidence, or
    both.

11. **Every experiment must be traceable to code, configuration, data, and version.**
    Results without provenance are not accepted scientific evidence.

12. **Paid API use requires explicit budgets.**
    Models and external APIs must expose and respect configurable usage limits.

13. **Browser subscription interfaces are not scraped or unofficially automated.**
    Authentication and provider terms are respected.

14. **Agent loops have finite stop conditions.**
    No workflow may recurse or retry indefinitely.

15. **Cross-project knowledge transfer must be explicit.**
    Cross-project reuse happens through deliberately promoted shared knowledge,
    not unrestricted retrieval of another project's private hypotheses or state.

## Architecture boundaries

- Code: `~/research/research-os`
- Project science: individual Git repositories
- Durable shared data: `~/.local/share/research-os`
- Cache: `~/.cache/research-os`
- Configuration: `~/.config/research-os`
- Runtime state: `~/.local/state/research-os`

## Change control

Changes to these invariants require:

1. an explicit rationale;
2. an analysis of scientific and reproducibility implications;
3. human approval;
4. a dedicated Git commit documenting the change.
## Where v1 enforces each invariant

Added when Research OS v1 built the layers above the kernel. The invariants
above are unchanged; this is a map, so a reviewer can check a claim rather than
search for it.

| # | invariant | enforced by |
|---|---|---|
| 1 | local before LLM | Git inspection, hashing, schema validation, file enumeration, filtering, ranking, cache lookup, BM25/FTS, command execution, test comparison and provenance are all ordinary Python; no frontier call does any of them |
| 2 | no continuously thinking agents | nothing runs unless a person runs it; no watcher, daemon, or service ships in v1 |
| 3 | expensive reasoning is event-triggered | every model call is one bounded worker invocation dispatched by the controller, counted against a budget checked before the spend |
| 4 | project-isolated science | digests are project-scoped; `insight` is the only cross-project path and requires explicit nomination and promotion |
| 5 | shared literature | one SQLite store under the data home, shared by every project; project capsules hold interpretations, not corpora |
| 6 | Git-tracked files are truth | no automated worker writes under `.research/`; refused unconditionally on the task model itself |
| 7 | rebuildable indexes | the literature index is reconstructible with `lit index`; deleting runtime state loses no science |
| 8 | no agent approves its own work | `claim_approval()` requires a concluded human `approve` Review binding current digests; `review` needs an interactive terminal and no flag bypasses it |
| 9 | reviewers start from clean context | the automation reviewer and the writing reviewer each receive a frozen packet in a fresh invocation, never the implementer's transcript |
| 10 | claims traceable to evidence | the R0 acceptance gate, unchanged; the paper packet re-establishes qualification rather than trusting the file |
| 11 | experiments traceable | `provenance` is required on an Experiment; an evidence packet records the command, its digest, the config digest and the outputs by content hash |
| 12 | paid use has budgets | separate counters for model calls, write tasks, experiments, cluster submissions and wall clock, each checked before the spend |
| 13 | no unofficial automation | three documented HTTP APIs and the provider CLIs' own non-interactive modes; nothing is scraped |
| 14 | finite stop conditions | one bounded repair, capped by the budget field itself; a forward-only task DAG; no retry loop anywhere |
| 15 | explicit cross-project transfer | an insight is nominated, then promoted by a person, and arrives in a prompt behind a fence that says whose findings it is |

## Change control record: R5 autonomous runtime

Per the change-control section above, a dedicated record. **No invariant above
changed.** What changed is the set of technologies permitted to satisfy them,
recorded in `ARCHITECTURE.md` §12a with the requirement that forced each.

Rationale, invariant by invariant, for the three adoptions (PostgreSQL for
operational state, LangGraph for bounded resumable workflows, one local
`researchd` control process):

| # | invariant | how R5 holds it |
|---|---|---|
| 1 | local before LLM | the frontier, the queue, leases, budgets, digests, failure classification, retry policy and job reconciliation are ordinary Python and SQL; no model is consulted for any of them |
| 2 | no continuously thinking agents | `researchd` is deterministic and inexpensive — ingest, claim, renew, reclaim, poll, reconcile, enforce. It calls no model. A background *process* is not a background *agent* |
| 3 | expensive reasoning is event-triggered | a model call happens only inside a claimed work item, against a reserved budget, dispatched from an event or a schedule; nothing polls a model |
| 4 | project-isolated science | every runtime row carries `project_id`; the kernel adapter is constructed per repository and reads only that capsule |
| 5 | shared literature | unchanged; the SQLite literature index remains the shared, rebuildable store |
| 6 | Git-tracked files are truth | `runtime/kernel.py` is the only module that reads a capsule and has no method that writes one; `tests/test_runtime_authority.py` asserts this by inspecting the package, not by convention |
| 7 | rebuildable indexes | PostgreSQL holds operational state only. Deleting it loses the queue, the leases, the spend counters and the checkpoints; it loses no science |
| 8 | no agent approves its own work | independence is *requested* and then *reported*: a review that had to run on the producer's own family is recorded as degraded rather than claimed as independent |
| 9 | reviewers start from clean context | review inputs are artifact references to frozen packets; a producer's scratch reasoning is never an input to its reviewer |
| 10 | claims traceable to evidence | unchanged; acceptance is still `validate.claim_approval()`, called and never reimplemented |
| 11 | experiments traceable | an `ExecutionSpec` is frozen and digested before submission, and the digest is stored on the job row |
| 12 | paid use has budgets | reserve → execute → reconcile against `budgets`, checked before the spend, in SQL, so two concurrent workers cannot both be told there is room for the last call |
| 13 | no unofficial automation | unchanged; provider CLIs' documented non-interactive modes and documented HTTP APIs only |
| 14 | finite stop conditions | three independent bounds: per-item `max_attempts`, per-class retry policy (several classes never retry), and `max_cycles_per_objective` on the continuation chain. A cycle cannot extend itself; it can only open a successor, and the chain depth is measured in SQL |
| 15 | explicit cross-project transfer | unchanged; `insight` nomination and human promotion remain the only path |

The one thing R5 deliberately does **not** acquire is epistemic authority:

```text
near-100% operational autonomy  !=  100% epistemic authority
```

## Change control record: R5 integration

Per the change-control section above, a dedicated record. **No invariant
changed.** What changed is that two of them are now enforced by mechanism where
they were previously enforced by absence, and one adoption was added to the
permitted-technology list in `ARCHITECTURE.md` §12b.

| # | invariant | what the integration changed |
|---|---|---|
| 6 | Git-tracked files are truth | unchanged, and now *harder* to violate: nothing under `research_os/runtime` imports `research_os.proposal.promote`, and `tests/test_runtime_authority.py` asserts it by parsing the package. The runtime can create a proposal; only `researchctl propose promote` can make one a capsule object, and it requires an interactive terminal |
| 8 | no agent approves its own work | unchanged. `review_independence: require` adds a mode where a critical review that cannot get a different provider family **fails** instead of being recorded as degraded. `prefer` remains the default, because on a one-family machine failing closed would mean no scientific review ever happens |
| 11 | experiments traceable | strengthened. An interpretation is now bound durably to one `(job, spec digest, interpreter version)` rather than to whichever job finished most recently. Association by temporal coincidence was not traceability |
| 12 | paid use has budgets | unchanged |
| 14 | finite stop conditions | unchanged, and the new continuation path is bounded by the same three: `should_continue` consults the cycle's recommendation, the lineage depth measured in SQL, and the budget. A capsule change makes a parked objective *eligible*; it does not exempt it |
| 15 | explicit cross-project transfer | unchanged. `nominate_insight` writes a nomination and leaves `scope`, `assumptions` and `applicability` empty — those three fields *are* the judgement that a finding transfers, and `missing_for_promotion` tells the researcher they are what is missing. The runtime cannot express the scoping, let alone the promotion |

The one thing worth stating as a new property rather than a preserved one:

```text
a runtime that can ASK for a scientific change  !=  a runtime that can MAKE one
```

Before this integration the runtime could not ask, which is why it stopped on an
unchanged frontier. It can ask now, and everything about how that ask reaches a
person — a separate type, a separate store, a deterministic grounding check, an
independent assessment, an interactive confirmation, a draft and nothing
stronger — exists so that asking never becomes making.

## Change control record: the autonomous discovery portfolio

Per the change-control section above, a dedicated record. **No invariant
changed, and no technology was added** — this layer uses PostgreSQL, LangGraph,
the existing daemon, the existing artifact store, the existing router and
ordinary Git, all of which `ARCHITECTURE.md` §12a and §12b already permit.

What changed is that the unit of continuation is now the *portfolio* rather
than the objective. Invariant by invariant:

| # | invariant | how the portfolio holds it |
|---|---|---|
| 1 | local before LLM | deduplication is three deterministic layers before a model is asked at all; the adjudication type is read from the falsifier by `runtime.adjudication.classify`; the allocator, the gates, the digest and the Curator are ordinary Python and consult no model |
| 2 | no continuously thinking agents | the tick is deterministic, sub-second and makes no model call; `tests/test_portfolio_authority.py` asserts the allocator and the tick have no way to make one, because `researchd` runs them |
| 3 | expensive reasoning is event-triggered | every model call happens inside one claimed work item, against a reserved budget, in one bounded stage that ends; nothing polls a model |
| 4 | project-isolated science | every idea row carries `project_id`; both digests are project-scoped, so an idea copied into another project inherits no reviewed identity |
| 5 | shared literature | unchanged; the novelty audit reads the existing shared index through an injected source |
| 6 | Git-tracked files are truth | the bank is written to a **reserved branch** under `.research-os/`, never `.research/`, and the Curator refuses any path under the capsule. Nothing in the package imports anything that writes one |
| 7 | rebuildable indexes | the bank is a deterministic *view* of PostgreSQL; `researchctl portfolio status` reports the uncurated count, so the one thing losing the database would lose is a number rather than an assumption |
| 8 | no agent approves its own work | a review whose model call *is* the version's origin call raises rather than degrading; `board_independence` counts distinct reviewer models and nothing rendered may say "independent" when it is 1 |
| 9 | reviewers start from clean context | `ReviewPacket` is frozen and has no field for another reviewer's verdict; three tests assert the type's shape |
| 10 | claims traceable to evidence | unchanged, and extended: literature evidence with no retrieved source key is refused by a check constraint, so model memory is not storable as evidence at all |
| 11 | experiments traceable | unchanged; an idea settled by measurement stops below VALIDATED on a host that cannot execute, rather than being concluded from reasoning about what the measurement would have shown |
| 12 | paid use has budgets | portfolio, project and run ceilings through the existing ledger; idea and lineage ceilings in the allocator before a stage is enqueued; a stage ceiling on the request itself |
| 13 | no unofficial automation | unchanged |
| 14 | finite stop conditions | six bounds where a portfolio can loop and a single objective cannot: breadth, lineage depth, branching factor, revisions, spend, and generating forever without generating anything. The stage machine's termination test drives it to a fixed point and found a real loop; the sixth bound was missing entirely and was found by running the tick twice against a real project — every bound was per idea or per lineage, and exploration is neither |
| 15 | explicit cross-project transfer | unchanged; nothing here nominates or promotes |

The property worth stating as new rather than preserved:

```text
a portfolio that keeps working  !=  a portfolio that keeps deciding
```

Every idea it produces is a candidate. The two acts that make one scientific --
promoting a proposal and recording a Review -- are unchanged, are human, and
are not reachable from this package.
