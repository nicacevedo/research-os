"""Human-facing rendering of runs, plans, providers, and the final report.

The report exists so a person can decide what to do next without reading JSON.
It always names what was actually observed - the commands the controller ran and
their exit codes, the model each role really used, how independent the review
was - and it always ends with the exact next human action, because the run stops
short of merging on purpose.
"""

from __future__ import annotations

from research_os.automation.config import AutomationConfig, ResolvedRoles
from research_os.automation.controller import final_reviews, ready_for_human_blockers
from research_os.automation.models import (
    AnalystReport,
    AutomationRun,
    ProviderProbe,
    Role,
    RunState,
    WorkOrder,
)
from research_os.automation.store import RunStore
from research_os.automation.worktree import worktrees_root


def _role_line(run: AutomationRun, role: str) -> str:
    """Return the provider and model a role actually used, not what was configured."""

    used = [item for item in run.invocations if str(item.role) == role]
    if used:
        last = used[-1]
        return f"{last.provider} / {last.model or 'provider default'}"
    setting = run.roles.get(role)
    if setting is None:
        return "not configured"
    return f"{setting.provider} / {setting.model or 'provider default'} (not invoked)"


def _configured(run: AutomationRun, role: str) -> str:
    """Return the provider and model a role is configured to use."""

    setting = run.roles.get(role)
    if setting is None:
        return "not configured"
    return f"{setting.provider} / {setting.model or 'provider default'}"


def render_plan(run: AutomationRun) -> str:
    """Render the plan and the actions the run intends to take."""

    budget_line = (
        f"budget      {run.model_calls_used}/{run.budget.max_model_calls} model "
        f"calls used, {run.budget.max_write_work_orders} write work orders allowed"
    )
    lines = [
        "",
        f"Run {run.run_id}  [{run.state}]{'  DRY RUN' if run.dry_run else ''}",
        f"project     {run.project_path}",
        f"project_id  {run.project_id or 'unregistered'}",
        f"base        {run.base_commit}  ({run.base_branch or 'detached'})",
        f"goal        {run.goal}",
        "",
        f"planner     {_configured(run, 'planner')}",
        f"coder       {_configured(run, 'coder')}",
        f"reviewer    {_configured(run, 'reviewer')}",
        f"independence {run.independence or 'unknown'}",
        f"             {run.independence_note or ''}",
        "",
        budget_line,
    ]
    if run.plan_summary:
        lines.extend(["", "plan summary", f"  {run.plan_summary}"])
    if not run.work_orders:
        lines.extend(["", "no work orders (planner was not run)"])
    for order in run.work_orders:
        lines.extend(_render_order_intent(run, order))
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_order_intent(run: AutomationRun, order: WorkOrder) -> list[str]:
    from research_os.automation.worktree import branch_name, worktree_path

    writes = "yes" if not order.read_only else "no"
    tree = order.worktree_path or worktree_path(run.run_id, order.task_id)
    branch = order.branch or branch_name(run.run_id, order.task_id)
    analysis = order.role is Role.ANALYST
    lines = [
        "",
        f"{order.task_id}  {order.title}   [{order.status}]",
        f"  role              {order.role}",
        f"  goal              {order.goal}",
        f"  completion        {order.completion_condition}",
        f"  writes            {writes}  ({order.risk_class})",
    ]
    if analysis:
        lines.append(f"  read scope        {', '.join(order.read_paths) or '-'}")
    else:
        lines.append(f"  allowed paths     {', '.join(order.allowed_paths) or '-'}")
    if order.forbidden_paths:
        lines.append(f"  forbidden paths   {', '.join(order.forbidden_paths)}")
    lines.extend(
        [
            f"  {'snapshot' if analysis else 'worktree'}          {tree}",
            f"  branch            {branch}",
        ]
    )
    if order.dependencies:
        lines.append(f"  depends on        {', '.join(order.dependencies)}")
    if analysis:
        lines.append(
            "  no acceptance command runs: an analyst changes nothing to check"
        )
    else:
        lines.append("  acceptance commands run by the controller:")
        for command in order.acceptance_commands:
            lines.append(f"    {command.display}")
        lines.append("    git diff --check HEAD   (added by the controller)")
    if order.expected_artifacts:
        lines.append(f"  expected artifacts {', '.join(order.expected_artifacts)}")
    if order.failure_reason:
        lines.append(f"  failure           {order.failure_reason}")
    return lines


