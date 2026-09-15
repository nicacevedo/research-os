# Integration build record — autonomous runtime, vnext

What this integration cycle started from, what it converged, what it fixed, what
it could not verify here, and what remains. A record of a point in time, like
`docs/V1_BUILD_RECORD.md` and `docs/R5_BUILD_RECORD.md`; the live
specifications are `docs/RUNTIME.md` and `docs/CAPSULE.md`.

Historical records are not rewritten to agree with this one. Where the R5 build
record says something this cycle changed, the change is recorded here.

## 1. Starting state, verified rather than assumed

Established by inspection before anything was modified.

```text
working tree            /home/nicacevedo/research/research-os   clean
checked-out branch      r5/autonomous-runtime                   7e7bfc9649a4af5c59341a9823b29004954c7012
                        == origin/r5/autonomous-runtime

origin/main             b3fd03c549c329ad21efb7ae203a9acc7387efa0   (tag v1.0.0)
origin/release/v1.1.0-autonomy
                        847ddecd2f08f116240f2ac6def63142844211f4
local main              e35414f7178dbd3656a6081dc5b0af621b7cc924   (stale, 53 behind origin/main)
```

Ancestry, computed rather than recalled:

```text
merge-base(r5, v1.1)        = b3fd03c   (== origin/main == v1.0.0)
merge-base(r5, main)        = b3fd03c
merge-base(v1.1, main)      = b3fd03c

origin/main is an ancestor of r5    yes
origin/main is an ancestor of v1.1  yes
v1.1 is an ancestor of r5           NO
```

So the two lines are **siblings on v1.0.0**, not a chain. `main` does not
contain the v1.1 work, which settles the question §2 of the integration brief
left open: the current stable scientific/capability line is
`release/v1.1.0-autonomy`, and it had never met R5.

### Running processes

107 `cursorsandbox` wrapper processes belonging to this user, all in
`futex_wait_queue`, aged seven days, left over from an earlier editor session.
None was killed. Because they cannot be proven inert, **every change in this
cycle was made in a separate worktree** —
`/home/nicacevedo/research/research-os-integration` — rather than in the
checkout they might still hold. No `researchd`, no PostgreSQL server and no
pytest belonging to anyone else was running.

### Host capabilities, probed rather than assumed

```text
bwrap            /usr/bin/bwrap           present
systemd-run      /usr/bin/systemd-run     present
unshare          /usr/bin/unshare         present
podman                                    absent
docker                                    absent
apptainer / singularity                   absent
sbatch / squeue / sacct / srun            absent
psql / initdb / pg_ctl (system)           absent
```

PostgreSQL for tests and for `runtime dev-db` comes from the `pgserver` dev
wheel, as `docs/RUNTIME.md` §8 describes. Slurm is genuinely unavailable from
this host; see §5.

### Baseline verification

Each line run on its own checkout, with its own `.venv`, before the merge.

```text
release/v1.1.0-autonomy   uv run --frozen pytest -q      2605 passed
r5/autonomous-runtime     uv run --frozen pytest -q      3006 passed
```

`--extra runtime` does not exist on the v1.1 line, which is itself a fact about
the divergence: the runtime extra and the `researchd` entry point arrive with
R5.

## 2. Convergence

```text
integration branch   integration/autonomous-runtime-vnext
base                 release/v1.1.0-autonomy (847ddec)
merged               r5/autonomous-runtime   (7e7bfc9)   --no-ff, history preserved
```

Neither published history was rewritten, rebased or force-pushed.

**Textual conflicts: three, all documentation.** `CHANGELOG.md` (both lines
wrote an `## [Unreleased]` section), `README.md` (both added a Status
paragraph), and `ARCHITECTURE.md`, which merged cleanly. Both changelog
sections are preserved under one converged heading, demoted a level rather than
edited, because each records what its line actually did.

**Source conflicts: none.** The two changesets overlap in exactly three files,
all Markdown. R5 lives almost entirely in a new package; v1.1 changed the v1
layers underneath it.

That is a low *textual* conflict rate and says nothing about semantics, which
is audited separately (§3).

```text
uv run --frozen pytest -q                                3249 passed
uv run --frozen ruff check .                             clean
uv run --frozen ruff format --check .                    clean (270 files)
uv run --frozen pytest -q tests/test_runtime_*.py         359 passed
same files, reverse order                                 359 passed
```

3249 = 2605 + (3006 - 2362): every test from both lines, and no test from
either lost.
