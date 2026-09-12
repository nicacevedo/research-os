"""Rendering an execution and its candidate packet for a human.

What every view insists on saying:

**An unobserved measurement is "unknown", never a number.** A local run has no
scheduler to ask for peak memory; a queued job has no exit code. Both render as
unknown, because a report that guesses gets quoted later as a measurement.

**A candidate packet is a candidate.** It says so, and it says what the human
action would be. Nothing in this subsystem creates Evidence.

**Undeclared writes are shown.** A run that wrote somewhere its specification
did not mention is not an error, but it is how a result quietly comes to depend
on a file nobody tracked.
"""

from __future__ import annotations

from research_os.experiment.config import ExperimentConfig
from research_os.experiment.models import (
    EvidencePacket,
    ExecutionState,
    ExperimentRun,
    ResourceUsage,
)
from research_os.experiment.slurm import SchedulerProbe
from research_os.textsafe import terminal_safe

RULE = "=" * 72
THIN = "-" * 72


def render_run(run: ExperimentRun, packet: EvidencePacket | None = None) -> str:
    """Render one execution in full."""

    lines = [
        "",
        RULE,
        f"Experiment run {run.run_id}  —  {run.state}",
        RULE,
        "",
        f"task            {run.task_name}",
        f"project         {run.project_path}",
        f"project_id      {run.project_id or 'unregistered'}",
        f"base commit     {run.base_commit or 'none'}",
        f"worktree        {run.worktree_path or '-'}",
        f"executor        {run.executor}",
        f"command         {' '.join(run.argv)}",
        f"authorised by   {run.authorized_by or 'not recorded'}",
        f"config digest   {run.config_digest or 'not recorded'}",
        f"created         {run.created_at}",
        f"started         {run.started_at or '-'}",
        f"ended           {run.ended_at or '-'}",
        f"exit code       {_unknown(run.exit_code)}",
    ]
    if run.parameters:
        lines.append("")
        lines.append("parameters")
        lines.extend(
            f"  {name:<20} {value}" for name, value in sorted(run.parameters.items())
        )
    if run.scheduler is not None:
        lines.extend(
            [
                "",
                "scheduler",
                f"  job id          {run.scheduler.job_id}",
                f"  partition       {run.scheduler.partition or '-'}",
                f"  account         {run.scheduler.account or '-'}",
                f"  host            {run.scheduler.host or 'this machine'}",
                f"  reported state  {run.scheduler.raw_state or 'not polled'}",
                f"  exit code       {_unknown(run.scheduler.exit_code)}",
            ]
        )
        if run.scheduler.reason:
            lines.append(f"  reason          {run.scheduler.reason}")

    lines.extend(["", "resource usage", *_usage_lines(run.usage)])

    lines.extend(["", "artifacts"])
    if not run.artifacts:
        lines.append("  (none were produced)")
    for item in run.artifacts:
        marker = "" if item.declared else "   UNDECLARED"
        lines.append(f"  {item.path}{marker}")
        lines.append(f"      {item.byte_size} bytes   sha256 {item.sha256}")
    if run.missing_outputs:
        lines.extend(
            [
                "",
                "declared outputs that were NOT produced",
                *(f"  {item}" for item in run.missing_outputs),
            ]
        )
    if run.failure_reason:
        lines.extend(["", f"failure         {run.failure_reason}"])
    if run.stdout_path:
        lines.extend(
            [
                "",
                f"stdout          {run.stdout_path}",
                f"stderr          {run.stderr_path}",
            ]
        )
    if packet is not None:
        lines.extend(render_packet_section(packet))
    lines.extend(["", RULE, "next human action", RULE, "", *_next_action(run, packet)])
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_packet_section(packet: EvidencePacket) -> list[str]:
    """Render the candidate packet, labelled so it cannot read as Evidence."""

    lines = [
        "",
        THIN,
        "candidate evidence packet (NOT capsule Evidence, NOT accepted)",
        THIN,
        f"  packet id       {packet.packet_id}",
        f"  usable          {'yes' if packet.usable else 'no'}",
    ]
    lines.append("  checks")
    if not packet.checks:
        lines.append("    (the command declared none)")
    for item in packet.checks:
        mark = "PASS" if item.passed else "FAIL"
        lines.append(f"    {mark}  {item.name}: {item.detail}")
    if packet.blocking_notes:
        lines.append("  why it is not usable")
        lines.extend(f"    - {item}" for item in packet.blocking_notes)
    if packet.notes:
        lines.append("  notes")
        lines.extend(f"    - {item}" for item in packet.notes)
    lines.extend(
        [
            "",
            "  This packet reports what a process produced. It contains no",
            "  judgement about whether the results support or contradict any",
            "  hypothesis: that is yours to make, and turning any of this into",
            "  capsule Evidence is your decision and your action.",
        ]
    )
    return lines


