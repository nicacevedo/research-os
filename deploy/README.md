# Deployment files

Shipped, never installed. Nothing in this repository copies anything from here
into a system location, enables a service, or reloads a service manager.

| file | what it is |
|---|---|
| `researchd.service` | a systemd **user** unit for the control plane. Its own header has the install steps. |
| `researchd-db.service` | a systemd **user** unit for the disposable local PostgreSQL. Only for a development or single-workstation setup; a production runtime points at a PostgreSQL somebody backs up and does not install this. |
| `apparmor-bwrap` | an AppArmor profile granting bubblewrap the `userns` permission, which is what this class of machine needs before Research OS can contain an experiment at all. Needs root. **Do not install it against bubblewrap < 0.12.0** — see below. Its own header has the install steps and says what it does not do. |

Installing the two units, plus `loginctl enable-linger`, is what makes a
reboot not require the researcher to remember anything. Without linger the
control plane stays stopped until the next login; without `researchd-db` it
comes back and retries against a cluster nobody started.

`apparmor-bwrap` is a different kind of prerequisite: without it this machine
has no working containment backend, so the runtime refuses to *execute* a
model-written experiment at all -- it will design and preregister one and then
stop. That refusal is correct and is not a bug to route around.

**It is also the one file here that can make things worse.** Granting `userns`
to a bubblewrap below 0.12.0 turns a binary that cannot contain anything into
one that contains things and can be walked out of: CVE-2026-87766 lets sandbox
*setup* follow a symlink out of the sandbox and write the host. Replace the
binary first, then install the profile -- never the reverse.
`docs/CONTAINMENT_OPTIONS.md` has the audited options for getting a patched
build, and `researchctl runtime doctor` reports a vulnerable-but-working bwrap
as `PRESENT_BUT_UNACCEPTABLE` rather than as a backend.

None of the three is installed for you.

Two boundaries worth restating, both from `AGENTS.md` and `SECURITY.md`:

**Enabling a persistent service is the researcher's decision.** The runtime
works perfectly well run from a terminal (`researchd`, or `researchctl runtime
daemon`). The unit exists for people who want it to survive a reboot.

**The unit's hardening is defence in depth, not a sandbox.** The control plane
runs project acceptance commands, which runs project code — including code an
autonomous worker has just written, in an isolated worktree. Worktree isolation
protects the canonical checkout. It is not an OS sandbox, and no network or
process sandboxing is provided.
