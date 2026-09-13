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
