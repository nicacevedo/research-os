"""Noticing that a person changed the science, from the runtime's side only.

The problem this solves is narrow and worth stating precisely. Human scientific
*authority* is intentional and stays: only a person may promote a proposal into
a capsule, accept a Claim, or change a preregistration. Human *choreography* is
not intentional and is the thing this release removes. Before this module, the
sequence was:

```text
cycle -> findings -> proposal -> "a decision is waiting for you"
   ...researcher runs `researchctl propose promote` ...
-> nothing happens, because nothing told the runtime
-> researcher types a continue command
```

That last step is routine human orchestration, and the objective sat parked
until it happened.

**The direction of the dependency is the design.** The obvious fix is for the
promotion command to notify the runtime -- and that would make the scientific
kernel depend on PostgreSQL and on a daemon being up, which
``DESIGN_INVARIANTS.md`` and ``docs/RUNTIME.md`` §2 both forbid, and which would
mean a researcher could not promote anything on a machine where the database
was down. So the kernel is not changed at all. The runtime *observes*: it hashes
what the capsule currently says, compares that with what it last saw, and emits
exactly one operational event when the two differ.

**What the digest covers, and why it is not the frontier digest.** Two digests
are computed, and they answer different questions.

``capsule_digest`` is over every object's id, status and project-scoped semantic
digest, plus the charter and state documents. It answers "did the canonical
science change at all" -- including changes the frontier cannot see, such as a
hypothesis's statement being sharpened or the charter being rewritten. That is
the *event*.

``frontier_digest`` is the existing one from
:func:`research_os.runtime.graphs.cycle.frontier_digest`, over the unresolved
work. It answers "is there different work to do" -- which is the *eligibility*
question, and the one that decides whether a successor cycle would learn
anything. The seven-cycle pilot in ``docs/RUNTIME.md`` §16 is what made that
distinction load-bearing: a successor opened over an identical frontier costs
model calls and returns the same answer.

So a capsule change is always recorded, and a successor cycle is opened only
when the work actually moved.
"""

from __future__ import annotations

import hashlib
import json
import logging

from research_os.errors import ResearchOSError
from research_os.runtime.kernel import ScientificKernelAdapter

LOG = logging.getLogger("research_os.runtime.capsulewatch")

#: What a project whose capsule cannot be read hashes to.
#:
#: A sentinel rather than an exception, because a capsule that is mid-edit --
#: a half-written YAML file, a `git checkout` in progress -- is an ordinary
#: transient state on a researcher's machine, and a control plane that failed a
#: pass over it would stop observing every other project too. The sentinel
#: differs from every real digest, so recovery is noticed as a change and the
#: next pass reads the repaired capsule. It carries the reason so a persistent
#: one is diagnosable rather than mysterious.
UNREADABLE_PREFIX = "unreadable:"


