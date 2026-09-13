# Research OS security

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
