"""Portfolio identifiers.

Same shape as the runtime's -- ``PREFIX-<UTC timestamp>-<8 hex>`` -- and for
the same reason: a person reading a log can tell what kind of thing an id names
without looking it up.

The prefix that matters most is ``PIDEA``. The capsule already has an object
type called Idea whose ids look like ``IDEA-0001``, and
:mod:`research_os.runtime.ids` states the rule this obeys: *a runtime id and a
scientific id must never be mistakable for one another, because the entire
authority model rests on them being different kinds of thing.* ``IDEA-0001`` is
science the researcher owns. ``PIDEA-20260920T181500Z-3fa17b0c`` is a candidate
direction with no scientific status at all.
"""

from __future__ import annotations

import re
from datetime import datetime

from research_os.runtime.ids import new_id

IDEA_ID_RE = re.compile(r"^PIDEA-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
IDEA_ACTION_ID_RE = re.compile(r"^IACT-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
IDEA_REVIEW_ID_RE = re.compile(r"^IREV-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
IDEA_EVIDENCE_ID_RE = re.compile(r"^IEVD-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
IDEA_EXPERIMENT_ID_RE = re.compile(r"^PEXP-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
OBJECTION_ID_RE = re.compile(r"^IOBJ-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
PORTFOLIO_DIGEST_ID_RE = re.compile(r"^PDIG-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SEED_ID_RE = re.compile(r"^SEED-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
CONTRACT_ID_RE = re.compile(r"^PCON-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
REQUEST_ID_RE = re.compile(r"^PFRQ-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
PROVENANCE_ID_RE = re.compile(r"^IPRV-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
CLAIM_ID_RE = re.compile(r"^PLCL-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")
SYNTHESIS_ID_RE = re.compile(r"^PSYN-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{8}$")

#: Every id pattern this package mints, so a test can assert none of them can
#: match a capsule id and none can match another runtime id.
ID_PATTERNS: dict[str, re.Pattern[str]] = {
    "idea": IDEA_ID_RE,
    "idea_action": IDEA_ACTION_ID_RE,
    "idea_review": IDEA_REVIEW_ID_RE,
    "idea_evidence": IDEA_EVIDENCE_ID_RE,
    "idea_experiment": IDEA_EXPERIMENT_ID_RE,
    "objection": OBJECTION_ID_RE,
    "portfolio_digest": PORTFOLIO_DIGEST_ID_RE,
    "seed": SEED_ID_RE,
    "contract": CONTRACT_ID_RE,
    "frontier_request": REQUEST_ID_RE,
    "idea_provenance": PROVENANCE_ID_RE,
    "literature_claim": CLAIM_ID_RE,
    "synthesis": SYNTHESIS_ID_RE,
}


def new_idea_id(*, moment: datetime | None = None) -> str:
    return new_id("PIDEA", moment=moment)


def new_idea_action_id(*, moment: datetime | None = None) -> str:
    return new_id("IACT", moment=moment)


def new_idea_review_id(*, moment: datetime | None = None) -> str:
    return new_id("IREV", moment=moment)


def new_idea_evidence_id(*, moment: datetime | None = None) -> str:
    return new_id("IEVD", moment=moment)


def new_idea_experiment_id(*, moment: datetime | None = None) -> str:
    """A portfolio experiment id.

    ``PEXP`` and not ``XRUN``: the v1 experiment layer already mints
    ``XRUN-...`` for one *execution* it owns end to end, and this names the
    portfolio's record of *which idea version asked*. Two ids that looked
    alike would invite exactly the association-by-coincidence that
    ``experiment_interpretations`` exists to prevent.
    """

    return new_id("PEXP", moment=moment)


def new_objection_id(*, moment: datetime | None = None) -> str:
    return new_id("IOBJ", moment=moment)


def new_portfolio_digest_id(*, moment: datetime | None = None) -> str:
    return new_id("PDIG", moment=moment)


def new_seed_id(*, moment: datetime | None = None) -> str:
    return new_id("SEED", moment=moment)


def new_contract_id(*, moment: datetime | None = None) -> str:
    """A scientific contract id.

    ``PCON``: the portfolio's frozen hypothesis + analysis + design. Not a
    capsule object -- a contract is operational state about a candidate idea,
    and it never reaches ``.research/`` -- which is why it is not shaped like
    ``EXP-0001``.
    """

    return new_id("PCON", moment=moment)


def new_request_id(*, moment: datetime | None = None) -> str:
    return new_id("PFRQ", moment=moment)


def new_synthesis_id(*, moment: datetime | None = None) -> str:
    return new_id("PSYN", moment=moment)


def new_claim_id(*, moment: datetime | None = None) -> str:
    return new_id("PLCL", moment=moment)


def new_provenance_id(*, moment: datetime | None = None) -> str:
    return new_id("IPRV", moment=moment)


def track_thread_id(run_id: str) -> str:
    """The LangGraph thread for one entry into an idea track.

    Derived from the *run*, for two reasons. One thread per stage *attempt*
    rather than per stage: a stage that failed on a provider outage and is
    tried again is a second attempt, and keying the thread on
    ``(idea, version, stage)`` made the second one collide with the first on
    ``research_runs.thread_id``'s unique index. And a thread keyed on the run
    is prunable by the existing retention, which deletes threads whose runs
    finished long enough ago.

    Named distinctly from :func:`research_os.runtime.ids.thread_id_for`, whose
    threads are cycle-graph threads. A resumer that fed one to the other graph
    would deserialise a state schema it does not have.
    """

    return f"idea-track:{run_id}"
