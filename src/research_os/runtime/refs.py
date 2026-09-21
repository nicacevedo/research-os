"""Git ref namespaces this system reserves, and who is allowed to write them.

One module because two components have to agree and they are far apart:
:func:`research_os.runtime.actions.coding.canonical_fingerprint`, which decides
whether a coding run escaped its worktree, and
:mod:`research_os.portfolio.curator`, which commits the autonomous idea bank
into the project's own repository.

**The collision this exists to prevent.** ``canonical_fingerprint`` hashes
every Git ref in the project repository before a coding run and again after it,
and reports any ref that moved as *"the coding run changed the canonical
checkout ... Something executed during the acceptance commands reached outside
the worktree"*. The window spans the whole pipeline, and the repository lock is
deliberately released before the builder runs, so anything else that commits to
that repository in that window fails somebody else's coding run.

The Curator commits to ``refs/heads/research-os/autonomous`` in exactly that
repository, on its own schedule. Without a reservation, running the portfolio
and a coding cycle on one project at the same time makes the coding cycle
report an escape that did not happen -- which is the same failure the
fingerprint's own docstring records having had once before, when it flagged the
pipeline's own worktree branch.

**What the reservation costs, stated rather than hidden.** A reserved ref is
recorded under a marker whose *value* is a constant, so:

- **creating** one is a new key and is still an escape;
- **deleting** one removes a key and is still an escape;
- **moving** one is invisible, and that is the whole exemption.

Movement is what the Curator does, and movement is what the Curator guards: it
records the commit it last wrote and refuses to commit onto a tip it does not
recognise, naming the unexpected sha. The first version of this dropped
reserved refs from the fingerprint entirely, which made creation and deletion
invisible too -- in *every* repository the coding pipeline touches, including
the ones that have no portfolio and therefore no Curator to notice. An
independent security review found that the cost was being carried by a
component that may not exist.

So: the fingerprint guards the refs nothing in this system writes; it still
notices a reserved ref appearing or vanishing anywhere; and the one namespace
this system moves guards itself.

The alternative -- a bare repository under the state home -- was rejected
because the whole value of the bank being Git is that the researcher can run
``git log research-os/autonomous`` in their own checkout.
"""

from __future__ import annotations

#: The branch the Curator materialises the autonomous idea bank onto, per
#: project. Never the researcher's canonical branch, never merged by this
#: system, and never checked out in the researcher's own working tree.
AUTONOMOUS_BANK_BRANCH = "research-os/autonomous"

#: Ref prefixes written by this system's own serialized writers, whose
#: *movement* the coding pipeline's escape fingerprint ignores. Deliberately
#: short: every entry is a namespace somebody has to be shown to guard by
#: another mechanism.
#:
#: The marker the fingerprint records them under. Present so that a reserved
#: ref appearing or disappearing is still a change, while moving one is not.
RESERVED_REF_PREFIXES: tuple[str, ...] = (f"refs/heads/{AUTONOMOUS_BANK_BRANCH}",)

#: What a reserved ref's value is recorded as. A constant, so a move is
#: invisible and an appearance or a disappearance is not.
RESERVED_REF_VALUE = "reserved"


def is_reserved_ref(ref: str) -> bool:
    """Whether ``ref`` belongs to a namespace this system reserves.

    Anchored: the ref must *be* a reserved prefix or live under it as a path
    component. A bare ``startswith`` also matched
    ``refs/heads/research-os/autonomous-anything``, which an acceptance command
    could create -- so the one namespace the escape check ignores would have
    been a whole family of names the Curator never looks at. An independent
    security review found it.
    """

    return any(
        ref == prefix or ref.startswith(prefix + "/")
        for prefix in RESERVED_REF_PREFIXES
    )