def render_status(run: AutomationRun, store: RunStore) -> str:
    """Render a compact status view of one run."""

    cost = run.total_cost_usd()
    lines = [
        f"run_id       {run.run_id}",
        f"state        {run.state}",
        f"dry_run      {run.dry_run}",
        f"project      {run.project_path}",
        f"project_id   {run.project_id or 'unregistered'}",
        f"goal         {run.goal}",
        f"base_commit  {run.base_commit}",
        f"created_at   {run.created_at}",
        f"updated_at   {run.updated_at}",
        f"model_calls  {run.model_calls_used}/{run.budget.max_model_calls}",
        f"cost_usd     {'unknown' if cost is None else f'{cost:.4f}'}",
        f"independence {run.independence or 'unknown'}",
        f"run_dir      {store.directory}",
    ]
    lines.append(f"repairs      {_repair_total(run)}/{run.budget.max_repair_attempts}")
    for order in run.work_orders:
        checks = ", ".join(
            f"{item.display}={_exit(item)}" for item in order.check_results
        )
        lines.append(
            f"{order.task_id}       {order.status}  [{order.role}]  {order.title}"
        )
        if order.branch:
            lines.append(f"             branch {order.branch}")
        if order.worktree_path:
            label = "snapshot" if order.role is Role.ANALYST else "worktree"
            lines.append(f"             {label} {order.worktree_path}")
        if order.role is Role.ANALYST:
            lines.append(f"             read scope {', '.join(order.read_paths)}")
            if order.analysis_path:
                lines.append(f"             analysis {order.analysis_path}")
        for artifact in order.dependency_artifacts:
            lines.append(
                f"             dependency input {artifact.task_id} "
                f"[{artifact.role}] {artifact.path}"
            )
        if checks:
            lines.append(f"             checks {checks}")
        if order.repair_attempts:
            lines.append(
                f"             repairs {order.repair_attempts} "
                f"({order.repair_reason or 'no reason recorded'})"
            )
    for outcome in run.reviews:
        lines.append(
            f"review       {outcome.task_id} {outcome.verdict} "
            f"({len(outcome.findings)} findings)"
        )
    if run.failure_reason:
        lines.append(f"failure      {run.failure_reason}")
    return "\n".join(lines) + "\n"


def render_report(run: AutomationRun, store: RunStore) -> str:
    """Render the full report a human reads before deciding anything."""

    cost = run.total_cost_usd()
    lines = [
        "",
        "=" * 72,
        f"Automation run {run.run_id}  —  {run.state}",
        "=" * 72,
        "",
        f"goal              {run.goal}",
        f"project           {run.project_path}",
        f"project_id        {run.project_id or 'unregistered (no capsule)'}",
        f"base commit       {run.base_commit}",
        f"base branch       {run.base_branch or 'detached'}",
        "",
        f"planner           {_role_line(run, 'planner')}",
        f"coder             {_role_line(run, 'coder')}",
        f"reviewer          {_role_line(run, 'reviewer')}",
        f"review quality    {run.independence or 'unknown'}",
        f"                  {run.independence_note or ''}",
        "",
        f"model calls       {run.model_calls_used} of {run.budget.max_model_calls}",
        (
            f"repair attempts   {_repair_total(run)} of "
            f"{run.budget.max_repair_attempts} allowed per work order"
        ),
        f"provider cost     {'unknown' if cost is None else f'USD {cost:.4f}'}",
        f"runtime ledger    {store.directory}",
    ]

    reviews = {outcome.task_id: outcome for outcome in final_reviews(run)}
    for order in run.work_orders:
        if order.role is Role.ANALYST:
            lines.extend(_render_analysis(order, store))
            continue
        lines.extend(
            [
                "",
                "-" * 72,
                f"{order.task_id}  {order.title}   [{order.status}]  [coder]",
                "-" * 72,
                f"branch            {order.branch or '-'}",
                f"worktree          {order.worktree_path or '-'}",
                f"worktree HEAD     {order.head_commit or '-'}",
                f"diff              {order.diff_path or '-'}",
                (
                    f"repair attempts   {order.repair_attempts} of "
                    f"{run.budget.max_repair_attempts}"
                ),
            ]
        )
        if order.repair_reason:
            lines.append(f"repair reason     {order.repair_reason}")
        if order.dependency_artifacts:
            lines.append("")
            lines.append("inputs from earlier work orders")
            for artifact in order.dependency_artifacts:
                lines.append(f"  {artifact.task_id} [{artifact.role}]  {artifact.path}")
                lines.append(f"    sha256 {artifact.sha256}")
        lines.extend(["", "files changed"])
        lines.extend(f"  {item}" for item in order.changed_paths or ["(none)"])
        lines.extend(["", "deterministic checks run by the controller"])
        if not order.check_results:
            lines.append("  (none were run)")
        for item in order.check_results:
            mark = "PASS" if item.ok else "FAIL"
            required = "required" if item.required else "advisory"
            lines.append(
                f"  {mark}  {item.display}   exit={_exit(item)}  "
                f"{required}  {item.duration_ms}ms"
            )
            if item.error:
                lines.append(f"        {item.error}")
        outcome = reviews.get(order.task_id)
        lines.extend(["", "independent review"])
        if outcome is None:
            lines.append("  (no review was recorded)")
        else:
            recorded = [item for item in run.reviews if item.task_id == order.task_id]
            if len(recorded) > 1:
                earlier = ", ".join(str(item.verdict) for item in recorded[:-1])
                lines.append(f"  earlier verdict {earlier}  (before the repair)")
            lines.append(f"  verdict         {outcome.verdict}")
            lines.append(
                f"  reviewer        {outcome.provider} / "
                f"{outcome.model or 'provider default'}"
            )
            lines.append(f"  independence    {outcome.independence}")
            lines.append(f"  summary         {outcome.summary}")
            if outcome.findings:
                lines.append("  unresolved findings")
                for finding in outcome.findings:
                    where = f" [{finding.path}]" if finding.path else ""
                    lines.append(f"    {finding.severity}{where}: {finding.message}")
            else:
                lines.append("  unresolved findings  (none)")
        if order.failure_reason:
            lines.extend(["", f"failure           {order.failure_reason}"])

    lines.extend(["", "=" * 72, "next human action", "=" * 72, ""])
    lines.extend(_next_action(run))
    lines.append("")
    return "\n".join(lines) + "\n"


