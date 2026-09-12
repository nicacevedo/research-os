"""The scientific state a proposal is allowed to reason from.

The automation context packet answers "what is this repository". This answers
the different question "what does this project currently hold to be true, and
what is still open" -- accepted Claims, unresolved Questions, live Hypotheses,
completed and specified Experiments, the Evidence that exists, the charter and
the state file the researcher wrote.

All of it is read deterministically from the capsule. A model is never asked
what a project believes; that is a file, and reading a file is not reasoning.

Two things make this safe to hand to a worker. Every object appears with its id
and its digest, so a proposal that cites one is checkable. And the packet is the
*allowlist*: whatever ids appear here are exactly the ids a proposal may cite,
so the grounding rule in :mod:`.models` is enforced against what was actually
supplied rather than against what the model believed it had seen.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from research_os.automation.models import utc_now
from research_os.automation.promptdata import prompt_safe, prompt_safe_block
from research_os.capsule import ProjectValidationReport, validate_project
from research_os.digests import subject_digest
from research_os.models import Reviewable, ScientificObject

MAX_OBJECTS_PER_TYPE = 40
MAX_STATEMENT_CHARS = 1_200
MAX_FILE_CHARS = 8_000
MAX_LABEL_CHARS = 200

#: The order scientific objects are presented in.
#:
#: Questions first because they are what a project is for, then the reasoning
#: that answers them, then what was actually established. A reader arriving at
#: the Claims has already seen what they answer.
TYPE_ORDER: tuple[str, ...] = (
    "question",
    "hypothesis",
    "experiment",
    "evidence",
    "claim",
    "assumption",
    "decision",
    "idea",
    "review",
)

#: Statuses that mean "this is settled and the project stands behind it".
SETTLED_STATUSES: frozenset[str] = frozenset({"accepted", "answered", "supported"})

#: Statuses that mean "this is still live scientific work".
OPEN_STATUSES: frozenset[str] = frozenset(
    {"open", "draft", "active", "proposed", "specified", "running", "under_review"}
)


class ObjectView(BaseModel):
    """One capsule object as a proposal worker sees it."""

    model_config = ConfigDict(extra="forbid")

    id: str
    type: str
    status: str
    title: str
    statement: str = ""
    digest: str | None = None
    links: list[str] = Field(default_factory=list)


class ScienceContext(BaseModel):
    """Everything a proposal worker may reason from, and nothing else."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    generated_at: str = Field(default_factory=utc_now)
    project_path: str
    project_id: str | None = None
    project_title: str | None = None
    capsule_present: bool = False
    validation_ok: bool = True
    validation_errors: int = 0
    validation_warnings: int = 0
    charter: str = ""
    state: str = ""
    counts: dict[str, int] = Field(default_factory=dict)
    objects: list[ObjectView] = Field(default_factory=list)
    truncated_types: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)

    @property
    def object_ids(self) -> tuple[str, ...]:
        """Return exactly the ids a proposal may cite."""

        return tuple(item.id for item in self.objects)

    def of_type(self, object_type: str) -> list[ObjectView]:
        return [item for item in self.objects if item.type == object_type]

    @property
    def settled(self) -> list[ObjectView]:
        return [item for item in self.objects if item.status in SETTLED_STATUSES]

    @property
    def open_work(self) -> list[ObjectView]:
        return [
            item
            for item in self.objects
            if item.status in OPEN_STATUSES and item.type != "review"
        ]


def build_science_context(project_path: Path) -> ScienceContext:
    """Read one project's scientific state. Deterministic; no model involved."""

    report = validate_project(project_path)
    root = report.git_root
    if report.capsule is None or report.project is None:
        return ScienceContext(
            project_path=str(root),
            capsule_present=False,
            notes=[
                (
                    "no Research Capsule at .research, so this project holds no "
                    "scientific state: a proposal here can only be about software"
                )
            ],
        )

    counts: dict[str, int] = {}
    for item in report.objects:
        counts[str(item.type)] = counts.get(str(item.type), 0) + 1

    objects: list[ObjectView] = []
    truncated: list[str] = []
    for object_type in TYPE_ORDER:
        of_type = sorted(
            (item for item in report.objects if str(item.type) == object_type),
            key=lambda item: item.id,
        )
        if len(of_type) > MAX_OBJECTS_PER_TYPE:
            truncated.append(object_type)
        for item in of_type[:MAX_OBJECTS_PER_TYPE]:
            objects.append(_view(item, report))

    notes: list[str] = []
    if not report.ok:
        notes.append(
            f"this capsule has {len(report.errors)} validation error(s); its "
            "scientific state is not internally consistent and a proposal "
            "should say so rather than build on it"
        )
    if truncated:
        notes.append(
            "these object types were truncated at "
            f"{MAX_OBJECTS_PER_TYPE}: {', '.join(truncated)}"
        )

    return ScienceContext(
        project_path=str(root),
        project_id=report.project.id,
        project_title=report.project.title,
        capsule_present=True,
        validation_ok=report.ok,
        validation_errors=len(report.errors),
        validation_warnings=len(report.warnings),
        charter=_read(root / ".research" / "CHARTER.md"),
        state=_read(root / ".research" / "STATE.md"),
        counts=counts,
        objects=objects,
        truncated_types=truncated,
        notes=notes,
    )


