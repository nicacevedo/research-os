"""What this machine can actually do, and what it is holding on to.

``researchctl doctor`` answers one question: if I ask Research OS to do
something right now, what will happen? The answer has to cover every subsystem,
because a researcher whose run fails at the reviewer does not want to discover
then that only one provider was installed, and a researcher whose disk is full
wants to know that before a worktree fails half way through a write.

Three rules shape every check here.

**Local inspection only.** Nothing in this module makes a network request,
submits a job, or invokes a model. A command a researcher runs to find out
whether things are set up must be free and must not fail for reasons unrelated
to setup.

**Three outcomes, not two.** ``PASS`` means it works. ``FAIL`` means something
a researcher asked for is broken. ``WARN`` means a capability is simply absent
-- no cluster configured, no second provider installed -- which is a normal
state to be in and must not make ``doctor`` exit non-zero.

**Every failure says what to do.** A check that reports a problem without an
action is a check that makes the researcher search the documentation.
"""

from __future__ import annotations

import os
import shutil
import sys
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from research_os.paths import xdg_dir_issue, xdg_dirs
from research_os.textsafe import terminal_safe

#: Python this build is supported on.
SUPPORTED_PYTHON = (3, 12)

#: Free space below which a write-enabled run is likely to fail part way.
LOW_DISK_BYTES = 512 * 1024 * 1024


class Status(StrEnum):
    """The three outcomes a check may have.

    ``WARN`` exists so that "you have no cluster configured" and "your capsule
    is corrupt" are not the same answer. Only ``FAIL`` sets a non-zero exit
    code, so ``doctor`` stays usable in a script.
    """

    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


@dataclass(frozen=True, slots=True)
class Check:
    """One thing that was inspected, and what was found."""

    name: str
    status: Status
    detail: str
    advice: str = ""

    def payload(self) -> dict[str, str]:
        return {
            "name": self.name,
            "status": str(self.status),
            "detail": self.detail,
            "advice": self.advice,
        }


@dataclass(frozen=True, slots=True)
class Section:
    """A group of related checks, with a heading a reader can scan."""

    title: str
    checks: tuple[Check, ...]
    note: str = ""

    @property
    def status(self) -> Status:
        if any(item.status is Status.FAIL for item in self.checks):
            return Status.FAIL
        if any(item.status is Status.WARN for item in self.checks):
            return Status.WARN
        return Status.PASS


@dataclass(frozen=True, slots=True)
class Usage:
    """What one runtime store is holding."""

    name: str
    path: Path
    entries: int
    bytes_used: int
    reclaimable: int = 0
    reclaim_hint: str = ""


@dataclass
class Report:
    """Everything ``doctor`` established, in the order it should be read."""

    sections: list[Section] = field(default_factory=list)
    usage: list[Usage] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        """True when nothing a researcher asked for is broken.

        A ``WARN`` is deliberately not a failure: a machine with one provider
        and no cluster is a perfectly good machine for most research, and an
        exit code that said otherwise would train people to ignore it.
        """

        return all(section.status is not Status.FAIL for section in self.sections)

    def payload(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "sections": [
                {
                    "title": section.title,
                    "status": str(section.status),
                    "note": section.note,
                    "checks": [item.payload() for item in section.checks],
                }
                for section in self.sections
            ],
            "storage": [
                {
                    "name": item.name,
                    "path": str(item.path),
                    "entries": item.entries,
                    "bytes": item.bytes_used,
                    "reclaimable": item.reclaimable,
                    "hint": item.reclaim_hint,
                }
                for item in self.usage
            ],
        }


# -- the checks ---------------------------------------------------------------


def collect(*, include_storage: bool = True) -> Report:
    """Inspect this machine. Makes no network request and invokes no model."""

    report = Report(
        sections=[
            _environment(),
            _providers(),
            _literature(),
            _experiments(),
            _runs(),
        ]
    )
    if include_storage:
        report.usage = list(storage_usage())
        report.sections.append(_disk(report.usage))
    return report