def _render_analysis(order: WorkOrder, store: RunStore) -> list[str]:
    """Render one analysis work order and what it actually established."""

    lines = [
        "",
        "-" * 72,
        f"{order.task_id}  {order.title}   [{order.status}]  [analyst]",
        "-" * 72,
        f"snapshot          {order.worktree_path or '-'}",
        f"snapshot commit   {order.head_commit or order.base_commit}",
        f"read scope        {', '.join(order.read_paths) or '-'}",
        "tools             Read, Glob, Grep  (no Write, no Edit, no Bash)",
        f"analysis          {order.analysis_path or '-'}",
        "",
        "The analyst changed nothing: the controller compared this snapshot",
        "before and after it ran. No acceptance command was run for it.",
    ]
    report = _load_analysis(order, store)
    if report is None:
        if order.failure_reason:
            lines.extend(["", f"failure           {order.failure_reason}"])
        return lines
    lines.extend(["", "summary", f"  {report.summary}", "", "findings"])
    if not report.findings:
        lines.append("  (none reported)")
    for finding in report.findings:
        refs = ", ".join(finding.file_refs) or "no file named"
        lines.append(
            f"  {finding.id}  [{finding.importance}/{finding.confidence}]  {refs}"
        )
        lines.append(f"    {finding.statement}")
    lines.extend(["", "uncertainties the analyst reported"])
    if not report.uncertainties:
        lines.append("  (none)")
    lines.extend(f"  - {item}" for item in report.uncertainties)
    lines.extend(["", "recommended action", f"  {report.recommended_action}"])
    if order.failure_reason:
        lines.extend(["", f"failure           {order.failure_reason}"])
    return lines


def _load_analysis(order: WorkOrder, store: RunStore) -> AnalystReport | None:
    """Return the archived analysis for one order, or ``None`` if unusable."""

    if not order.analysis_path:
        return None
    try:
        raw = store.path(*order.analysis_path.split("/")).read_text(encoding="utf-8")
        return AnalystReport.model_validate_json(raw)
    except (OSError, ValueError):
        return None


def _repair_total(run: AutomationRun) -> int:
    return sum(order.repair_attempts for order in run.work_orders)


