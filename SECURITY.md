# Research OS security

## Reporting a vulnerability

Report security issues privately, not as a public issue: use GitHub's **Report a
vulnerability** button under this repository's Security tab, which opens a
private advisory. Please include what an attacker would gain, the smallest
reproduction you have, and the commit you saw it on. There is no bounty and no
guaranteed response time; this is a research tool maintained by one person.

Do not include real credentials, private scientific data, or unpublished
manuscript content in a report.

## The trust boundary

Research OS is designed for **repositories you already trust**, running on a
machine you control, as your own user. That assumption is load-bearing, and
everything below follows from it.

**Worktree isolation is a Git boundary, not an OS sandbox.** A write-enabled
worker gets its own Git worktree so it cannot modify the researcher's canonical
checkout, and the controller verifies that immediately before every write
invocation. That protects the scientific record from an ordinary mistake. It
does not confine a process: the worktree is an ordinary directory on the same
filesystem, with the same user, the same network, and the same environment.

**There is now an OS sandbox, and whether it works is a property of your host.**
`research_os/sandbox.py` runs commands this repository did not write inside
OS-level containment: the worktree read-write, declared inputs read-only, an
isolated `/tmp` with a throwaway `HOME`, a read-only operating system, and no
home directory, SSH keys, SSH agent, Git credentials, provider credentials,
unrelated environment or network. Network is a capability, not a default.

Three modes, set by `sandbox.mode` in `automation.yaml`:

| mode | behaviour |
|---|---|
| `required` | contained or not run |
| `preferred` | *default.* Contained where possible; the absence recorded on every command result |
| `off` | not contained, by a deliberate configured choice |

**High-autonomy runtime execution overrides this to `required`.** Unattended
execution of model-written code with the researcher's credentials is the
exposure this exists for, and `preferred` there would be a default that quietly
permits it.

**And it refuses to claim containment it does not have.** The probe *runs* each
technology rather than looking for its binary, because the interesting failure
is invisible to `which`: on Ubuntu 24.04 and later,
`kernel.apparmor_restrict_unprivileged_userns` is 1 by default, so a non-setuid
`bwrap` gets a user namespace it has no capabilities in and isolates nothing --
and `systemd-run --user` accepts `ProtectHome` and `PrivateNetwork`, starts the
unit, and lets the process see the real home directory and the real network.
Neither is used on such a host. `researchctl runtime doctor` reports what was
probed, what happened, and the remedy. Where nothing works, high-autonomy
execution is refused and the fingerprint check below is all that remains.

**Project code executes with your Unix permissions.** Two paths run code this
repository did not write:

- *Acceptance checks.* After a write-enabled worker changes a project, the
  controller runs that project's declared check commands -- `pytest`, `ruff` --
  against the code the worker just wrote. `pytest` executes the project's
  `conftest.py` and its test modules. If a model wrote them, a model chose what
  runs.
- *Declared experiments.* An experiment runs a command the researcher declared
  in `~/.config/research-os/experiments.yaml`, selected by name, with typed
  parameters substituted whole-token. It executes the project's program.

Both are argument vectors, never shell strings: nothing here interpolates into a
shell, and the acceptance-command grammar authorises whole argv vectors rather
than a program name. That stops a *plan* from naming an arbitrary command. It
does not stop a program the researcher already trusts from doing what programs
do.

The practical consequence: **do not point Research OS at a repository you would
not run `pytest` in.** Cloning an untrusted project and starting a run on it is
equivalent to executing that project's code.

**What is enforced rather than requested.** These are structural, not prompt
instructions:

- A model cannot change its own permissions, allowed paths, executable commands,
  model-call budgets, experiment budgets, run state, canonical Git branches, or
  scientific approval state.
- No automated path records a human Review, sets `reviewer_kind` to human, or
  marks a Claim accepted. The acceptance gate lives in `validate.py`.
- Model-originated and externally retrieved text is fenced as data by
  `research_os.automation.promptdata` before it reaches another prompt, and
  cannot forge a fence delimiter or alter controller-authored instructions.
- Every command is an argv list. There is no `shell=True`, no `os.system`, no
  `eval`, and no `exec` anywhere in the source.

**What is not claimed.** No containment against a hostile project, no protection
against a compromised provider CLI, no multi-user isolation, and no defence
against someone with write access to your own state directory.