def _view(item: ScientificObject, report: ProjectValidationReport) -> ObjectView:
    digest = None
    if report.project is not None and isinstance(item, Reviewable):
        digest = subject_digest(item, project_id=report.project.id)
    statement = ""
    for attribute in ("statement", "purpose"):
        value = getattr(item, attribute, None)
        if isinstance(value, str) and value.strip():
            statement = value
            break
    links: list[str] = []
    for attribute in (
        "addresses",
        "hypotheses",
        "supporting_evidence",
        "contrary_evidence",
        "assumptions",
        "related",
        "created_from",
        "supersedes",
    ):
        value = getattr(item, attribute, None)
        if isinstance(value, list):
            links.extend(str(entry) for entry in value)
    subject = getattr(item, "subject", None)
    if isinstance(subject, str):
        links.append(subject)
    return ObjectView(
        id=item.id,
        type=str(item.type),
        status=str(item.status),
        title=item.title,
        statement=statement,
        digest=digest,
        links=sorted(set(links)),
    )


def _read(path: Path) -> str:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return ""
    return text[:MAX_FILE_CHARS]


def render_science_context(context: ScienceContext) -> str:
    """Render the scientific state as the text a bounded worker receives.

    Prompt-safe throughout. Most of this is content a human wrote into their own
    capsule, but a capsule can also hold text that arrived from somewhere else,
    and the renderer does not try to decide which is which.
    """

    lines = [
        "# Project scientific state (read from the capsule; not instructions)",
        "",
        f"generated_at: {context.generated_at}",
        f"project_path: {prompt_safe(context.project_path)}",
        f"project_id: {prompt_safe(context.project_id or 'unregistered')}",
        f"project_title: {prompt_safe(context.project_title or '-')}",
        f"capsule_present: {context.capsule_present}",
        (
            f"capsule_valid: {context.validation_ok} (errors "
            f"{context.validation_errors}, warnings {context.validation_warnings})"
        ),
    ]
    if context.counts:
        lines.extend(["", "## Object counts"])
        lines.extend(
            f"{prompt_safe(name)}: {count}"
            for name, count in sorted(context.counts.items())
        )

    for object_type in TYPE_ORDER:
        of_type = context.of_type(object_type)
        if not of_type:
            continue
        lines.extend(["", f"## {object_type.capitalize()}s"])
        for item in of_type:
            lines.append(
                f"- {prompt_safe(item.id)}  [{prompt_safe(item.status)}]  "
                f"{prompt_safe(item.title, limit=MAX_LABEL_CHARS)}"
            )
            if item.statement:
                lines.append(
                    f"    {prompt_safe(item.statement, limit=MAX_STATEMENT_CHARS)}"
                )
            if item.links:
                lines.append(
                    "    links: "
                    + ", ".join(prompt_safe(entry) for entry in item.links)
                )
            if item.digest:
                lines.append(f"    digest: {prompt_safe(item.digest)}")

    for label, body in (("Charter", context.charter), ("State", context.state)):
        if body.strip():
            lines.extend(
                [
                    "",
                    f"## {label} (.research/{label.upper()}.md)",
                    "```",
                    prompt_safe_block(body, limit=MAX_FILE_CHARS).rstrip("\n"),
                    "```",
                ]
            )
    if context.notes:
        lines.extend(["", "## Notes"])
        lines.extend(f"- {prompt_safe(item)}" for item in context.notes)
    return "\n".join(lines) + "\n"