def _environment() -> Section:
    checks: list[Check] = []
    version = ".".join(str(part) for part in sys.version_info[:3])
    checks.append(
        Check(
            name="python",
            status=(
                Status.PASS if sys.version_info[:2] == SUPPORTED_PYTHON else Status.FAIL
            ),
            detail=version,
            advice=(
                ""
                if sys.version_info[:2] == SUPPORTED_PYTHON
                else f"Research OS targets Python {SUPPORTED_PYTHON[0]}."
                f"{SUPPORTED_PYTHON[1]}."
            ),
        )
    )
    git = shutil.which("git")
    checks.append(
        Check(
            name="git",
            status=Status.PASS if git else Status.FAIL,
            detail=git or "not found",
            advice="" if git else "Install Git. Every capsule is a Git repository.",
        )
    )
    for name, path in xdg_dirs().items():
        issue = xdg_dir_issue(path)
        checks.append(
            Check(
                name=name,
                status=Status.PASS if issue is None else Status.FAIL,
                detail=str(path) if issue is None else f"{path} ({issue})",
                advice="" if issue is None else f"Fix the permissions on {path}.",
            )
        )
    return Section(title="environment", checks=tuple(checks))


def _providers() -> Section:
    """Report the agent CLIs and how the roles would actually be assigned.

    The routing is the part that matters. A researcher with one provider gets a
    run whose reviewer shares the implementer's model family, which still
    catches real defects but is not an independent review -- and the only
    acceptable way to ship that is to say so before the run, not after.
    """

    from research_os.automation.config import load_config, resolve_roles
    from research_os.automation.models import Independence
    from research_os.automation.providers import default_registry, probe_registry
    from research_os.errors import ResearchOSError

    checks: list[Check] = []
    note = ""
    try:
        probes = probe_registry(default_registry())
    except ResearchOSError as exc:
        return Section(
            title="providers",
            checks=(
                Check(
                    name="registry",
                    status=Status.FAIL,
                    detail=str(exc),
                    advice="Install and authenticate an agent CLI.",
                ),
            ),
        )

    for name, probe in sorted(probes.items()):
        checks.append(
            Check(
                name=name,
                status=Status.PASS if probe.available else Status.WARN,
                detail=(
                    f"{probe.executable or 'not found'}"
                    + (f"  {probe.version}" if probe.version else "")
                    + (f"  {probe.auth_status}" if probe.auth_status else "")
                ),
                advice=""
                if probe.available
                else f"{name} is not installed or not authenticated here.",
            )
        )

    try:
        config = load_config(None)
        resolved = resolve_roles(config, probes)
    except ResearchOSError as exc:
        checks.append(
            Check(
                name="roles",
                status=Status.FAIL,
                detail=str(exc),
                advice="Check your automation config, or install a provider.",
            )
        )
        return Section(title="providers", checks=tuple(checks))

    for role, setting in sorted(resolved.roles.items()):
        checks.append(
            Check(
                name=f"role:{role}",
                status=Status.PASS,
                detail=(
                    f"{setting.provider} / {setting.model or 'provider default'}"
                    f"   {setting.access}"
                ),
            )
        )
    independent = resolved.independence is Independence.INDEPENDENT_PROVIDER_FAMILY
    checks.append(
        Check(
            name="review independence",
            status=Status.PASS if independent else Status.WARN,
            detail=str(resolved.independence),
            advice=resolved.note or "",
        )
    )
    if resolved.substitutions:
        note = "; ".join(resolved.substitutions)
    return Section(title="providers", checks=tuple(checks), note=note)