def _usage_lines(usage: ResourceUsage) -> list[str]:
    if not usage.anything_observed:
        return [
            "  (nothing could be observed for this execution; no value is",
            "   estimated, because an estimate would be quoted later as a",
            "   measurement)",
        ]
    return [
        f"  wall clock      {_seconds(usage.wall_clock_seconds)}",
        f"  cpu time        {_seconds(usage.cpu_seconds)}",
        f"  peak memory     {_memory(usage.max_rss_kb)}",
        f"  nodes           {_unknown(usage.node_count)}",
        f"  cpus            {_unknown(usage.cpu_count)}",
        f"  observed by     {usage.observed_by or 'not recorded'}",
    ]


def render_scheduler_probe(found: SchedulerProbe, config: ExperimentConfig) -> str:
    """Render whether an experiment could be submitted from this machine."""

    lines = [
        "",
        "Scheduler",
        "",
        f"  available       {'yes' if found.available else 'no'}",
        f"  executor        {found.kind}",
        f"  ssh host        {found.ssh_host or 'not configured (local submission)'}",
        f"  partitions      {', '.join(found.partitions) or 'none configured'}",
    ]
    if found.partitions:
        lines.append(
            f"  default         {found.partitions[0]}   (the first entry; list your "
            "preferred partition first)"
        )
    for name, path in sorted(found.executables.items()):
        lines.append(f"  {name:<15} {path or 'not found'}")
    lines.extend(
        [
            f"  detail          {found.detail}",
            "",
            f"  config file     {config.source or 'built-in defaults (nothing declared)'}",
            "",
        ]
    )
    return terminal_safe("\n".join(lines) + "\n")


def render_command_list(config: ExperimentConfig, project_id: str | None) -> str:
    """Render every command declared for one project."""

    project = config.for_project(project_id)
    lines = [
        "",
        f"Declared experiment commands for {project_id or '(unregistered)'}",
        "",
    ]
    if not project.commands:
        lines.extend(
            [
                "  (none)",
                "",
                "  Experiment commands are declared by you, in",
                f"  {config.source or 'a file that does not exist yet'}.",
                "  That file lives outside every automation worktree, so a worker",
                "  cannot add a command or change what one runs.",
                "",
            ]
        )
        return terminal_safe("\n".join(lines) + "\n")
    for name, spec in sorted(project.commands.items()):
        lines.append(f"  {name}   [{spec.executor}]")
        if spec.description:
            lines.append(f"      {spec.description}")
        lines.append(f"      command    {' '.join(spec.argv)}")
        if spec.parameters:
            lines.append("      parameters")
            for parameter in spec.parameters:
                bounds = ""
                if parameter.choices:
                    bounds = f"  one of: {', '.join(parameter.choices)}"
                elif parameter.minimum is not None or parameter.maximum is not None:
                    bounds = f"  range: {parameter.minimum}..{parameter.maximum}"
                need = "required" if parameter.required else "optional"
                lines.append(
                    f"        {parameter.name:<16} {parameter.type}  {need}{bounds}"
                )
        if spec.outputs:
            lines.append(f"      outputs    {', '.join(spec.outputs)}")
        if spec.checks:
            lines.append(f"      checks     {', '.join(spec.checks)}")
        lines.append(f"      timeout    {spec.timeout_seconds}s")
        lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def render_run_list(runs: list[ExperimentRun]) -> str:
    lines = [""]
    if not runs:
        lines.extend(["no experiment runs yet", ""])
        return terminal_safe("\n".join(lines) + "\n")
    for run in runs:
        lines.append(f"{run.state:12} {run.run_id}  {run.task_name}  [{run.executor}]")
        lines.append(f"    {run.project_path}")
        if run.scheduler is not None:
            lines.append(f"    job {run.scheduler.job_id}")
    lines.append("")
    return terminal_safe("\n".join(lines) + "\n")


def _next_action(run: ExperimentRun, packet: EvidencePacket | None) -> list[str]:
    if run.state in {
        ExecutionState.SUBMITTED,
        ExecutionState.PENDING,
        ExecutionState.RUNNING,
    }:
        return [
            "This job is with the scheduler. Ask again when you expect it to be done:",
            "",
            f"     researchctl experiment poll {run.run_id}",
        ]
    if run.state is ExecutionState.UNKNOWN:
        return [
            "Neither the queue nor the accounting database knows this job. It may",
            "have aged out of accounting, or the scheduler may be unreachable.",
            "Research OS will not guess what happened to it.",
        ]
    if packet is None:
        return [
            "No candidate evidence packet was built for this run.",
            f"     researchctl experiment show {run.run_id}",
        ]
    if packet.usable:
        return [
            "The execution finished, produced everything it declared, and passed",
            "every check. The packet above is a candidate: read it, and if you",
            "agree with what it shows, record Evidence in your capsule yourself.",
            "",
            "Research OS does not create Evidence and did not create any here.",
        ]
    return [
        "This run did not produce a usable result:",
        *(f"  - {item}" for item in packet.blocking_notes),
        "",
        "Nothing about it should be read as a result.",
    ]


def _unknown(value: object) -> str:
    return "unknown" if value is None else str(value)


def _seconds(value: float | None) -> str:
    return "unknown" if value is None else f"{value:.3f}s"


def _memory(value: int | None) -> str:
    if value is None:
        return "unknown"
    return f"{value} KB ({value / 1024:.1f} MB)"
