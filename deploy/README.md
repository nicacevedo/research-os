# Deployment files

Shipped, never installed. Nothing in this repository copies anything from here
into a system location, enables a service, or reloads a service manager.

| file | what it is |
|---|---|
| `researchd.service` | a systemd **user** unit for the control plane. Its own header has the install steps. |

Two boundaries worth restating, both from `AGENTS.md` and `SECURITY.md`:

**Enabling a persistent service is the researcher's decision.** The runtime
works perfectly well run from a terminal (`researchd`, or `researchctl runtime
daemon`). The unit exists for people who want it to survive a reboot.

**The unit's hardening is defence in depth, not a sandbox.** The control plane
runs project acceptance commands, which runs project code — including code an
autonomous worker has just written, in an isolated worktree. Worktree isolation
protects the canonical checkout. It is not an OS sandbox, and no network or
process sandboxing is provided.