def _literature() -> Section:
    from research_os.errors import ResearchOSError
    from research_os.literature.config import config_path, load_config
    from research_os.literature.store import database_path

    checks: list[Check] = []
    try:
        config = load_config()
    except ResearchOSError as exc:
        return Section(
            title="literature",
            checks=(
                Check(
                    name="config",
                    status=Status.FAIL,
                    detail=str(exc),
                    advice=f"Fix or remove {config_path()}.",
                ),
            ),
        )

    checks.append(
        Check(
            name="config",
            status=Status.PASS,
            detail=str(config.source) if config.source else "built-in defaults",
        )
    )
    checks.append(
        Check(
            name="contact email",
            status=Status.PASS if config.contact_email else Status.WARN,
            detail=config.contact_email or "unset",
            advice=(
                ""
                if config.contact_email
                else "Scholarly APIs ask for a contact address and rate-limit "
                "anonymous callers harder. Set RESEARCH_OS_CONTACT_EMAIL."
            ),
        )
    )
    checks.append(
        Check(
            name="sources",
            status=Status.PASS
            if config.enabled_sources and not config.offline
            else Status.WARN,
            detail=(
                "offline"
                if config.offline
                else ", ".join(config.enabled_sources) or "none enabled"
            ),
            advice=(
                "Retrieval is switched off; the local index still works."
                if config.offline or not config.enabled_sources
                else ""
            ),
        )
    )
    database = database_path()
    checks.append(
        Check(
            name="index",
            status=Status.PASS,
            detail=(
                f"{database} ({_human_bytes(database.stat().st_size)})"
                if database.is_file()
                else f"{database} (not created yet)"
            ),
        )
    )
    return Section(title="literature", checks=tuple(checks))


def _experiments() -> Section:
    """Report what may be executed, without executing or submitting anything."""

    from research_os.errors import ResearchOSError
    from research_os.experiment.config import config_path, load_config
    from research_os.experiment.slurm import probe as probe_scheduler

    try:
        config = load_config()
    except ResearchOSError as exc:
        return Section(
            title="experiments",
            checks=(
                Check(
                    name="config",
                    status=Status.FAIL,
                    detail=str(exc),
                    advice=f"Fix or remove {config_path()}.",
                ),
            ),
        )

    declared = sum(len(project.commands) for project in config.projects.values())
    checks = [
        Check(
            name="config",
            status=Status.PASS,
            detail=str(config.source) if config.source else "built-in defaults",
        ),
        Check(
            name="declared commands",
            status=Status.PASS if declared else Status.WARN,
            detail=(
                ", ".join(
                    f"{project_id}: {', '.join(sorted(project.commands))}"
                    for project_id, project in sorted(config.projects.items())
                    if project.commands
                )
                or "none"
            ),
            advice=(
                ""
                if declared
                else "No experiment can run until you declare its command; "
                "this prints a starting point:\n"
                "     researchctl experiment example-config"
            ),
        ),
    ]
    scheduler = probe_scheduler(config.slurm)
    checks.append(
        Check(
            name="scheduler",
            status=Status.PASS if scheduler.available else Status.WARN,
            detail=(
                f"{scheduler.kind}"
                + (f" via {scheduler.ssh_host}" if scheduler.ssh_host else "")
                + (
                    f"   partitions: {', '.join(scheduler.partitions)}"
                    if scheduler.partitions
                    else ""
                )
            ),
            advice=scheduler.detail if not scheduler.available else "",
        )
    )
    return Section(title="experiments", checks=tuple(checks))


def _answer_command(run_id: str) -> str:
    return f'     researchctl research answer {run_id} --answer "..."'


