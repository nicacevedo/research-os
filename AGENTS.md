# Agent operating contract

This repository is the Research OS kernel. It is not an individual science project.

Before significant work:

1. Read `DESIGN_INVARIANTS.md` (architectural constitution).
2. Read `ARCHITECTURE.md`.
3. Read `SECURITY.md`.
4. Read the currently approved release plan (`docs/plans/R0_KERNEL_PLAN.md` while R0 is active).
5. Work only on the currently authorized milestone.

Operating rules:

6. Git-tracked YAML and Markdown under a project's `.research/` directory are canonical scientific state.
7. SQLite indexes, caches, and `project_registry.sqlite` are rebuildable and noncanonical.
8. Never run `sudo` or modify host packages without explicit human authorization.
9. Never manipulate firmware, UEFI, MOK, or Secure Boot.
10. Never expose or commit secrets.
11. Never opportunistically implement later-release functionality.
12. Run deterministic validation and tests before stopping.
13. Never push, merge, or enable persistent services unless explicitly instructed.
14. Stop after the approved milestone.

Do not invent configuration, schemas, or commands that the current milestone does not authorize.
