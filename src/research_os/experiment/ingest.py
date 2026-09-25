"""Turning what a process left on disk into a candidate evidence packet.

Every step is deterministic and none of it is a scientific judgement. The
executor said a process finished; this establishes what it produced, hashes it,
runs the checks the researcher declared, and assembles a packet a human can read.

The packet is a *candidate*. It carries no verdict about any hypothesis, because
whether a result supports or contradicts anything is exactly the judgement this
system reserves for a person. What it does carry is everything they would need
to make it: the command that ran, the commit it ran from, the digest of every
file it wrote, which declared outputs are missing, and which checks failed.

Two things are deliberately recorded even though they are inconvenient.

**Undeclared files are reported.** A command that wrote somewhere its
specification did not mention is not an error, but it is something a reader
should see, because it is how a result quietly depends on a file nobody tracked.

**A missing declared output is a blocker, not a warning.** An experiment that
said it would produce a result file and did not has not produced a result,
whatever its exit code said.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
from pathlib import Path

from research_os.automation.filescope import contained_file, read_contained
from research_os.errors import ExperimentIngestError
from research_os.experiment.models import (
    ArtifactRecord,
    CheckOutcome,
    EvidencePacket,
    ExperimentRun,
)

#: The largest file that is hashed and recorded as an artifact.
#:
#: Large enough for an ordinary result file and small enough that hashing a
#: run's outputs is not itself an experiment. A larger file is recorded by name
#: with its size, and its absence of a digest is stated.
MAX_ARTIFACT_BYTES = 512 * 1024 * 1024

#: Directories never scanned when looking for what a run produced.
#:
#: Machinery rather than results. A ``.git`` directory changes on every
#: operation and a ``__pycache__`` is a byproduct of running Python at all;
#: recording either as an experimental artifact would bury the result.
SKIPPED_DIRECTORIES: frozenset[str] = frozenset(
    {".git", "__pycache__", ".pytest_cache", ".ruff_cache", ".mypy_cache", ".venv"}
)

#: The deterministic checks a command specification may name.
#:
#: Named rather than expressed, so a configuration file selects from checks the
#: controller knows how to perform instead of describing one in code.
KNOWN_CHECKS: tuple[str, ...] = (
    "outputs_exist",
    "outputs_are_json",
    "outputs_are_non_empty",
    "exit_code_zero",
)


def digest_file(path: Path) -> tuple[str | None, int]:
    """Return the digest and size of one file, streamed rather than loaded."""

    try:
        size = path.stat().st_size
    except OSError as exc:
        raise ExperimentIngestError(f"cannot stat {path}: {exc}") from exc
    if size > MAX_ARTIFACT_BYTES:
        return None, size
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(block)
    except OSError as exc:
        raise ExperimentIngestError(f"cannot read {path}: {exc}") from exc
    return digest.hexdigest(), size


def collect_artifacts(
    worktree: Path,
    *,
    declared: list[str],
    discovered: list[str] | None = None,
) -> list[ArtifactRecord]:
    """Record every declared output that exists, plus any extras the caller found.

    A declared output that is missing produces no record at all -- its absence is
    what :meth:`ExperimentRun.missing_outputs` reports, and inventing a
    zero-length record for it would make a missing result look like an empty one.
    """

    records: list[ArtifactRecord] = []
    seen: set[str] = set()
    for relative in [*declared, *(discovered or [])]:
        if relative in seen:
            continue
        seen.add(relative)
        # The containment rule every other evidence reader applies: no link at
        # *any* component. Asking `is_symlink()` of the file alone followed a
        # directory link -- `results -> specimens` committed as a convenience
        # -- and hashed a committed specimen as this run's output.
        target = contained_file(worktree, relative)
        if target is None:
            continue
        sha256, size = digest_file(target)
        if sha256 is None:
            # Too large to hash. Recorded by name and size with the reason
            # stated, because a result nobody can identify by content is still a
            # result somebody has to be told about.
            records.append(
                ArtifactRecord(
                    path=relative,
                    sha256="0" * 64,
                    byte_size=size,
                    media_type="application/octet-stream",
                    declared=relative in declared,
                )
            )
            continue
        records.append(
            ArtifactRecord(
                path=relative,
                sha256=sha256,
                byte_size=size,
                media_type=mimetypes.guess_type(relative)[0] or "",
                declared=relative in declared,
            )
        )
    return sorted(records, key=lambda item: item.path)


def discover_changed_paths(worktree: Path, *, since: dict[str, float]) -> list[str]:
    """Return files whose modification time moved since the run started.

    A cheap, honest heuristic for "what did this actually touch", used only to
    *report* undeclared writes. It never decides whether a run succeeded, so a
    filesystem with coarse timestamps costs a reader a note rather than costing
    the run a verdict.
    """

    found: list[str] = []
    for path in _walk(worktree):
        relative = path.relative_to(worktree).as_posix()
        try:
            modified = path.stat().st_mtime
        except OSError:
            continue
        if since.get(relative) != modified:
            found.append(relative)
    return sorted(found)


def snapshot_mtimes(worktree: Path) -> dict[str, float]:
    """Record modification times before a run, so afterwards means something."""

    snapshot: dict[str, float] = {}
    for path in _walk(worktree):
        try:
            snapshot[path.relative_to(worktree).as_posix()] = path.stat().st_mtime
        except OSError:
            continue
    return snapshot


def _walk(worktree: Path):
    """Yield every ordinary file in the worktree, never following a symlink."""

    import os

    for directory, subdirectories, files in os.walk(worktree, followlinks=False):
        subdirectories[:] = [
            name for name in subdirectories if name not in SKIPPED_DIRECTORIES
        ]
        here = Path(directory)
        for name in files:
            candidate = here / name
            if candidate.is_symlink() or not candidate.is_file():
                continue
            yield candidate


def run_checks(
    run: ExperimentRun,
    *,
    worktree: Path,
    names: list[str],
) -> list[CheckOutcome]:
    """Run the deterministic checks a command specification named.

    A check this build does not implement fails rather than being skipped: a
    configuration asking for a guarantee that nobody provides should not look as
    though the guarantee held.
    """

    outcomes: list[CheckOutcome] = []
    for name in names:
        if name == "exit_code_zero":
            outcomes.append(
                CheckOutcome(
                    name=name,
                    passed=run.exit_code == 0,
                    detail=f"exit code {run.exit_code}",
                )
            )
        elif name == "outputs_exist":
            missing = run.missing_outputs
            outcomes.append(
                CheckOutcome(
                    name=name,
                    passed=not missing,
                    detail=(
                        "every declared output was produced"
                        if not missing
                        else "missing: " + ", ".join(missing)
                    ),
                )
            )
        elif name == "outputs_are_non_empty":
            empty = [
                item.path
                for item in run.artifacts
                if item.declared and item.byte_size == 0
            ]
            outcomes.append(
                CheckOutcome(
                    name=name,
                    passed=not empty and not run.missing_outputs,
                    detail=(
                        "empty: " + ", ".join(empty)
                        if empty
                        else "missing: " + ", ".join(run.missing_outputs)
                        if run.missing_outputs
                        else "every declared output has content"
                    ),
                )
            )
        elif name == "outputs_are_json":
            outcomes.append(_json_check(run, worktree))
        else:
            outcomes.append(
                CheckOutcome(
                    name=name,
                    passed=False,
                    detail=(
                        f"{name!r} is not a check this build implements; known "
                        f"checks are {', '.join(KNOWN_CHECKS)}"
                    ),
                )
            )
    return outcomes


def _json_check(run: ExperimentRun, worktree: Path) -> CheckOutcome:
    """Check that every declared ``.json`` output parses. Structure only."""

    targets = [
        item for item in run.artifacts if item.declared and item.path.endswith(".json")
    ]
    if not targets:
        return CheckOutcome(
            name="outputs_are_json",
            passed=not run.missing_outputs,
            detail="no declared output is a .json file",
        )
    broken: list[str] = []
    for item in targets:
        raw = read_contained(worktree, item.path, max_bytes=MAX_ARTIFACT_BYTES)
        if raw is None:
            broken.append(f"{item.path} (not a file this run wrote in its worktree)")
            continue
        try:
            json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            broken.append(f"{item.path} ({exc})")
    return CheckOutcome(
        name="outputs_are_json",
        passed=not broken,
        detail=(
            "every declared .json output parses"
            if not broken
            else "did not parse: " + "; ".join(broken)
        ),
    )


def build_evidence_packet(
    run: ExperimentRun,
    *,
    checks: list[CheckOutcome],
    notes: list[str] | None = None,
) -> EvidencePacket:
    """Assemble the candidate packet a human reads before deciding anything.

    Deterministic from the run record and the checks. The notes it carries are
    facts about the execution -- an undeclared write, an unobservable resource,
    an unrecognised scheduler state -- never a reading of the results.
    """

    collected = list(notes or [])
    if run.undeclared_artifacts:
        collected.append(
            "this run wrote files its specification did not declare: "
            + ", ".join(run.undeclared_artifacts)
        )
    if not run.usage.anything_observed:
        collected.append(
            "no resource usage could be observed for this execution; the report "
            "says unknown rather than estimating"
        )
    if run.scheduler is not None and run.scheduler.raw_state:
        collected.append(
            f"the scheduler reported this job as {run.scheduler.raw_state!r}"
        )
    return EvidencePacket(
        packet_id=f"PKT-{run.run_id.removeprefix('XRUN-')}",
        run_id=run.run_id,
        task_name=run.task_name,
        project_id=run.project_id,
        project_path=run.project_path,
        base_commit=run.base_commit,
        executor=run.executor,
        argv=list(run.argv),
        parameters=dict(run.parameters),
        state=run.state,
        exit_code=run.exit_code,
        artifacts=list(run.artifacts),
        missing_outputs=run.missing_outputs,
        checks=checks,
        usage=run.usage,
        notes=collected,
    )
