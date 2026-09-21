"""The discovery portfolio cannot manufacture scientific acceptance either.

The runtime has a test file of this shape already, and this is its twin for the
layer above. The argument for structural assertions is the same: a behavioural
test covers the path it calls, and the property being protected is *there is no
such path anywhere*.

What is new here is the second question. The runtime's version asks whether the
runtime can write science. This one also asks whether the portfolio, which
produces a parallel vocabulary of its own -- ``idea_reviews``, "VALIDATED", a
bank committed to the researcher's own repository -- can be *read* as science
by somebody who never opens the architecture document. Several of the tests
below are about the words that reach a page rather than about the code that
reaches a file.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "research_os"
PORTFOLIO_DIR = SRC / "portfolio"


def _sources() -> list[Path]:
    found = sorted(PORTFOLIO_DIR.rglob("*.py"))
    assert found, "no portfolio sources found; the path in this test is wrong"
    return found


def _imports(path: Path) -> list[tuple[int, str, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.extend((node.lineno, node.module, alias.name) for alias in node.names)
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name, alias.name) for alias in node.names)
    return found


def _calls(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            target = node.func
            if isinstance(target, ast.Name):
                names.add(target.id)
            elif isinstance(target, ast.Attribute):
                names.add(target.attr)
    return names


# ----------------------------------------------------------- structural --
def test_no_portfolio_module_writes_a_human_review() -> None:
    """``AGENTS.md`` binds agents; this makes the code unable to break it.

    The portfolio has its own ``idea_reviews`` table and its own reviewer
    roles, which makes the confusion it must not cause more available than it
    was for the runtime: a layer that already writes something called a review
    is one import away from writing the kernel's.
    """

    forbidden = {"write_review", "build_review"}
    offenders = [
        f"{path.name}:{lineno} imports {name}"
        for path in _sources()
        for lineno, module, name in _imports(path)
        if module == "research_os.review" and name in forbidden
    ]
    assert offenders == [], "; ".join(offenders)
    called = {name for path in _sources() for name in _calls(path)}
    assert not (called & forbidden), sorted(called & forbidden)


def test_no_portfolio_module_promotes_anything() -> None:
    """Promotion is what makes a candidate scientific, and it is a human act.

    Both promotion paths, because the portfolio has a reason to reach for
    each: a HUMAN_READY idea wants to become a capsule object, and a finding
    that transfers wants to become an insight.
    """

    offenders = [
        f"{path.name}:{lineno} imports {module}.{name}"
        for path in _sources()
        for lineno, module, name in _imports(path)
        if module
        in {
            "research_os.proposal.promote",
            "research_os.insights.promote",
        }
        or name in {"promote_proposal", "promote_insight"}
    ]
    assert offenders == [], "; ".join(offenders)
    # Call names as well as imports, matching the human-Review test above. The
    # asymmetry ran the wrong way: `import research_os.proposal as p` followed
    # by `p.promote.promote_proposal(...)` passed the import check, and this is
    # the more consequential of the two prohibitions.
    called = {name for path in _sources() for name in _calls(path)}
    forbidden = {"promote_proposal", "promote_insight", "promote"}
    assert not (called & forbidden), sorted(called & forbidden)


def test_no_portfolio_module_reimplements_the_acceptance_rule() -> None:
    """One definition of "accepted", and it is the kernel validator's.

    A second implementation would be a second answer, and the second one would
    be this layer's.
    """

    offenders = [
        f"{path.name}: defines {node.name}"
        for path in _sources()
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
        if isinstance(node, ast.FunctionDef)
        and node.name in {"claim_approval", "qualifying_review", "accept_claim"}
    ]
    assert offenders == [], "; ".join(offenders)


def test_the_curator_cannot_reason() -> None:
    """The Curator renders rows. It has no way to ask anything.

    Stated in its docstring and asserted here, because "deterministic" is the
    property that makes the bank reproducible and a single model call inside it
    would end that without any test failing.
    """

    source = (PORTFOLIO_DIR / "curator.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported = {
        module
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module
        for module in (node.module,)
    }
    forbidden = {
        "research_os.portfolio.prompts",
        "research_os.portfolio.contracts",
        "research_os.portfolio.runner",
        "research_os.runtime.routing",
        "research_os.runtime.prompts",
    }
    assert not (imported & forbidden), sorted(imported & forbidden)
    assert "ModelRequest" not in source
    assert "complete(" not in source


def test_the_digest_is_rendered_and_not_written() -> None:
    """The one artifact a returning researcher reads instead of everything else.

    An unlabelled model narrative there would do more damage per word than
    anywhere else in the system, so the module that produces it must have no
    way to obtain one.
    """

    source = (PORTFOLIO_DIR / "digest.py").read_text(encoding="utf-8")
    assert "ModelRequest" not in source
    assert "prompts" not in source
    assert ".complete(" not in source


def test_the_allocator_asks_no_model() -> None:
    """`researchd` runs the tick, and the daemon calls no model.

    ``DESIGN_INVARIANTS.md``'s R5 record says so, and an earlier draft of the
    allocator had a model tie-breaker that would have made it false -- in the
    one process where the statement is load bearing. The runtime's own guard
    could not have caught it: the AST test that looks for a model call parses
    ``daemon.py`` only.
    """

    for name in ("allocation.py", "tick.py"):
        source = (PORTFOLIO_DIR / name).read_text(encoding="utf-8")
        assert "ModelRequest" not in source, name
        assert ".complete(" not in source, name
        assert "models" not in {
            node.arg
            for node in ast.walk(ast.parse(source))
            if isinstance(node, ast.arg)
        }, name


def test_the_curator_refuses_to_write_under_the_capsule() -> None:
    """One character between the bank and the researcher's science.

    ``.research-os/`` and ``.research/``. The second is what the kernel
    validator reads, and an autonomous tree it would try to parse is a tree
    that can produce a validation failure in the researcher's own project.
    """

    from research_os.portfolio.curator import BANK_ROOT, CuratorError, _safe

    assert BANK_ROOT.startswith(".research-os/")
    with pytest.raises(CuratorError):
        _safe(".research/claims/CLM-0001.yaml")
    with pytest.raises(CuratorError):
        _safe(".research")
    assert _safe(f"{BANK_ROOT}/IDEAS.md")


def test_every_portfolio_id_is_distinguishable_from_a_capsule_id() -> None:
    """A runtime id and a scientific id must never be mistakable.

    ``research_os/runtime/ids.py`` states the rule and the reason: the whole
    authority model rests on them being different kinds of thing. The capsule's
    Idea is ``IDEA-0001``; this layer's is ``PIDEA-...`` and must not match the
    capsule's pattern even by accident.
    """

    import re

    from research_os.portfolio.ids import (
        ID_PATTERNS,
        new_idea_action_id,
        new_idea_id,
        new_idea_review_id,
    )

    capsule_like = re.compile(r"^(Q|IDEA|HYP|ASM|CLM|DEC|EXP|EVI|REV)-[0-9]{4}$")
    for minted in (new_idea_id(), new_idea_action_id(), new_idea_review_id()):
        assert not capsule_like.match(minted), minted
    assert ID_PATTERNS["idea"].match(new_idea_id())


# --------------------------------------------------------- what is said --
def test_every_rendered_page_says_no_human_has_evaluated_it() -> None:
    """The header is not optional, and it is not decoration.

    ``VALIDATED`` is a scientific word, and the only other thing in this system
    that is validated is a capsule Claim a person reviewed. A researcher
    reading ``bank/VALIDATED.md`` in their own repository is not reading the
    architecture document.
    """

    from research_os.portfolio.curator import HEADER_MARKER, render_bank, render_index
    from research_os.portfolio.models import IdeaStatus

    class _EmptyStore:
        def get_version(self, _idea_id: str) -> None:
            return None

    store = _EmptyStore()
    for page in (
        render_index(store, []),  # type: ignore[arg-type]
        render_bank(store, [], status=IdeaStatus.VALIDATED),  # type: ignore[arg-type]
        render_bank(store, [], status=IdeaStatus.HUMAN_READY),  # type: ignore[arg-type]
    ):
        assert page.startswith(HEADER_MARKER), page[:120]


def test_the_cli_says_what_kind_of_object_it_is_listing() -> None:
    """`researchctl ideas` and `.research/ideas/` are different things."""

    from research_os.portfolio.commands import KIND_BANNER

    assert "PIDEA" in KIND_BANNER
    assert "IDEA-0001" in KIND_BANNER
    assert "is scientific state" in KIND_BANNER
    assert "none of them" in KIND_BANNER


def test_the_human_ready_page_does_not_claim_the_ideas_are_true() -> None:
    from research_os.portfolio.curator import render_bank
    from research_os.portfolio.models import IdeaStatus

    class _EmptyStore:
        def get_version(self, _idea_id: str) -> None:
            return None

    page = " ".join(
        render_bank(_EmptyStore(), [], status=IdeaStatus.HUMAN_READY).split()  # type: ignore[arg-type]
    )
    assert "not a claim that they are true" in page
    assert "nothing here has been reviewed by a person" in page


def test_the_authority_table_places_every_portfolio_verb() -> None:
    """One authority table for the whole system.

    A second one would eventually disagree with the first about what needs a
    person, and the disagreement would be discovered by something doing it.
    """

    from research_os.runtime.policy import ACTIONS, AutonomyLevel, Dispatch

    portfolio_actions = {
        action: policy
        for action, policy in ACTIONS.items()
        if policy.dispatch is Dispatch.PORTFOLIO
    }
    assert portfolio_actions, "the portfolio's verbs are not in the policy table"
    assert all(
        policy.level is not AutonomyLevel.A2 for policy in portfolio_actions.values()
    ), "no portfolio action may require human scientific authority; none of them "
    "touches scientific state"
    assert all(not policy.human_executes for policy in portfolio_actions.values())
