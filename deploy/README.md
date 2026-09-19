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

**Both units have now been installed and exercised on this machine**, which is
how the one defect in `researchd.service` was found: it had never been
installed, so nothing had discovered that `ProtectKernelModules=true` cannot be
applied by a *user* manager. The unit did not degrade -- it failed to start with
`status=218/CAPABILITIES` and crash-looped. The directive is gone and its
header says why, including the check that removing it costs no protection a
user unit actually delivers. What was verified after the fix:

| | |
|---|---|
| `systemctl --user start` | active, attached to the configured DSN |
| `systemctl --user stop` | graceful in 1.7s, "finishing the current pass", zero-work report |
| `kill -9` the worker | systemd restarted it after `RestartSec`; run/finding/work/model-call counts identical afterwards, and no successor was created |
| starting twice | second `start` is a no-op; a manual `researchd` beside the service finds the advisory lock held, says so, and exits 0 |
| the human gate | the parked run's `next_recommendation` survived the crash and the restart |

Not done, because it is a separate decision: `loginctl enable-linger`. Without
it these units start at your next login and stop when you log out, which is why
`Linger=no` is the state to check first if the control plane is not running
after a reboot.

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
