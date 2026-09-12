"""The deterministic path from a writing request to a draft on a branch.

The same shape as the code controller, for the same reasons, and reusing the
same isolation: an isolated worktree per task, a scope the controller enforces
from the observed diff, an outbound-symlink gate before the writer runs, one
bounded repair, and a stop at READY_FOR_HUMAN with nothing merged.

What differs is what is checked. A coding task is checked by running commands; a
writing task is checked by resolving what the prose asserts against what the
task was given. Those checks are deterministic and run before the reviewer, so
the reviewer spends its attention on judgement rather than bookkeeping.

The run ends with a branch and a report. It never merges, never pushes, never
creates capsule Evidence, and never marks anything accepted.
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass, field
from pathlib import Path

from research_os.automation.config import AutomationConfig, ResolvedRoles, resolve_roles
from research_os.automation.executor import collect_evidence
from research_os.automation.filescope import assert_contained_symlinks
from research_os.automation.gitutil import has_commits, head_commit, repository_root
from research_os.automation.models import (
    Access,
    ModelInvocation,
    Role,
    RoleSetting,
    utc_now,
)
from research_os.automation.providers import (
    InvocationRequest,
    InvocationResult,
    ProviderAdapter,
    probe_registry,
)
from research_os.automation.worktree import assert_isolated, create_worktree
from research_os.errors import (
    AutomationError,
    PaperWritingError,
    ProviderInvocationError,
    ProviderUnavailableError,
)
from research_os.paper.checks import check_draft, read_written
from research_os.paper.models import (
    DraftRecord,
    GroundingReport,
    SectionKind,
    SourceManifest,
    SourcePacket,
    WritingReview,
    WritingVerdict,
)
from research_os.paper.reviewer import (
    REVIEW_SCHEMA,
    build_writing_review_prompt,
    parse_writing_review,
    render_writing_findings,
)
from research_os.paper.store import DraftStore, make_draft_id
from research_os.paper.writer import (
    MANIFEST_SCHEMA,
    build_repair_prompt,
    build_writer_prompt,
    parse_manifest,
)

WRITER_TIMEOUT_SECONDS = 1800
REVIEWER_TIMEOUT_SECONDS = 900

#: How many model calls one writing task may spend.
#:
#: Three: the writer, one bounded repair, and the review that must follow it. A
#: writing task that needed more would be a run, and a run is what the
#: automation controller is for.
MAX_MODEL_CALLS = 3

#: The most files one writing task may declare as its scope.
#:
#: A manuscript section is a small number of files. A task scoped to dozens is
#: not a section, and the controller would be enforcing a scope nobody read.
MAX_ALLOWED_PATHS = 12


@dataclass
class DraftOutcome:
    """Everything one writing task produced."""

    store: DraftStore
    draft: DraftRecord
    packet: SourcePacket
    invocations: list[ModelInvocation] = field(default_factory=list)

    @property
    def model_calls(self) -> int:
        return len(self.invocations)

    @property
    def ready_for_human(self) -> bool:
        return self.draft.ready_for_human


@dataclass
class PaperController:
    """Drives one writing task to a reviewed draft on an isolated branch."""

    providers: dict[str, ProviderAdapter]
    config: AutomationConfig
    allow_repair: bool = True

    def write(
        self,
        *,
        project_path: Path,
        section: SectionKind,
        instruction: str,
        packet: SourcePacket,
        allowed_paths: list[str],
    ) -> DraftOutcome:
        """Draft one manuscript section and check it against its sources."""

        instruction = instruction.strip()
        if not instruction:
            raise PaperWritingError("a writing task needs an instruction")
        if not allowed_paths:
            raise PaperWritingError(
                "a writing task needs at least one path it may write to"
            )
        if len(allowed_paths) > MAX_ALLOWED_PATHS:
            raise PaperWritingError(
                f"a writing task may name at most {MAX_ALLOWED_PATHS} paths; this "
                f"one names {len(allowed_paths)}. A scope nobody read is not a scope."
            )
        root = repository_root(project_path)
        resolved = self._resolve_roles()
        created_at = utc_now()
        draft = DraftRecord(
            draft_id=make_draft_id(
                project_path=str(root), section=str(section), created_at=created_at
            ),
            project_id=packet.project_id,
            project_path=str(root),
            base_commit=head_commit(root) if has_commits(root) else None,
            section=section,
            instruction=instruction,
            allowed_paths=list(allowed_paths),
            created_at=created_at,
        )
        store = DraftStore.create(draft)
        store.save_packet(packet)
        store.append_event(
            "draft_started",
            section=str(section),
            allowed_paths=list(allowed_paths),
            claims=len(packet.claims),
            evidence=len(packet.evidence),
            experiments=len(packet.experiments),
            literature=len(packet.literature),
            excluded_claims=sorted(packet.excluded_claims),
        )

        record = create_worktree(
            run_id=_run_id_for(draft.draft_id),
            task_id="T-001",
            repository=root,
            base_commit=draft.base_commit or "",
        )
        worktree = Path(record.path)
        draft = draft.model_copy(
            update={"worktree_path": record.path, "branch": record.branch}
        )
        store.save(draft)
        store.append_event(
            "worktree_created",
            path=record.path,
            branch=record.branch,
            base_commit=record.base_commit,
        )
        assert_isolated(record, canonical_repository=root)
        assert_contained_symlinks(worktree)

        invocations: list[ModelInvocation] = []
        setting = self._writer_setting(resolved)
        existing = read_written(worktree, allowed_paths)
        prompt = build_writer_prompt(
            section=section,
            instruction=instruction,
            packet=packet,
            allowed_paths=allowed_paths,
            existing=existing,
        )
        draft, manifest = self._invoke_writer(
            store, draft, setting, prompt, worktree, invocations, root
        )

        grounding = self._check(store, draft, packet, manifest, worktree)
        draft = store.save(
            draft.model_copy(update={"manifest": manifest, "grounding": grounding})
        )

        review = self._review(
            store, draft, packet, manifest, grounding, resolved, invocations
        )
        draft = store.save(draft.model_copy(update={"review": review}))

        needs_repair = not grounding.grounded or (
            review.verdict is WritingVerdict.PASS_WITH_REPAIR
        )
        if needs_repair and self.allow_repair and draft.repair_attempts == 0:
            draft, manifest, grounding, review = self._repair(
                store,
                draft,
                packet,
                manifest,
                grounding,
                review,
                resolved,
                worktree,
                invocations,
                root,
            )

        draft = store.save(
            draft.model_copy(
                update={
                    "manifest": manifest,
                    "grounding": grounding,
                    "review": review,
                    "finished_at": utc_now(),
                }
            )
        )
        store.append_event(
            "draft_finished",
            grounded=grounding.grounded,
            verdict=str(review.verdict),
            blockers=[item.message for item in grounding.blockers],
            repair_attempts=draft.repair_attempts,
            ready_for_human=draft.ready_for_human,
        )
        return DraftOutcome(
            store=store, draft=draft, packet=packet, invocations=invocations
        )

    # -- phases ----------------------------------------------------------

    def _invoke_writer(
        self,
        store: DraftStore,
        draft: DraftRecord,
        setting: RoleSetting,
        prompt: str,
        worktree: Path,
        invocations: list[ModelInvocation],
        root: Path,
    ) -> tuple[DraftRecord, SourceManifest]:
        invocation_id = f"INV-{len(invocations) + 1:04d}"
        store.write_text(f"prompts/{invocation_id}.txt", prompt)
        invocation, result = self._invoke(
            role=Role.CODER,
            setting=setting,
            prompt=prompt,
            cwd=worktree,
            timeout_seconds=WRITER_TIMEOUT_SECONDS,
            json_schema=MANIFEST_SCHEMA,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        store.write_text(f"model_outputs/{invocation_id}.txt", result.text or "")
        if not result.ok:
            store.save(
                draft.model_copy(
                    update={"failure_reason": invocation.error or "writer failed"}
                )
            )
            raise ProviderInvocationError(
                f"the writing worker failed: {invocation.error or 'unknown error'}"
            )
        manifest = parse_manifest(
            structured=result.structured,
            text=result.text,
            draft_id=draft.draft_id,
            section=draft.section,
        )
        evidence = collect_evidence(_scope_order(draft, root), worktree=worktree)
        if not evidence.contained:
            raise PaperWritingError(
                f"{draft.draft_id}: symlinks resolving outside the worktree: "
                + ", ".join(evidence.outbound_symlinks)
            )
        if evidence.scope_violations:
            store.append_event("scope_violation", paths=list(evidence.scope_violations))
            store.save(
                draft.model_copy(
                    update={
                        "failure_reason": (
                            "the writer changed paths outside its scope: "
                            + ", ".join(evidence.scope_violations)
                        )
                    }
                )
            )
            raise PaperWritingError(
                f"{draft.draft_id} changed paths outside its scope: "
                + ", ".join(evidence.scope_violations)
            )
        diff_path = store.write_text("draft/diff.patch", evidence.diff)
        updated = store.save(
            draft.model_copy(
                update={
                    "changed_paths": list(evidence.changed_paths),
                    "diff_path": store.relative(diff_path),
                    "invocation_ids": [*draft.invocation_ids, invocation_id],
                    "provider": setting.provider,
                    "model": invocation.model or setting.model,
                }
            )
        )
        store.append_event(
            "draft_written",
            changed_paths=list(evidence.changed_paths),
            invocation_id=invocation_id,
        )
        return updated, manifest

    def _check(
        self,
        store: DraftStore,
        draft: DraftRecord,
        packet: SourcePacket,
        manifest: SourceManifest,
        worktree: Path,
    ) -> GroundingReport:
        written = read_written(worktree, sorted(set(draft.changed_paths)))
        grounding = check_draft(
            packet=packet, manifest=manifest, written=written, section=draft.section
        )
        store.save_grounding(grounding)
        store.append_event(
            "grounding_checked",
            grounded=grounding.grounded,
            issues=len(grounding.issues),
            blockers=[item.check for item in grounding.blockers],
            cited=grounding.cited_keys,
            unsupported_numbers=grounding.unsupported_numbers,
        )
        return grounding

    def _review(
        self,
        store: DraftStore,
        draft: DraftRecord,
        packet: SourcePacket,
        manifest: SourceManifest,
        grounding: GroundingReport,
        resolved: ResolvedRoles,
        invocations: list[ModelInvocation],
    ) -> WritingReview:
        setting = resolved.roles["reviewer"]
        diff = ""
        if draft.diff_path:
            diff = store.path(*draft.diff_path.split("/")).read_text(encoding="utf-8")
        prompt = build_writing_review_prompt(
            section=draft.section,
            instruction=draft.instruction,
            packet=packet,
            manifest=manifest,
            grounding=grounding,
            diff=diff,
        )
        invocation_id = f"INV-{len(invocations) + 1:04d}"
        store.write_text(f"prompts/{invocation_id}.txt", prompt)
        invocation, result = self._invoke(
            role=Role.REVIEWER,
            setting=setting,
            prompt=prompt,
            cwd=store.path("review"),
            timeout_seconds=REVIEWER_TIMEOUT_SECONDS,
            json_schema=REVIEW_SCHEMA,
            invocation_id=invocation_id,
        )
        invocations.append(invocation)
        store.write_text(f"model_outputs/{invocation_id}.txt", result.text or "")
        if not result.ok:
            raise ProviderInvocationError(
                f"the writing reviewer failed: {invocation.error or 'unknown error'}"
            )
        review = parse_writing_review(
            structured=result.structured,
            text=result.text,
            draft_id=draft.draft_id,
            provider=setting.provider,
            model=invocation.model or setting.model,
            independence=str(resolved.independence),
            independence_note=resolved.note,
            invocation_id=invocation_id,
        )
        store.save_review(review)
        store.append_event(
            "draft_reviewed",
            verdict=str(review.verdict),
            findings=len(review.findings),
            independence=str(resolved.independence),
        )
        return review

    def _repair(
        self,
        store: DraftStore,
        draft: DraftRecord,
        packet: SourcePacket,
        manifest: SourceManifest,
        grounding: GroundingReport,
        review: WritingReview,
        resolved: ResolvedRoles,
        worktree: Path,
        invocations: list[ModelInvocation],
        root: Path,
    ) -> tuple[DraftRecord, SourceManifest, GroundingReport, WritingReview]:
        """Make the one bounded repair, then re-establish everything.

        Nothing is widened for it: the same worktree, the same scope, the same
        sources. What the repair gets is evidence -- the exact deterministic
        findings and the reviewer's text -- and no additional authority.
        """

        reason = (
            "deterministic grounding checks failed"
            if not grounding.grounded
            else f"the reviewer returned {review.verdict}"
        )
        store.append_event("repair_started", reason=reason, attempt=1)
        assert_contained_symlinks(worktree)
        diff = ""
        if draft.diff_path:
            diff = store.path(*draft.diff_path.split("/")).read_text(encoding="utf-8")
        prompt = build_repair_prompt(
            section=draft.section,
            instruction=draft.instruction,
            packet=packet,
            allowed_paths=list(draft.allowed_paths),
            grounding=grounding,
            review_findings=(
                render_writing_findings(review)
                if review.verdict is WritingVerdict.PASS_WITH_REPAIR
                else None
            ),
            diff=diff,
        )
        draft = draft.model_copy(update={"repair_attempts": 1, "repair_reason": reason})
        store.save(draft)
        draft, manifest = self._invoke_writer(
            store,
            draft,
            self._writer_setting(resolved),
            prompt,
            worktree,
            invocations,
            root,
        )
        grounding = self._check(store, draft, packet, manifest, worktree)
        draft = store.save(
            draft.model_copy(update={"manifest": manifest, "grounding": grounding})
        )
        review = self._review(
            store, draft, packet, manifest, grounding, resolved, invocations
        )
        store.append_event(
            "repair_completed",
            grounded=grounding.grounded,
            verdict=str(review.verdict),
        )
        return draft, manifest, grounding, review

    # -- primitives ------------------------------------------------------

    def _invoke(
        self,
        *,
        role: Role,
        setting: RoleSetting,
        prompt: str,
        cwd: Path,
        timeout_seconds: int,
        json_schema: dict | None,
        invocation_id: str,
    ) -> tuple[ModelInvocation, InvocationResult]:
        adapter = self.providers.get(setting.provider)
        if adapter is None:
            raise ProviderUnavailableError(
                f"no adapter for provider {setting.provider!r}"
            )
        cwd.mkdir(parents=True, exist_ok=True)
        started = utc_now()
        monotonic = time.monotonic()
        result = adapter.invoke(
            InvocationRequest(
                role=role,
                prompt=prompt,
                cwd=cwd,
                read_only=setting.read_only,
                timeout_seconds=timeout_seconds,
                model=setting.model,
                effort=setting.effort,
                access=setting.access,
                tools=tuple(setting.tools),
                json_schema=json_schema,
            )
        )
        invocation = ModelInvocation(
            invocation_id=invocation_id,
            run_id=_run_id_for(invocation_id + started),
            role=role,
            provider=setting.provider,
            model=result.resolved_model or setting.model,
            effort=setting.effort,
            read_only=setting.read_only,
            cwd=str(cwd),
            started_at=started,
            ended_at=utc_now(),
            duration_ms=int((time.monotonic() - monotonic) * 1000),
            timeout_seconds=timeout_seconds,
            timed_out=result.timed_out,
            exit_code=result.exit_code,
            provider_session_id=result.session_id,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            total_cost_usd=result.total_cost_usd,
            error=result.error,
        )
        return invocation, result

    def _resolve_roles(self) -> ResolvedRoles:
        return resolve_roles(self.config, probe_registry(self.providers))

    @staticmethod
    def _writer_setting(resolved: ResolvedRoles) -> RoleSetting:
        """Return the write role, refusing anything that is not isolated-write.

        The writer is the one worker here with write tools, so the check that it
        really is the isolated-write position is worth making explicitly rather
        than inheriting from configuration.
        """

        setting = resolved.roles.get("coder")
        if setting is None:
            raise AutomationError(
                "this configuration declares no write role, so a writing task "
                "cannot be dispatched"
            )
        if setting.access is not Access.ISOLATED_WRITE or setting.read_only:
            raise AutomationError(
                f"the write role declares {setting.access}; a writing task runs "
                "a write-enabled worker in its own disposable worktree"
            )
        return setting


def _scope_order(draft: DraftRecord, root: Path):
    """Return the work order the scope enforcement reads.

    Built from the draft rather than kept as state: scope enforcement is the
    automation controller's, and reusing it means a writing task's scope is
    enforced by exactly the code a coding task's is -- including the
    unconditional refusal of anything under ``.research/``.
    """

    from research_os.automation.models import (
        ExpectedOutput,
        RiskClass,
        WorkOrder,
    )

    return WorkOrder(
        task_id="T-001",
        title=f"write the {draft.section} section",
        goal=draft.instruction,
        role=Role.CODER,
        risk_class=RiskClass.WRITE_ISOLATED,
        project_path=str(root),
        base_commit=draft.base_commit or "0" * 40,
        read_only=False,
        allowed_paths=list(draft.allowed_paths),
        completion_condition="the draft is grounded in its supplied sources",
        timeout_seconds=WRITER_TIMEOUT_SECONDS,
        provider=draft.provider or "unknown",
        expected_output=ExpectedOutput.DIFF,
    )


def _run_id_for(material: str) -> str:
    """Return a run id shaped like an automation run id, for the shared models.

    The worktree helpers and ``ModelInvocation`` are shared with the automation
    control plane and both require one. A writing task is not an automation run,
    so this derives a stable id from the draft rather than pretending one exists;
    the real home of these records is the draft directory.
    """

    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()[:8]
    stamp = utc_now().replace("-", "").replace(":", "")
    return f"RUN-{stamp}-{digest}"
