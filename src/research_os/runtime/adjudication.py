"""How an unresolved scientific target could actually be settled.

**The gap this closes.** ``planner@5`` stopped the runtime freezing a seventh
specification for a hypothesis that already had six. It did not stop the
*first* one being the wrong kind of work. HYP-0002 of the thesis pilot says

    the pricing subproblem has a finite infimum if and only if
    ``||X' psi||_inf <= lambda_1``

and its own falsification clause asks for a counterexample. No measurement
decides that. The runtime nonetheless designed six experiments for it, because
the only thing it knew about an unresolved hypothesis was that it was
unresolved, and the only verb it had for an unresolved hypothesis was
``design_experiment``.

So::

    unresolved hypothesis  !=  empirical experiment required

**Where the classification comes from, and why that matters.** Not from a
model. From the project's own ``falsification`` clause, weighted above its
``statement``, because the falsifier is the sentence in which the researcher
already wrote down what would settle the thing. A hypothesis that says "Prove
that ... for every dual point" has *stated* that it is adjudicated by proof;
reading that is not inference, it is reading.

Deterministic, therefore: the same object classifies the same way on every
host, in every cycle, forever, and the verdict carries the literal signals it
matched so a person can check the reasoning in one line rather than trusting
it.

**What it must never do.** Nothing here writes, and nothing here is scientific
truth. An :class:`AdjudicationKind` is *planning* metadata about a capsule
object; it is not stored in the capsule, it participates in no scientific
digest, it has no status a person could accept, and it cannot retire, support
or refute anything. It decides which *verb* the runtime reaches for. If it is
wrong, the cost is a cycle spent on the wrong kind of work -- the same cost the
system already pays -- and never a false scientific statement.

That is deliberately the smallest design that solves the routing problem.
Adding an ``adjudication:`` field to ``Hypothesis`` was the alternative, and it
would have meant a canonical schema change, a migration of every capsule on
disk, and a new way for an automated system to write a scientific-sounding
label into files a person is supposed to own.

**Conservative by construction.** When the signals are absent the verdict is
:attr:`AdjudicationKind.UNDETERMINED`, and when they conflict it is
:attr:`AdjudicationKind.MIXED`. Neither blocks anything: the routing guard in
:mod:`research_os.runtime.graphs.cycle` refuses an action only on a *positive*
determination, so a target this module does not understand behaves exactly as
it did before this module existed.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum

#: How much more the falsification clause counts than the statement.
#:
#: Two, and the asymmetry is the whole method. A statement describes what is
#: believed; a falsifier describes *what would settle it*, which is the
#: question being asked here. HYP-0001 of the thesis pilot makes the point --
#: its statement is full of solver names and its falsifier is full of medians,
#: repetitions and wall time, and only the second one says how to decide it.
FALSIFIER_WEIGHT = 2

#: How far ahead the leading category must be to win outright.
#:
#: Below this the verdict is MIXED rather than the leader, because a target
#: with a mathematical core *and* a literature-priority component -- HYP-0003
#: is exactly that -- is not served by picking one and discarding the other.
DOMINANCE_RATIO = 2


class AdjudicationKind(StrEnum):
    """What kind of work could settle a scientific target.

    A closed set, and each member names a *mechanism of adjudication* rather
    than a topic. "About mathematics" is not a member; "settled by derivation"
    is.
    """

    EMPIRICAL = "empirical"
    """Settled by measurement. An experiment is the right instrument."""

    MATHEMATICAL = "mathematical"
    """Settled by derivation or counterexample.

    A numerical run can *witness* such a proposition -- exhibit the
    counterexample, or fail to find one over a swept range -- and that is
    worth having. It cannot establish it. The distinction is the one §32 of
    the mission brief insists on: a witness is not a proof.
    """

    NOVELTY_OR_LITERATURE = "novelty_or_literature"
    """Settled by primary sources. Whether something is already known is a
    question about the published record, and no amount of local computation
    answers it."""

    MIXED = "mixed"
    """Settled by an ordered combination: theory or literature first, then
    measurement of what remains open."""

    DIAGNOSTIC = "diagnostic"
    """Answerable by observing this implementation, and *only* about this
    implementation.

    Useful, and routinely mistaken for more than it is. That a particular
    program stops on a dual-stall heuristic is a fact about the program. It
    cannot establish the central scientific proposition, so a diagnostic result
    is never on its own a reason to close a target.
    """

    UNDETERMINED = "undetermined"
    """Not enough signal to say. Routes nothing and blocks nothing."""


#: Literal phrases that indicate each mechanism, matched case-insensitively.
#:
#: General scientific-methodology vocabulary, deliberately: nothing here names
#: a solver, a norm or a project. The test that keeps it honest is
#: ``tests/test_runtime_adjudication.py``, which classifies targets from an
#: unrelated domain as well as the thesis pilot's own.
#:
#: Three words are conspicuously *absent* and each absence was a correction.
#: "exhibit" opens both HYP-0002's falsifier ("exhibit a psi ...", mathematical)
#: and HYP-0001's ("exhibit one preregistered instance regime ...", empirical),
#: so it separates nothing. "KKT" appears in a purely empirical falsifier as
#: part of "a matched KKT tolerance". "test" is in every falsifier ever
#: written.
SIGNALS: Mapping[AdjudicationKind, tuple[str, ...]] = {
    AdjudicationKind.MATHEMATICAL: (
        r"\bprov(?:e|es|ed|en|ably|able)\b",
        r"\bproof\b",
        r"\bderiv(?:e|es|ed|ation|ations)\b",
        r"\btheorem\b",
        r"\blemma\b",
        r"\bcorollar(?:y|ies)\b",
        r"\bcounterexample\b",
        r"\bif and only if\b",
        r"\biff\b",
        r"\bfor every\b",
        r"\bfor all\b",
        r"\bthere exists?\b",
        r"\banalytic(?:al|ally)?\b",
        r"\binfim(?:um|a)\b",
        r"\bsuprem(?:um|a)\b",
        r"\bupper bound\b",
        r"\blower bound\b",
        r"\bis exactly\b",
        r"\breduces to\b",
        r"\bequivalent to\b",
        r"\bidentically\b",
    ),
    AdjudicationKind.EMPIRICAL: (
        r"\bmeasure(?:d|s|ment|ments)?\b",
        r"\bprofil(?:e|es|ed|ing)\b",
        r"\bbenchmark(?:s|ed|ing)?\b",
        r"\bwall(?:[ -])?times?\b",
        r"\bwall[ -]?clock\b",
        r"\brepetitions?\b",
        r"\bseeds?\b",
        r"\bmedian\b",
        r"\bpreregistered\b",
        r"\binstance regimes?\b",
        r"\bsample size\b",
        r"\bstatistical(?:ly)?\b",
        r"\bobserved rate\b",
        r"\bthroughput\b",
        r"\bspeedups?\b",
    ),
    AdjudicationKind.NOVELTY_OR_LITERATURE: (
        r"\bpublished\b",
        r"\bpublication\b",
        r"\bprior art\b",
        r"\bliterature\b",
        r"\bnovel(?:ty)?\b",
        r"\bknown\b",
        r"\balready established\b",
        r"\battributable to\b",
        r"\bcite[ds]?\b",
        r"\bcitations?\b",
        r"\bpre-\d{4}\b",
    ),
    AdjudicationKind.DIAGNOSTIC: (
        r"\bthe implemented\b",
        r"\bas implemented\b",
        r"\bimplementation\b",
        r"\bthis codebase\b",
        r"\bdefault configuration\b",
        r"\bdefault tolerance\b",
        r"\ba run that\b",
        r"\binstrument(?:ed|ation)?\b",
        r"\bstops on\b",
    ),
}

_COMPILED: Mapping[AdjudicationKind, tuple[tuple[str, re.Pattern[str]], ...]] = {
    kind: tuple((pattern, re.compile(pattern, re.IGNORECASE)) for pattern in patterns)
    for kind, patterns in SIGNALS.items()
}


@dataclass(frozen=True, slots=True)
class Adjudication:
    """One target's verdict, with the evidence for it.

    ``matched`` is what makes this auditable rather than oracular. A person who
    disagrees with a verdict can read the phrases it was built from in the same
    line that reports it, which is the difference between a classification they
    can argue with and one they have to accept.
    """

    kind: AdjudicationKind
    scores: Mapping[AdjudicationKind, int] = field(default_factory=dict)
    matched: tuple[str, ...] = ()

    @property
    def settles_by_measurement(self) -> bool:
        """Whether an experiment is a legitimate instrument for this target.

        True for everything except the two kinds an experiment structurally
        cannot decide. MIXED is included, because the empirical half of a mixed
        target is real -- the *ordering* is what the caller enforces, not this
        property.
        """

        return self.kind not in (
            AdjudicationKind.MATHEMATICAL,
            AdjudicationKind.NOVELTY_OR_LITERATURE,
        )

    def reason(self) -> str:
        """One line for a refusal message or a planner block."""

        if not self.matched:
            return f"{self.kind}: no adjudication signal in the object's own text"
        shown = ", ".join(self.matched[:6])
        return f"{self.kind}: matched {shown}"


def classify(statement: str = "", falsification: str = "") -> Adjudication:
    """Classify how a target could be settled, from its own words.

    Both arguments are the capsule object's text. Neither is a model's opinion
    about the object, and that is what makes the result reproducible.
    """

    scores: dict[AdjudicationKind, int] = {}
    matched: list[str] = []
    for kind, patterns in _COMPILED.items():
        total = 0
        for _pattern, compiled in patterns:
            hits = 0
            # The matched *text*, not the pattern that matched it. A person
            # auditing a verdict wants to read "prove, for every", which are
            # words in their own hypothesis; a regex alternation tells them
            # nothing they can check.
            in_statement = compiled.search(statement) if statement else None
            in_falsifier = compiled.search(falsification) if falsification else None
            if in_statement is not None:
                hits += 1
            if in_falsifier is not None:
                hits += FALSIFIER_WEIGHT
            if hits:
                total += hits
                found = in_falsifier or in_statement
                if found is not None:
                    word = found.group(0).strip().lower()
                    if word not in matched:
                        matched.append(word)
        if total:
            scores[kind] = total

    if not scores:
        return Adjudication(kind=AdjudicationKind.UNDETERMINED, scores={}, matched=())

    ranked = sorted(scores.items(), key=lambda item: (-item[1], str(item[0])))
    leader, lead_score = ranked[0]
    if len(ranked) == 1:
        return Adjudication(kind=leader, scores=scores, matched=tuple(matched))
    runner_up = ranked[1][1]
    kind = (
        leader if lead_score >= DOMINANCE_RATIO * runner_up else AdjudicationKind.MIXED
    )
    return Adjudication(kind=kind, scores=scores, matched=tuple(matched))


def classify_object(obj: object) -> Adjudication:
    """Classify a capsule object, reading only text it already carries.

    ``getattr`` rather than an isinstance ladder because every scientific
    object that can sit unresolved on the frontier -- Hypothesis, Question,
    Claim -- carries some of ``statement``, ``falsification`` and ``title``,
    and none of them carries a field this needs and the others lack.
    """

    statement = str(getattr(obj, "statement", "") or getattr(obj, "title", "") or "")
    falsification = str(getattr(obj, "falsification", "") or "")
    return classify(statement=statement, falsification=falsification)


def adjudication_view(object_id: str, verdict: Adjudication) -> dict[str, object]:
    """One verdict as plain data for a fenced planner block.

    ``noncanonical`` is stated in the entry, next to the text a skimming
    planner will read, for the same reason
    :func:`research_os.runtime.sciencecontext.finding_view` states it.
    """

    return {
        "object_id": object_id,
        "noncanonical": True,
        "settled_by": str(verdict.kind),
        "experiment_can_decide_it": verdict.settles_by_measurement,
        "basis": verdict.reason(),
    }


def unresolved_adjudications(
    kernel: object, object_ids: Sequence[str]
) -> tuple[tuple[str, Adjudication], ...]:
    """Classify each target the frontier reports unresolved.

    **One capsule validation, not one per target.** The obvious implementation
    calls ``kernel.object(object_id)`` in the loop, and that is what the first
    version did -- which is a full parse-and-validate of every file in the
    capsule *per identifier*. On a frontier of twenty unresolved objects it
    turned one planning call into twenty capsule validations, and the test
    suite's wall time roughly tripled before the cause was found.
    ``by_id()`` validates once and returns the map.

    An unreadable capsule, or an id it does not hold, yields UNDETERMINED
    rather than raising: this is planning context, and losing one entry of it
    must never fail a cycle -- the same rule
    :func:`research_os.runtime.sciencecontext._hypothesis_of` follows, and for
    the same reason. UNDETERMINED blocks nothing, so degrading this way is safe
    in the direction that matters.
    """

    try:
        by_id = kernel.by_id()  # type: ignore[attr-defined]
    except Exception:  # noqa: BLE001 - planning context, never a cycle failure
        by_id = {}
    return tuple(
        (
            object_id,
            classify_object(by_id[object_id])
            if object_id in by_id
            else Adjudication(kind=AdjudicationKind.UNDETERMINED),
        )
        for object_id in object_ids
    )
