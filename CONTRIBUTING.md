# Contributing to Research OS

## Getting a working checkout

Research OS needs Python 3.12 and [uv](https://docs.astral.sh/uv/). Nothing
else — no model provider, no API key, no network access.

```bash
git clone https://github.com/nicacevedo/research-os.git
cd research-os
uv sync --all-groups
```

## The gates

Three commands. All three must pass before you stop.

```bash
uv run pytest -q
uv run ruff check .
uv run ruff format --check .
```

The test suite is **hermetic** by design:

- it opens no sockets — every literature transport test drives a scripted client
  that never reaches the network;
- it spends no model calls — every provider is a scripted double;
- it touches no repository outside `tmp_path`, because the four
  `RESEARCH_OS_*_HOME` variables are relocated per test and a fixture refuses to
  let a stale one leak between them.

If a change makes any of those untrue, the change is wrong, not the rule.

It takes roughly 13 minutes: much of it is real subprocesses, real Git
worktrees, real file locking and a deterministic lock stress test, because those
are the parts that were wrong before and tests that mocked them did not notice.

## What a change has to come with

**A test that fails without it.** Ordinary, but this project has a specific
history with it: several times a suite passed while not testing the property it
named, and the defect was found later by an independent review. If you are
fixing a bug, write the test first and watch it fail.

**For anything touching a safety boundary, a test that discriminates.** Break
the fix on purpose — monkeypatch the guard away — and confirm the test fails.
A regression test that passes with the protection removed is not a regression
test. `tests/test_crash_recovery.py` and
`tests/test_proposal_grounding_correction.py` are written this way.

**Comments that say why, not what.** The existing source explains the reasoning
behind a decision and, where a previous approach was wrong, what was wrong with
it. Match that.

## Invariants you may not weaken

These are not style preferences. A change that relaxes one will be rejected even
if every test passes.

- No automated path may record a human Review, set `reviewer_kind` to human, or
  mark a Claim accepted.
- No model may modify its own permissions, allowed paths, executable commands,
  budgets, run state, canonical branches, or scientific approval state.
- Every subprocess takes an argv list. No `shell=True`, no `os.system`, no
  `eval`, no `exec`.
- A write-enabled worker runs in an isolated Git worktree, never the canonical
  checkout.
- Model-originated and retrieved text is fenced as data before entering another
  prompt.
- Repair and correction are bounded. One repair per work item, one grounding
  correction per proposal. No recursive model loops.
- The run lock is `flock` on a stable pathname and is **never** unlinked. A
  `flock` is held on an inode; removing the name while it is held lets the next
  arrival lock a different inode at the same path. This was measured producing
  real violations, twice.

`DESIGN_INVARIANTS.md` is the full list and is authoritative.

## Where things live

| area | module |
| --- | --- |
| scientific objects, digests, acceptance | `src/research_os/capsule.py`, `validate.py`, `review.py` |
| prompt/data boundary | `src/research_os/automation/promptdata.py` |
| command authorisation | `src/research_os/automation/command_policy.py` |
| worktree isolation | `src/research_os/automation/worktree.py` |
| run locking | `src/research_os/runlock.py` |
| literature transport | `src/research_os/literature/http.py` |
| recovery and reclamation | `src/research_os/diagnostics.py` |

## Reporting a security issue

Do not open a public issue. See [SECURITY.md](SECURITY.md).