def capsule_digest(
    kernel: ScientificKernelAdapter,
    report: object | None = None,
) -> str:
    """Hash everything canonical about one project's science.

    Over ``(id, status, subject_digest)`` for every object, plus the charter and
    state documents, plus the project identity. Sorted, so the answer does not
    depend on directory order.

    **Status is included and the kernel's own digest excludes it**, and both are
    right. ``semantic_projection`` leaves ``status`` out so that a Review does
    not go stale when an object is moved administratively -- the science it
    reviewed is unchanged. Here the question is "did anything canonical change",
    and a Question moving from ``open`` to ``answered`` is the single most
    important thing a researcher does that this must not miss.
    """

    try:
        report = kernel.validate() if report is None else report
        if report.project is None:
            return f"{UNREADABLE_PREFIX}no project identity"
        project_id = str(report.project.id)
        objects = [
            [
                str(obj.id),
                str(getattr(obj, "status", "")),
                kernel_digest(kernel, project_id=project_id, obj=obj),
            ]
            for obj in report.objects
        ]
    except ResearchOSError as exc:
        LOG.debug("capsule unreadable at %s: %s", kernel.repo_path, exc)
        return f"{UNREADABLE_PREFIX}{exc}"

    payload = json.dumps(
        {
            "v": 1,
            "project": project_id,
            "objects": sorted(objects),
            # The two narrative documents. Not scientific objects, but canonical
            # Git-tracked scientific state: the charter is what the project is
            # for, and a proposal grounded in it should notice when it changes.
            "charter": _document_digest(kernel, "CHARTER.md"),
            "state": _document_digest(kernel, "STATE.md"),
        },
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def kernel_digest(
    kernel: ScientificKernelAdapter, *, project_id: str, obj: object
) -> str:
    """One object's contribution to the capsule digest.

    For a reviewable object this is the kernel's own project-scoped
    ``subject_digest`` -- delegated rather than reimplemented, because a second
    implementation of a scientific digest would eventually disagree with the
    kernel's, and the one that disagreed quietly would be the one that failed to
    notice a change.

    **A Review is not reviewable, and that blind spot was a real defect.**
    ``Reviewable`` is every scientific object *except* ``Review``, so
    ``subject_digest`` raises for one — and returning a constant marker for it
    meant a Review contributed only its id and status. An adversarial review
    executed the consequence: a researcher changing a Review's verdict from
    ``revise`` to ``approve`` — the highest-authority act in the system — moved
    ``claims_with_stale_review`` and the frontier digest, and left the capsule
    digest byte-identical. The watcher saw nothing, emitted nothing, and stored
    the new frontier while reporting no change. Permanently.

    So a Review is hashed from the fields that decide an acceptance:
    ``claim_approval`` reads ``reviewer_kind``, ``verdict``, ``subject``,
    ``subject_digest`` and the evidence and experiment digests, and all of them
    are here. Anything else unreviewable keeps the marker, which is honest: its
    identity and status are still in the hash, so adding one is visible.
    """

    from research_os.digests import subject_digest
    from research_os.models import Review

    del kernel
    if isinstance(obj, Review):
        return json.dumps(
            {
                "reviewer_kind": str(obj.reviewer_kind),
                "verdict": str(obj.verdict),
                "subject": str(obj.subject),
                "subject_digest": str(obj.subject_digest),
                "evidence_digests": dict(sorted((obj.evidence_digests or {}).items())),
                "experiment_digests": dict(
                    sorted((obj.experiment_digests or {}).items())
                ),
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    try:
        return subject_digest(obj, project_id=project_id)  # type: ignore[arg-type]
    except (TypeError, ValueError) as exc:
        LOG.debug("object %r is not reviewable: %s", getattr(obj, "id", "?"), exc)
        return "not-reviewable"


def _document_digest(kernel: ScientificKernelAdapter, name: str) -> str:
    path = kernel.repo_path / ".research" / name
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
    except OSError:
        return "unreadable"


def observed_digests(repo_path: object) -> tuple[str, str]:
    """Return ``(capsule_digest, frontier_digest)`` for one repository.

    **From one validation of the capsule.** The previous version said so and did
    not do it: ``capsule_digest(kernel)`` called ``kernel.validate()`` and
    ``kernel.frontier()`` called it again, so the two digests came from two
    independent reads of the working tree. An adversarial review pointed out
    what lands in between -- a researcher's ``git commit`` of a promotion, which
    is precisely the event this function exists to notice -- and the resulting
    row would pair a capsule digest from before the commit with a frontier from
    after it.

    Now the report is read once here and passed to both. What that does *not*
    buy is atomicity against a concurrent ``git checkout``: the report reads
    many files, and the charter and state documents are read again inside
    :func:`capsule_digest`. A torn read is still possible and its consequence is
    bounded in the safe direction -- the pair recorded is ``(capsule at A,
    frontier at B)``, the next observation reads ``(B, frontier at B)``, the
    capsule digest differs, and the change is recorded one observation late.
    Never missed, because the capsule digest of the later moment can only equal
    the earlier one if nothing canonical changed.
    """

    from research_os.runtime.graphs.cycle import frontier_digest

    kernel = ScientificKernelAdapter(repo_path)  # type: ignore[arg-type]
    try:
        report = kernel.validate()
    except ResearchOSError as exc:
        LOG.debug("capsule unreadable at %s: %s", kernel.repo_path, exc)
        return f"{UNREADABLE_PREFIX}{exc}", ""
    capsule = capsule_digest(kernel, report)
    if capsule.startswith(UNREADABLE_PREFIX):
        return capsule, ""
    try:
        return capsule, frontier_digest(kernel.frontier(report))
    except ResearchOSError as exc:
        LOG.debug("frontier unavailable at %s: %s", kernel.repo_path, exc)
        return capsule, ""