def _runs() -> Section:
    """Report runs that are waiting on a person, or that stopped mid-flight.

    An interrupted run is the one thing here a researcher is likely not to know
    about: the process died, the state file still says ``EXECUTING``, and
    nothing will ever move it. Naming it, with the command that recovers it, is
    the whole point of this section.
    """

    from research_os.automation.models import TERMINAL_RUN_STATES, RunState
    from research_os.automation.store import RunStore
    from research_os.errors import ResearchOSError
    from research_os.research.models import ResearchState
    from research_os.research.store import ResearchStore

    checks: list[Check] = []
    waiting: list[str] = []
    stalled: list[str] = []
    try:
        for run_id in ResearchStore.list_run_ids():
            run = ResearchStore.open(run_id).load()
            if run.state is ResearchState.WAITING_FOR_HUMAN:
                waiting.append(run_id)
            elif run.state in {ResearchState.EXECUTING, ResearchState.PLANNING}:
                stalled.append(run_id)
    except ResearchOSError as exc:
        checks.append(Check("research runs", Status.FAIL, str(exc)))
    else:
        checks.append(
            Check(
                name="research runs waiting",
                status=Status.PASS if not waiting else Status.WARN,
                detail=", ".join(waiting) or "none",
                advice=(
                    "Each of these is waiting on you:\n"
                    + "\n".join(_answer_command(item) for item in waiting)
                    if waiting
                    else ""
                ),
            )
        )
        checks.append(
            Check(
                name="research runs interrupted",
                status=Status.PASS if not stalled else Status.WARN,
                detail=", ".join(stalled) or "none",
                advice=(
                    "These stopped mid-flight and nothing will move them. "
                    "Recover one, or end it with 'research cancel':\n"
                    + "\n".join(
                        f"     researchctl research resume {item}" for item in stalled
                    )
                    if stalled
                    else ""
                ),
            )
        )

    unfinished: list[str] = []
    try:
        for run_id in RunStore.list_run_ids():
            state = RunStore.open(run_id).load().state
            if state not in TERMINAL_RUN_STATES and state is not RunState.CREATED:
                unfinished.append(run_id)
    except ResearchOSError as exc:
        checks.append(Check("automation runs", Status.FAIL, str(exc)))
    else:
        checks.append(
            Check(
                name="automation runs unfinished",
                status=Status.PASS if not unfinished else Status.WARN,
                detail=", ".join(unfinished) or "none",
                advice=(
                    "Inspect them, then release their worktrees with "
                    "'auto cleanup':\n"
                    + "\n".join(
                        f"     researchctl auto report {item}" for item in unfinished
                    )
                    if unfinished
                    else ""
                ),
            )
        )
    return Section(title="runs", checks=tuple(checks))


def _disk(usage: Iterable[Usage]) -> Section:
    from research_os.paths import state_home

    root = state_home()
    try:
        free = shutil.disk_usage(root if root.exists() else root.parent).free
    except OSError as exc:
        return Section(
            title="disk",
            checks=(Check("free space", Status.WARN, f"cannot determine: {exc}"),),
        )
    reclaimable = sum(item.reclaimable for item in usage)
    checks = [
        Check(
            name="free space",
            status=Status.PASS if free >= LOW_DISK_BYTES else Status.WARN,
            detail=f"{_human_bytes(free)} free under {root}",
            advice=(
                ""
                if free >= LOW_DISK_BYTES
                else "A write-enabled run creates a worktree and may install a "
                "check environment. This is tight."
            ),
        ),
        Check(
            name="reclaimable",
            status=Status.PASS,
            detail=_human_bytes(reclaimable),
            advice=(
                "Release the worktrees and check environments finished runs "
                "still hold:\n     researchctl storage --reclaim"
                if reclaimable
                else ""
            ),
        ),
    ]
    return Section(title="disk", checks=tuple(checks))


# -- storage ------------------------------------------------------------------


