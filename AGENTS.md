# Agent operating contract

This repository is the Research OS kernel. It is not an individual science project.

## Live authority

Read these, in this order, before significant work:

1. `DESIGN_INVARIANTS.md` — architectural constitution.
2. `ARCHITECTURE.md` — system boundaries and responsibilities.
3. `docs/CAPSULE.md` — the live Research Capsule specification: layout, object
   schemas, IDs, lifecycle, semantic digests, and the Claim acceptance rule.
4. `SECURITY.md` — secrets, privilege, and provider boundaries.
5. `ROADMAP.md` — release sequence and current implementation state.
6. `docs/AUTOMATION_MVP.md` — the automation control plane: what it may do,
   what stays human, and the isolation and budget boundaries it enforces.
7. `docs/RUNTIME.md` — the autonomous runtime (R5): the authority model, the
   queue's guarantees, the idempotency ledger, locking, the failure taxonomy,
   and the autonomy levels. Read it before touching `research_os.runtime`.

`docs/CAPSULE.md` is authoritative on anything scientific. Where any other
document disagrees with it, it wins.

Everything under `docs/plans/` is **historical**. Those documents record what was
approved at a point in time, are not rewritten to match later decisions, and must
never be implemented from. Do not treat a plan as the current contract.

## Human review

Scientific approval is a human act. Concretely:

> Automated agents must not invoke `researchctl review`, must not respond to its
> interactive prompts on behalf of the researcher, and must not author a Review
> with `reviewer_kind: human`. Human Reviews require the researcher's direct
> interaction.

`researchctl review` requires an interactive terminal. That is a usability and
safety guard, not proof of human identity: it makes unattended approval
inconvenient and obvious, and it does not authenticate anyone. The boundary above
is policy, and it binds agents regardless of what the terminal permits. An agent
may prepare evidence, draft findings text for a person to review and edit, and
run every read-only command; it may not record the approval.

The same boundary covers the two other places something can cross into science
or into another project's reasoning:

> Automated agents must not invoke `researchctl propose promote` or
> `researchctl insight promote`, and must not respond to their confirmation
> prompts on behalf of the researcher. An agent may *create* a proposal and a
> nomination -- the autonomous runtime does both, constantly -- and neither is
> scientific state. Promotion is what makes a proposal a capsule object and a
> nomination knowledge other projects read, and both are human acts.

Nothing under `research_os/runtime` imports `research_os.proposal.promote`, and
`tests/test_runtime_authority.py` asserts that by parsing the package rather
than by trusting this paragraph.

## Operating rules

6. Git-tracked YAML and Markdown under a project's `.research/` directory are
   canonical scientific state.
7. The global project registry is rebuildable, noncanonical discovery metadata.
   Deleting it must never alter project files.
8. Never run `sudo` or modify host packages without explicit human authorization.
9. Never manipulate firmware, UEFI, MOK, or Secure Boot.
10. Never expose or commit secrets.
11. Never opportunistically implement later-release functionality.
12. Before stopping, run `uv run pytest`, `uv run ruff check .`, and
    `uv run ruff format --check .`. All three must pass.
13. Never push, merge, or enable persistent services unless explicitly instructed.
14. Stop after the approved work package.

Do not invent configuration, schemas, or commands that the current work package
does not authorize.