def _next_action(run: AutomationRun) -> list[str]:
    """Say exactly what the human does next. Nothing was merged for them."""

    if run.state is RunState.READY_FOR_HUMAN:
        written = [item for item in run.work_orders if item.role is not Role.ANALYST]
        if not written:
            return [
                "This run only analysed the repository. Nothing was changed,",
                "so there is no diff to merge; the findings are in the report",
                "above and archived under the run directory.",
                "",
                (
                    "Release the snapshot when you are done: "
                    f"researchctl auto cleanup {run.run_id}"
                ),
            ]
        order = written[-1]
        lines = [
            "Nothing has been merged, pushed, or scientifically accepted.",
            "",
            "1. Read the diff:",
            f"     git -C {order.worktree_path} diff HEAD",
            "",
            "2. If you accept it, merge it yourself from the project:",
            f"     git -C {run.project_path} merge --no-ff {order.branch}",
            "",
            "3. Release the worktree when you are done with it:",
            f"     researchctl auto cleanup {run.run_id}",
        ]
        if any(outcome.findings for outcome in run.reviews):
            lines.extend(
                [
                    "",
                    "The reviewer left findings above. They are advisory: the",
                    "controller neither applied nor dismissed them.",
                ]
            )
        return lines
    if run.state is RunState.FAILED:
        blockers = ready_for_human_blockers(run) or ["(no blocker recorded)"]
        lines = [
            f"This run failed: {run.failure_reason or 'no reason recorded'}",
            "",
            "Blockers:",
            *(f"  - {item}" for item in blockers),
            "",
            "Any worktree it created was deliberately left in place for you to",
            "inspect:",
        ]
        lines.extend(
            f"  {item.path}  ({item.branch})"
            for item in run.worktrees
            if item.removed_at is None
        )
        lines.append("")
        lines.append(
            f"Remove them when you are finished: researchctl auto cleanup {run.run_id}"
        )
        return lines
    if run.state is RunState.CANCELLED:
        return [
            f"This run was cancelled: {run.failure_reason or 'no reason recorded'}",
            f"Remove any worktree it created: researchctl auto cleanup {run.run_id}",
        ]
    if run.dry_run:
        return [
            "This was a dry run. No write-enabled worker was invoked and no",
            "worktree was created.",
            "",
            "Start a real run with the same goal when the plan above looks right.",
        ]
    if run.state is RunState.PLAN_READY:
        return [
            "The plan above has been validated but nothing has been executed.",
            "",
            f"     researchctl auto run {run.run_id}",
        ]
    return [
        f"The run is {run.state}. Inspect it with:",
        f"     researchctl auto status {run.run_id}",
    ]


def render_providers(
    probes: dict[str, ProviderProbe],
    *,
    config: AutomationConfig,
    resolved: ResolvedRoles | None,
) -> str:
    """Render local provider discovery and the resulting role assignment."""

    lines = ["", "Local provider discovery", ""]
    for name in sorted(probes):
        probe = probes[name]
        verified = "verified" if probe.noninteractive_verified else "not verified"
        lines.extend(
            [
                f"{name}",
                f"  executable            {probe.executable or 'not found'}",
                f"  available             {'yes' if probe.available else 'no'}",
                f"  noninteractive        {verified}",
                f"  version               {probe.version or 'unknown'}",
                f"  authentication        {probe.auth_status}",
                f"  detail                {probe.detail}",
            ]
        )
    lines.extend(
        [
            "",
            f"config file             {config.source or 'built-in defaults'}",
            f"allowed check programs  {', '.join(config.allowed_check_programs)}",
            "",
        ]
    )
    if resolved is None:
        lines.append("no provider is available, so no role could be assigned")
        return "\n".join(lines) + "\n"
    lines.append("Role assignment")
    for role in ("planner", "analyst", "coder", "reviewer"):
        setting = resolved.roles.get(role)
        if setting is None:
            continue
        tools = ", ".join(setting.tools) or "none"
        lines.append(
            f"  {role:9} {setting.provider} / "
            f"{setting.model or 'provider default'}  "
            f"[{setting.access}; tools: {tools}]"
        )
    for substitution in resolved.substitutions:
        lines.append(f"  note: {substitution}")
    lines.extend(
        [
            "",
            f"review independence     {resolved.independence}",
            f"                        {resolved.note}",
            "",
            f"worktree root           {worktrees_root()}",
            "",
        ]
    )
    return "\n".join(lines) + "\n"


def _exit(item: object) -> str:
    code = getattr(item, "exit_code", None)
    if getattr(item, "timed_out", False):
        return "timeout"
    return "none" if code is None else str(code)
