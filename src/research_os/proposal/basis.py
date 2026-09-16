"""What a proposal rested on, and whether that is still true.

A proposal is asynchronous: it is created by one bounded run and promoted by a
person, possibly much later, and in between the project's canonical science can
move. Promoting onto a basis that no longer holds is the failure this module
exists to prevent, and it is a quiet one -- the promotion succeeds, the draft is
written, and nothing says that the Claim the reasoning depended on was retired
last week.

**Why not the repository HEAD.** ``base_commit`` is already on the proposal and
comparing it to the current HEAD was the obvious freshness check and the wrong
one: a proposal is about scientific objects, and failing it because somebody
fixed a typo in the README would teach a researcher to click past the warning.
So this snapshots the objects the proposal actually cited, and unrelated
repository changes are not staleness.

**Separate from ``promote.py`` on purpose.** That module is the one door between
proposed work and a project's scientific record, and "nothing imports it except
the CLI command a person runs" is a property worth keeping checkable. The
proposal controller needs to *compute* a basis when it creates a proposal, and
having it import the module that holds ``write_promotion`` in order to do so
would blur exactly the boundary that matters.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from research_os.capsule import validate_project
from research_os.digests import subject_digest
from research_os.proposal.models import ResearchProposal, ScientificBasisSnapshot


def _digest_of(pairs: tuple[tuple[str, str], ...]) -> str:
    material = "\n".join(f"{name}={value}" for name, value in pairs)
    return hashlib.sha256(
        ("proposal-basis-v1\n" + material).encode("utf-8")
    ).hexdigest()


def _charter_digest(root: Path) -> str | None:
    charter = root / ".research" / "CHARTER.md"
    if not charter.is_file():
        return None
    try:
        return hashlib.sha256(charter.read_bytes()).hexdigest()
    except OSError:
        return None


def _object_digest(obj: object, project_id: str) -> str:
    """One cited object's semantic digest, or a marker if it has none.

    Guarded, because `Reviewable` is every scientific object *except* ``Review``
    and a proposal can legitimately cite a Review: ``build_science_context``
    includes reviews in the citable set, so a worker that wrote
    ``addresses: ["REV-0001"]`` produced a *valid* proposal whose basis snapshot
    then raised an uncaught ``TypeError`` — through `ProposalController._parse`,
    past every handler in `propose_capsule_change`, leaving the reservation
    `RESERVED` and the action dead on exactly the projects where a human has
    recorded a Review. Found by an adversarial review.

    `research_os.runtime.capsulewatch` guards the same call for the same reason.
    """

    from research_os.models import Review

    if isinstance(obj, Review):
        # A Review has no subject digest of its own; what identifies it for
        # staleness is the verdict and what it was a verdict *about*.
        return (
            f"review|{obj.reviewer_kind}|{obj.verdict}|{obj.subject}|"
            f"{obj.subject_digest}"
        )
    try:
        return subject_digest(obj, project_id=project_id)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return "not-reviewable"


def referenced_object_ids(
    proposal: ResearchProposal, *, root: Path | None = None
) -> tuple[str, ...]:
    """What this proposal rests on: the objects it cites, and what *they* rest on.

    The *cited* set rather than the offered one. ``grounding.capsule_ids`` is
    everything the worker was shown -- usually every object in the project --
    and snapshotting that would make any change anywhere in the capsule
    invalidate every waiting proposal, which is the over-broad check this whole
    mechanism exists to replace.

    **Plus one hop, which an adversarial review showed is not optional.** A
    proposal citing ``HYP-0001`` rests on the Evidence that hypothesis rests on
    and on any Review that has judged it, and neither is in ``addresses``.
    Executed: retracting the cited hypothesis's supporting Evidence -- status to
    ``withdrawn``, statement replaced with "RETRACTED: the load cell was
    miscalibrated" -- and adding a human Review with ``verdict: reject`` left
    the basis reporting ``fresh``, and the promotion proceeded. Neither change
    touches the cited object's own semantic digest: ``semantic_projection`` puts
    only evidence *ids* in a Hypothesis's projection, and a Review is a separate
    object the subject does not point at.

    So one hop: each cited object's ``supporting_evidence``,
    ``contrary_evidence``, ``assumptions`` and ``hypotheses``, plus every Review
    whose ``subject`` is a cited object. One hop and not transitive closure,
    because the closure of a mature capsule is the capsule, and that is the
    over-broad check again.

    ``root`` is the project to read the graph from. Without it only the directly
    cited ids are returned, which is what a caller that has no project to read
    can honestly say.
    """

    cited: set[str] = set()
    for item in proposal.items:
        cited.update(item.addresses)
    if root is None or not cited:
        return tuple(sorted(cited))

    report = validate_project(root)
    by_id = {str(obj.id): obj for obj in report.objects}
    neighbours: set[str] = set()
    for object_id in cited:
        obj = by_id.get(object_id)
        if obj is None:
            continue
        for attribute in (
            "supporting_evidence",
            "contrary_evidence",
            "assumptions",
            "hypotheses",
        ):
            for value in getattr(obj, attribute, None) or ():
                neighbours.add(str(value))
    for obj in report.objects:
        subject = getattr(obj, "subject", None)
        if subject is not None and str(subject) in cited:
            # A Review of a cited object. Adding one, removing one, or changing
            # its verdict is the most consequential change that can happen to
            # what a proposal rests on.
            neighbours.add(str(obj.id))
    return tuple(sorted(cited | (neighbours & set(by_id))))


def scientific_basis(
    proposal_items_cite: tuple[str, ...],
    *,
    root: Path,
    project_id: str | None,
    literature_keys: tuple[str, ...] = (),
    finding_packet_digest: str | None = None,
    include_charter: bool = False,
) -> ScientificBasisSnapshot:
    """Snapshot the scientific state a proposal is about to rest on.

    Each referenced object contributes its **status** and its project-scoped
    ``subject_digest`` -- the kernel's own semantic digest, not a second
    implementation of it, so "did the science this proposal relied on change" is
    answered by the same hash the kernel uses to decide whether a Review has
    gone stale.

    **Status is included deliberately, and the kernel excludes it deliberately.**
    ``semantic_projection`` leaves ``status`` out because a Review's subject
    must not go stale when an object is moved administratively -- the science it
    reviewed is unchanged. A *proposal's* premise is the opposite: "this project
    has an open question about X" stops being true the moment that Question is
    answered, and the statement of the Question does not move when it is. So the
    two digests answer different questions and both are right.

    An object that is referenced and *absent* contributes the sentinel
    ``"<missing>"`` rather than being skipped. Skipping it would let a proposal
    survive the deletion of the very Claim it was about.
    """

    report = validate_project(root)
    by_id = {str(obj.id): obj for obj in report.objects}
    resolved_project = (
        project_id
        if project_id is not None
        else (str(report.project.id) if report.project is not None else None)
    )
    pairs: list[tuple[str, str]] = []
    versions: dict[str, int] = {}
    statuses: dict[str, str] = {}
    for object_id in proposal_items_cite:
        obj = by_id.get(object_id)
        if obj is None or resolved_project is None:
            pairs.append((object_id, "<missing>"))
            statuses[object_id] = "<missing>"
            continue
        status = str(getattr(obj, "status", ""))
        pairs.append((object_id, f"{status}|{_object_digest(obj, resolved_project)}"))
        statuses[object_id] = status
        versions[object_id] = int(getattr(obj, "schema_version", 1))
    return ScientificBasisSnapshot(
        project_id=resolved_project,
        capsule_schema_versions=versions,
        referenced_object_statuses=statuses,
        referenced_object_ids=list(proposal_items_cite),
        referenced_object_digest=_digest_of(tuple(pairs)),
        charter_digest=_charter_digest(root) if include_charter else None,
        finding_packet_digest=finding_packet_digest,
        literature_keys=sorted(literature_keys),
    )


@dataclass(frozen=True, slots=True)
class BasisStatus:
    """Whether a proposal's scientific basis still holds.

    ``checkable=False`` is a third answer and not a synonym for ``fresh``. A
    proposal written before basis snapshots existed records none, and reporting
    that as fresh would be asserting a check that never ran.
    """

    checkable: bool
    fresh: bool
    reason: str = ""
    changed_objects: tuple[str, ...] = ()
    unchecked: tuple[str, ...] = ()
    """Parts of the basis this caller could not verify. Reported, never blocking.

    A fourth answer, and it has to be distinct from the other three. "The
    objects are unchanged and the runtime findings were not re-checked" is not
    staleness -- blocking on it would refuse every runtime proposal forever,
    because recomputing a finding digest needs the operational database and the
    scientific layers deliberately do not depend on it. Nor is it freshness
    with nothing to say. So it is freshness *with a caveat*, and the caveat is
    printed where a person will read it before deciding.
    """

    @property
    def stale(self) -> bool:
        return self.checkable and not self.fresh


def basis_status(
    proposal: ResearchProposal,
    *,
    project_path: Path | None = None,
    finding_packet_digest: str | None = None,
) -> BasisStatus:
    """Recompute the proposal's basis and say whether it still holds.

    **What this deliberately does not check.** The repository ``HEAD``. It is on
    the proposal as ``base_commit`` and comparing it was the obvious freshness
    test and the wrong one: a proposal is about scientific objects, and failing
    it because someone edited a README would teach a researcher to click past
    the warning. Unrelated repository changes are not staleness.

    **What it does check.** The project identity, the referenced objects'
    project-scoped semantic digests, their schema versions, the charter when the
    snapshot recorded one, and -- when the caller can recompute it -- the digest
    of the findings the proposal was grounded in. Any material change fails
    closed: the caller must regenerate and reassess rather than promote onto a
    basis that no longer holds.
    """

    snapshot = proposal.scientific_basis
    if snapshot is None:
        return BasisStatus(
            checkable=False,
            fresh=False,
            reason=(
                "this proposal records no scientific basis, so whether the "
                "science it rested on has changed cannot be established. It was "
                "made before basis snapshots existed; regenerate it if the "
                "project has moved on since."
            ),
        )
    if not snapshot.referenced_object_ids and snapshot.charter_digest is None:
        # Nothing was cited, so nothing's change could be detected, so `fresh`
        # would be asserting a check that did not happen. Reachable and not
        # hypothetical: `addresses` is optional for a question, a hypothesis and
        # an experiment, so a whole proposal can legitimately cite nothing --
        # and an adversarial review executed it, answering a Question, retiring
        # a Hypothesis and withdrawing an Evidence while the basis reported
        # `fresh` and the promotion proceeded. The only surviving check was the
        # project id.
        return BasisStatus(
            checkable=False,
            fresh=False,
            reason=(
                "this proposal cites no scientific object and was not grounded "
                "in the charter, so there is nothing whose change could be "
                "detected. Read it against the project's current state yourself."
            ),
        )

    root = (
        Path(project_path) if project_path is not None else Path(proposal.project_path)
    )
    current = scientific_basis(
        tuple(snapshot.referenced_object_ids),
        root=root,
        project_id=snapshot.project_id,
        literature_keys=tuple(snapshot.literature_keys),
        finding_packet_digest=(
            finding_packet_digest
            if finding_packet_digest is not None
            else snapshot.finding_packet_digest
        ),
        include_charter=snapshot.charter_digest is not None,
    )

    if current.project_id != snapshot.project_id:
        return BasisStatus(
            checkable=True,
            fresh=False,
            reason=(
                f"this proposal was made for project {snapshot.project_id!r} and "
                f"{root} now identifies as {current.project_id!r}"
            ),
        )
    if current.referenced_object_digest != snapshot.referenced_object_digest:
        changed = _changed_objects(snapshot, root)
        return BasisStatus(
            checkable=True,
            fresh=False,
            reason=(
                "the scientific objects this proposal relied on have changed "
                "since it was written, so its reasoning rests on state this "
                "project no longer holds: " + (", ".join(changed) or "unknown")
            ),
            changed_objects=changed,
        )
    if current.capsule_schema_versions != snapshot.capsule_schema_versions:
        return BasisStatus(
            checkable=True,
            fresh=False,
            reason=(
                "a referenced object's schema version changed, so the objects "
                "this proposal reasoned about are not the same kind of thing "
                "they were"
            ),
        )
    if snapshot.charter_digest is not None and (
        current.charter_digest != snapshot.charter_digest
    ):
        return BasisStatus(
            checkable=True,
            fresh=False,
            reason=(
                "the project charter changed after this proposal was written, "
                "and the proposal was grounded in it"
            ),
        )
    unchecked: tuple[str, ...] = ()
    if snapshot.finding_packet_digest is not None:
        if finding_packet_digest is None:
            # The one part of a *runtime* proposal's basis that a human
            # promotion never checks, and it used to report "the basis is
            # unchanged" anyway. An adversarial review found that.
            #
            # Reported rather than blocking. `_promote` cannot recompute the
            # digest -- that needs the operational database, which the
            # scientific layers deliberately do not depend on -- so refusing on
            # it would refuse every runtime proposal forever. Saying so is the
            # honest answer; saying nothing was the defect.
            unchecked = ("the runtime findings it cites were not re-checked",)
        elif finding_packet_digest != snapshot.finding_packet_digest:
            return BasisStatus(
                checkable=True,
                fresh=False,
                reason=(
                    "the runtime findings this proposal was grounded in have "
                    "been superseded, so what it cites no longer says what it "
                    "said"
                ),
            )
    return BasisStatus(
        checkable=True,
        fresh=True,
        reason="the basis is unchanged",
        unchecked=unchecked,
    )


def _changed_objects(snapshot: ScientificBasisSnapshot, root: Path) -> tuple[str, ...]:
    """Which referenced objects moved, for a message a person can act on.

    "Something changed" is not a report anybody can act on. Two of the three
    kinds of change can be named exactly, because the snapshot records them per
    object: an object that is now absent, and one whose status moved. The third
    -- a change to an object's content -- cannot be localised, because the
    snapshot keeps one combined digest rather than one per object, which is what
    keeps it small. In that case the referenced set is named as the set the
    change lies within.
    """

    report = validate_project(root)
    by_id = {str(obj.id): obj for obj in report.objects}
    changed: list[str] = []
    for object_id in snapshot.referenced_object_ids:
        obj = by_id.get(object_id)
        if obj is None:
            changed.append(f"{object_id} (absent)")
            continue
        was = snapshot.referenced_object_statuses.get(object_id)
        now = str(getattr(obj, "status", ""))
        if was is not None and was != now:
            changed.append(f"{object_id} ({was} -> {now})")
    if changed:
        return tuple(changed)
    # The per-object semantic digests are not stored -- the snapshot keeps one
    # combined hash, which is what keeps it small -- so when nothing is absent
    # and no status moved, the change is in an object's *content* and this can
    # only name the set it lies within.
    return tuple(
        f"{object_id} (content)" for object_id in snapshot.referenced_object_ids
    )