def storage_usage() -> list[Usage]:
    """Measure every runtime store, and say how much of it is reclaimable.

    Reclaimable means exactly one thing: bulk that a finished run is still
    holding and that can be rebuilt from scratch -- an isolated worktree, a
    controller-owned check environment. Run records, event ledgers, prompts,
    model outputs, reviews and evidence packets are never counted, because they
    are how a run explains itself and no amount of disk pressure makes deleting
    them the right move.
    """

    from research_os.automation.store import runs_root, worktrees_root
    from research_os.experiment.store import experiments_root
    from research_os.insights.store import insights_root, nominations_root
    from research_os.literature.store import database_root, files_root
    from research_os.paper.store import drafts_root
    from research_os.proposal.store import proposals_root
    from research_os.research.store import research_root

    worktree_bytes, environment_bytes, hint = _reclaimable()
    reclaim = {
        "worktrees": (worktree_bytes, hint),
        "automation runs": (
            environment_bytes,
            "check environments held by finished runs" if environment_bytes else "",
        ),
    }
    entries = [
        ("research runs", research_root()),
        ("automation runs", runs_root()),
        ("worktrees", worktrees_root()),
        ("proposals", proposals_root()),
        ("experiment runs", experiments_root()),
        ("drafts", drafts_root()),
        ("insights", insights_root()),
        ("nominations", nominations_root()),
        ("literature index", database_root()),
        ("literature files", files_root()),
    ]
    found: list[Usage] = []
    for name, path in entries:
        count, size = _measure(path)
        reclaimable, note = reclaim.get(name, (0, ""))
        found.append(
            Usage(
                name=name,
                path=path,
                entries=count,
                bytes_used=size,
                reclaimable=reclaimable,
                reclaim_hint=note,
            )
        )
    return found


def _reclaimable() -> tuple[int, int, str]:
    """Return (worktree bytes, check-environment bytes, a one-line note).

    Counted only for runs that have finished. A run still in flight is holding
    its worktree because it is using it.
    """

    from research_os.automation.models import TERMINAL_RUN_STATES
    from research_os.automation.store import RUNTIME_DIRNAME, RunStore, runs_root
    from research_os.errors import ResearchOSError

    worktrees = 0
    environments = 0
    holding: set[str] = set()
    try:
        run_ids = RunStore.list_run_ids()
    except ResearchOSError:
        return 0, 0, ""
    for run_id in run_ids:
        try:
            run = RunStore.open(run_id).load()
        except ResearchOSError:
            continue
        if run.state not in TERMINAL_RUN_STATES:
            continue
        for record in run.worktrees:
            if record.removed_at is not None:
                continue
            _, size = _measure(Path(record.path))
            worktrees += size
            holding.add(run_id)
        _, runtime = _measure(runs_root() / run_id / RUNTIME_DIRNAME)
        environments += runtime
    note = f"{len(holding)} finished run(s) still hold a worktree" if holding else ""
    return worktrees, environments, note


def _measure(path: Path) -> tuple[int, int]:
    """Return (top-level entries, total bytes) for one directory.

    Symbolic links are measured as links and never followed, so a store that
    happens to contain one cannot make this walk wander off into a project or
    loop forever.
    """

    if not path.exists():
        return 0, 0
    if path.is_file():
        return 1, path.stat().st_size
    entries = 0
    total = 0
    try:
        entries = sum(1 for _ in path.iterdir())
    except OSError:
        return 0, 0
    for root, directories, files in os.walk(path, followlinks=False):
        directories[:] = [
            name for name in directories if not os.path.islink(os.path.join(root, name))
        ]
        for name in files:
            try:
                total += os.lstat(os.path.join(root, name)).st_size
            except OSError:
                continue
    return entries, total


def _human_bytes(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} TB"


# -- rendering ----------------------------------------------------------------

RULE = "=" * 72
THIN = "-" * 72


