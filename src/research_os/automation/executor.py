"""Bounded write-enabled execution inside one isolated worktree.

The worker is given a scope; the controller enforces it. After execution the
controller reads the worktree itself - changed paths, diff, HEAD - and fails the
work order when anything outside ``allowed_paths`` moved, regardless of what the
worker reported.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from research_os.automation.gitutil import (
    changed_paths,
    head_commit,
    stage_intent_to_add,
    working_diff,
    working_diff_stat,
)
from research_os.automation.models import WorkOrder

MAX_DIFF_CHARS = 400_000


@dataclass(frozen=True, slots=True)
class ExecutionEvidence:
    """What the controller observed in the worktree after the worker exited."""

    changed_paths: tuple[str, ...]
    diff: str
    diff_stat: str
    head_commit: str
    scope_violations: tuple[str, ...]

    @property
    def produced_changes(self) -> bool:
        return bool(self.changed_paths)


def build_coder_prompt(order: WorkOrder, *, context_text: str) -> str:
    """Return the complete prompt for the write-enabled coding worker."""

    allowed = "\n".join(f"- {item}" for item in order.allowed_paths)
    forbidden = (
        "\n".join(f"- {item}" for item in order.forbidden_paths)
        or "- (none beyond the rules below)"
    )
    commands = "\n".join(
        f"- {command.display}"
        + (f"  # {command.description}" if command.description else "")
        for command in order.acceptance_commands
    )
    artifacts = (
        "\n".join(f"- {item}" for item in order.expected_artifacts) or "- (none named)"
    )
    return f"""You are the coding worker of a deterministic research automation
controller. You are running inside a disposable, isolated Git worktree created
for this task alone. It is not the researcher's checkout.

TASK {order.task_id}: {order.title}

GOAL
{order.goal}

COMPLETION CONDITION
{order.completion_condition}

YOU MAY CHANGE ONLY THESE PATHS
{allowed}

YOU MUST NOT CHANGE THESE PATHS
{forbidden}

The controller compares the resulting diff against that list. Any change outside
it fails this task, whatever else you accomplished.

EXPECTED ARTIFACTS
{artifacts}

AFTER YOU STOP, the controller - not you - will run these commands in this
worktree and judge the result by their exit codes:
{commands}

RULES
- Do not create a commit, and do not run git, merge, push, or rebase.
- Do not install dependencies or access the network.
- Do not edit anything under a ".research/" directory: those are canonical
  scientific files that only a human may change.
- Do not edit test files unless they are explicitly listed above as changeable.
- Leave the change in the working tree as a reviewable diff.

When you are done, reply with a short report: what you changed, in which files,
and why you believe the completion condition is met.

{context_text}
"""


def collect_evidence(order: WorkOrder, *, worktree: Path) -> ExecutionEvidence:
    """Read the worktree and decide, locally, what the worker actually did."""

    stage_intent_to_add(worktree)
    changed = changed_paths(worktree)
    diff = working_diff(worktree)
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n[diff truncated by the controller]\n"
    return ExecutionEvidence(
        changed_paths=changed,
        diff=diff,
        diff_stat=working_diff_stat(worktree),
        head_commit=head_commit(worktree),
        scope_violations=scope_violations(
            changed,
            allowed=tuple(order.allowed_paths),
            forbidden=tuple(order.forbidden_paths),
        ),
    )


def scope_violations(
    paths: tuple[str, ...],
    *,
    allowed: tuple[str, ...],
    forbidden: tuple[str, ...],
) -> tuple[str, ...]:
    """Return every changed path the work order did not authorise.

    ``.research`` is refused unconditionally: no automated worker may edit
    canonical scientific files, and a plan cannot grant that authority.
    """

    violations: list[str] = []
    for path in paths:
        if path == ".research" or path.startswith(".research/"):
            violations.append(path)
            continue
        if any(_covers(entry, path) for entry in forbidden):
            violations.append(path)
            continue
        if not any(_covers(entry, path) for entry in allowed):
            violations.append(path)
    return tuple(violations)


def _covers(scope_entry: str, path: str) -> bool:
    """Return whether ``scope_entry`` names ``path`` or a directory holding it."""

    entry = scope_entry.rstrip("/")
    if not entry:
        return False
    return path == entry or path.startswith(f"{entry}/")
