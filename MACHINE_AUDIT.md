# Machine audit

Recorded 2026-09-08 from already-verified host facts. This is not a live
re-audit. Research OS agents must not run another broad machine audit unless a
documented fact needed for kernel work is genuinely missing.

This file describes **this host**. It is not a portable architecture contract.
Portable rules live in `DESIGN_INVARIANTS.md`, `ARCHITECTURE.md`, and
`SECURITY.md`.

## Verified runtime

| Fact | Value |
|---|---|
| Host OS | Ubuntu 24.04.5 LTS |
| Active kernel | 6.8.0-139-generic |
| Research OS interpreter | uv-managed CPython 3.12.14 |
| System Python | separate; must not be imposed on science projects |
| uv | 0.12.10 |
| sqlite3 | 3.45.1 |
| Kernel repo | `~/research/research-os` |

Bootstrap validation already showed `uv.lock` valid, `researchctl version`
`0.1.0`, and `researchctl doctor` passing Python, Git, SQLite, and the four XDG
directories.

> **Later change (WP-B).** The observation above is left as recorded. Doctor no
> longer checks for `sqlite3`: the kernel never needed the `sqlite3` executable
> -- the project registry used Python's bundled `sqlite3` module -- and since
> WP-B replaced that registry with JSON, the kernel uses no SQLite at all.
> Current doctor checks the running Python version, `git` on `PATH`, and the
> four XDG directories. The `sqlite3 3.45.1` row above remains an accurate fact
> about this host.

## XDG directories

These directories already exist on this host:

```text
~/.config/research-os
~/.local/share/research-os
~/.cache/research-os
~/.local/state/research-os
```

Empty bootstrap leftovers under those trees (`literature/`, `embeddings/`,
`scheduler/`, and similar) are **not** part of the R0 contract. R0 must not
populate them, require them, or delete them.

`~/.config/research-os` had no `config.toml` and no `secrets.env` at audit time.
That is valid for R0.

## UEFI / MOK volume-full recovery

This laptop experienced a UEFI/MOK failure during the Ubuntu 22.04 → 24.04
migration.

Observed messages included:

```text
Could not create MokListRT: Volume Full
Could not create MokListXRT: Volume Full
Could not create SbatLevelRT: Volume Full
Could not create MokListTrustedRT: Volume Full
import_mok_state() failed: Volume Full
```

Recovery required ASUS firmware intervention:

```text
Secure Boot Control → Enabled
Key Management
Reset to Setup Mode
Secure Boot Control → Disabled
Save Changes and Exit
```

Current Secure Boot state is **intentionally disabled**.

## Binding rule for agents

Research OS agents must **not** automatically manipulate firmware, BIOS/UEFI,
Secure Boot, or MOK state. They must not enroll or delete MOK keys, modify UEFI
key databases, or perform firmware updates. Such operations require explicit
human authorization.

## Other host notes

Battery telemetry is degraded/invalid and must not be treated as reliable.

Doctor and other kernel commands must not inspect firmware.