## Secrets

Secrets must never enter Git.

Canonical future secret location:

```text
~/.config/research-os/secrets.env
```

If that file is created later, recommended permissions are `chmod 600`. R0 does
not create, load, parse, or print it.

Secrets must not appear in:

- canonical YAML or Markdown
- SQLite indexes or registries
- ordinary logs
- `researchctl` output

Never commit `.env`, `secrets.env`, PEM files, or keys. `.gitignore` already
excludes these patterns.

## Privileged system mutation

No Research OS component may automatically perform privileged system
modification. Agents must not run `sudo`, install OS packages, change
networking, or enable persistent system services without explicit human
authorization.

## Firmware, UEFI, Secure Boot, MOK

This host previously required manual ASUS firmware recovery after a UEFI/MOK
volume-full failure. Current Secure Boot state is intentionally disabled.

Research OS must never automatically:

- manipulate Secure Boot
- enroll or delete MOK keys
- modify UEFI key databases
- perform BIOS/firmware updates

Those operations require explicit human authorization. See `MACHINE_AUDIT.md`.

## Untrusted text at the terminal

Model output and external retrieved text are data, and they reach a human
through a terminal that reads some byte sequences as commands. `ESC` opens an
ANSI control sequence that can clear the screen, move the cursor back over a
line already printed, recolour output, or retitle the window; `0x9B` does the
same thing to a terminal in an 8-bit mode. A finding or summary carrying one
could make a rendered report say something other than what the run recorded.

`research_os.textsafe.terminal_safe` is the display boundary. Every rendered
report, status, plan, and provider view passes through it, and every control
character that is not a newline or a tab is rewritten as a visible `\xNN`
rather than sent to the terminal. It neutralises without censoring: the human
still reads the text that was written.

Two things it deliberately is not:

- It is not the model-to-model boundary. Text entering another model's prompt
  goes through `research_os.automation.promptdata`, which is stricter and also
  neutralises data-block delimiters. Both modules take their definition of
  "control character" from `textsafe` so the two boundaries cannot drift apart.
- It is not applied to stored artifacts. The run archive keeps the bytes the
  provider returned, with their digests. The machine-readable views
  (`auto status --json`, `auto events`) escape rather than rewrite, so they stay
  parseable and still return the original strings from `json.loads`.

## Browser and provider policy

Browser subscription interfaces must not be scraped or unofficially automated.
Authentication and provider terms are respected.

## Human authorization boundaries

Humans retain authority over credentials, provider subscriptions, system-level
Linux changes, firmware/boot configuration, enabling persistent services, and
onboarding real scientific projects.

Agents work inside this repository (and, later, explicitly named project
repositories) under an authorized milestone.

## API budgets

Paid API use requires explicit budgets (invariant 12).

v1 has API clients: three scholarly HTTP sources (OpenAlex, Crossref, arXiv)
and the agent provider CLIs. Every model call is counted against a budget
checked *before* the spend -- separately for model calls, write tasks,
experiments, cluster submissions and wall clock -- and a plan the run could
never pay for is refused at planning time. The scholarly sources are
credential-free and rate-limited by policy rather than by budget; where a key
is optional it is read from the environment and never stored.

## Agent execution constraints

- Finite stop conditions; stop after the approved milestone
- No continuously thinking agents in R0
- No containers and no MCP. v1 has execution adapters and a Slurm abstraction:
  an experiment runs only a command the researcher declared in
  `~/.config/research-os/experiments.yaml`, outside every worktree, selected by
  name with typed parameters substituted whole-token. Execution needs explicit
  authorisation, runs in an isolated worktree, and is bounded by per-run
  counters for local runs and cluster submissions.
- Do not touch real scientific project repositories unless explicitly instructed
- Do not expose secrets in chat, logs, or commits
- The interactive-terminal gate on `review` and `propose promote` is
  `sys.stdin.isatty()`. It is a usability and safety guard, not authentication:
  anything that allocates a PTY satisfies it. The binding rule that an agent
  must not record a human Review lives in `AGENTS.md`, and the structural
  guarantee is the acceptance gate in `validate.py`, which no automated path
  writes.
- Do not push or merge unless explicitly instructed
