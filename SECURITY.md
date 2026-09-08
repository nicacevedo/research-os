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

Paid API use requires explicit budgets (invariant 12). R0 has no API clients.
Later releases that add providers must expose and respect configurable limits
before any paid call.

## Agent execution constraints

- Finite stop conditions; stop after the approved milestone
- No continuously thinking agents in R0
- No execution adapters, containers, Slurm, or MCP in R0
- Do not touch real scientific project repositories unless explicitly instructed
- Do not expose secrets in chat, logs, or commits
- Do not push or merge unless explicitly instructed