def render(report: Report, *, verbose: bool = False) -> str:
    """Render a diagnostic report for a terminal.

    Advice is printed under the check it belongs to rather than collected at
    the end, because a reader scanning for their own problem should not have to
    match it back up. Provider versions, auth strings and configuration paths
    all come from outside this process, so they go through the display
    sanitizer on the way out.
    """

    lines = ["", RULE, "Research OS", RULE]
    for section in report.sections:
        lines.extend(["", f"{section.status:4}  {section.title}", THIN])
        for check in section.checks:
            lines.append(
                f"  {check.status:4}  {check.name:26}  {terminal_safe(check.detail)}"
            )
            lines.extend(f"        {line}" for line in _wrap(check.advice))
        if section.note:
            lines.append(f"        note: {terminal_safe(section.note)}")

    if report.usage:
        lines.extend(["", THIN, "runtime storage", THIN])
        for item in report.usage:
            if not item.entries and not item.bytes_used and not verbose:
                continue
            row = (
                f"  {item.name:20}  {item.entries:>5} entries  "
                f"{_human_bytes(item.bytes_used):>10}"
            )
            if item.reclaimable:
                row += f"   ({_human_bytes(item.reclaimable)} reclaimable)"
            lines.append(row)
            if item.reclaim_hint:
                lines.append(f"        {terminal_safe(item.reclaim_hint)}")

    if not report.sections:
        lines.append("")
        return "\n".join(lines)

    lines.extend(
        [
            "",
            THIN,
            "summary",
            THIN,
            "  Nothing is broken."
            if report.ok
            else "  Something a researcher asked for is broken; see the FAIL rows.",
        ]
    )
    warnings = sum(
        1
        for section in report.sections
        for item in section.checks
        if item.status is Status.WARN
    )
    if warnings:
        noun = "capability is" if warnings == 1 else "capabilities are"
        lines.append(
            f"  {warnings} {noun} absent or degraded. That is a normal state to "
            "be in; the rows above say which."
        )
    lines.append("")
    return "\n".join(lines)


def _wrap(text: str, *, width: int = 64) -> list[str]:
    """Wrap advice, keeping any line the author indented exactly as written.

    An indented line is a command to copy. Wrapping one at the display width
    would break it in the middle, and a command a reader has to reassemble
    before running is worse than no command at all.
    """

    import textwrap

    if not text.strip():
        return []
    lines: list[str] = []
    for paragraph in terminal_safe(text).split("\n"):
        if paragraph.startswith(" "):
            lines.append(paragraph)
        elif paragraph.strip():
            lines.extend(textwrap.wrap(paragraph, width=width))
    return lines


# -- reclaiming ---------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Reclaimed:
    """What one reclaim pass released."""

    run_ids: tuple[str, ...]
    paths: tuple[str, ...]
    failures: tuple[str, ...]


def reclaim() -> Reclaimed:
    """Release the worktrees and check environments finished runs still hold.

    Delegated to the automation controller's own cleanup rather than walking
    the filesystem here. That routine knows which worktrees a run created,
    removes them through Git, and refuses any path outside the run directory --
    so the worst case of a bug in this function is that nothing is released,
    never that something outside runtime state is deleted.

    Records, ledgers, prompts, model outputs, reviews and branches are kept. A
    reclaimed run can still be read in full; it just cannot be resumed into the
    checkout it no longer has.
    """

    from research_os.automation.config import load_config
    from research_os.automation.controller import AutomationController
    from research_os.automation.models import TERMINAL_RUN_STATES
    from research_os.automation.store import RunStore
    from research_os.errors import ResearchOSError

    controller = AutomationController(providers={}, config=load_config(None))
    released: list[str] = []
    touched: list[str] = []
    failures: list[str] = []
    for run_id in RunStore.list_run_ids():
        try:
            store = RunStore.open(run_id)
            if store.load().state not in TERMINAL_RUN_STATES:
                continue
            _, paths = controller.cleanup(store)
        except (ResearchOSError, OSError) as exc:
            failures.append(f"{run_id}: {exc}")
            continue
        if paths:
            touched.append(run_id)
            released.extend(paths)
    return Reclaimed(
        run_ids=tuple(touched), paths=tuple(released), failures=tuple(failures)
    )


def render_reclaimed(result: Reclaimed) -> str:
    lines: list[str] = []
    if result.paths:
        lines.append(
            f"Released {len(result.paths)} runtime path(s) from "
            f"{len(result.run_ids)} finished run(s):"
        )
        lines.extend(f"  {path}" for path in result.paths)
    else:
        lines.append("Nothing to reclaim: no finished run is holding a worktree.")
    for failure in result.failures:
        lines.append(f"  could not clean up {terminal_safe(failure)}")
    lines.append("")
    lines.append(
        "Run records, event ledgers, prompts, model outputs, reviews and "
        "branches were kept. Nothing scientific was touched."
    )
    return "\n".join(lines) + "\n"
