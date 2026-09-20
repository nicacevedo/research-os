"""The Curator: deterministic, idempotent, and unable to touch the science.

Three of these tests are about what the Curator writes. The other five are
about what it must not be able to do, and each of those closes a way the bank
could have quietly become something a researcher would read as their own work.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_os.automation import gitutil
from research_os.portfolio.curator import (
    BANK_ROOT,
    HEADER_MARKER,
    UnexpectedBankTipError,
    curate,
    snapshot,
    snapshot_digest,
    worktree_root,
)
from research_os.portfolio.models import EvidenceKind, EvidenceStrength, IdeaStatus
from research_os.portfolio.store import PortfolioStore
from research_os.runtime.actions.coding import canonical_fingerprint, escaped
from research_os.runtime.db import Database
from research_os.runtime.refs import AUTONOMOUS_BANK_BRANCH
from tests.fs_helpers import make_git_repo
from tests.portfolio_helpers import portfolio, record_review, seed_idea
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_project, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_project", "runtime_xdg"]


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    """A project repository with one commit, on its own default branch."""

    repo = make_git_repo(tmp_path / "project")
    (repo / "README.md").write_text("a scientific project\n", encoding="utf-8")
    for args in (
        ["config", "user.email", "researcher@example.invalid"],
        ["config", "user.name", "A Researcher"],
        ["add", "README.md"],
        ["commit", "-m", "first"],
    ):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    return repo


def _curate(runtime_db: Database, project: str, repository: Path):
    return curate(db=runtime_db, project_id=project, repository=repository)


# ---------------------------------------------------------- what it writes --
def test_the_bank_lands_on_its_own_branch_and_nowhere_else(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    idea, _ = seed_idea(portfolio, runtime_project)
    before = gitutil.head_commit(repository)
    branch_before = gitutil.current_branch(repository)

    result = _curate(runtime_db, runtime_project, repository)

    assert result.changed
    assert result.branch == AUTONOMOUS_BANK_BRANCH
    # The researcher's checkout is exactly where they left it.
    assert gitutil.head_commit(repository) == before
    assert gitutil.current_branch(repository) == branch_before
    assert gitutil.porcelain_status(repository) == ()
    assert not (repository / BANK_ROOT).exists()

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", AUTONOMOUS_BANK_BRANCH],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert f"{BANK_ROOT}/IDEAS.md" in listing
    assert f"{BANK_ROOT}/ideas/{idea.idea_id}.md" in listing
    assert f"{BANK_ROOT}/bank/VALIDATED.md" in listing
    assert not any(name.startswith(".research/") for name in listing)


def test_the_curator_worktree_is_outside_the_researchers_repository(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """Their ``git status`` must never mention the Curator."""

    seed_idea(portfolio, runtime_project)
    _curate(runtime_db, runtime_project, repository)
    target = worktree_root() / runtime_project
    assert target.exists()
    assert repository not in target.parents
    assert gitutil.porcelain_status(repository) == ()


def test_every_page_carries_what_was_actually_done(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """The header, with counts computed from rows rather than asserted.

    A researcher reading `bank/VALIDATED.md` is not reading the architecture
    document. What they must see beside the word is how many executions, how
    many retrieved sources and how many distinct reviewer models it rests on.
    """

    idea, _ = seed_idea(portfolio, runtime_project)
    portfolio.add_evidence(
        idea_id=idea.idea_id,
        idea_version=1,
        kind=EvidenceKind.LITERATURE,
        strength=EvidenceStrength.SUPPORTS,
        summary="a retrieved source",
        literature_key="openalex:W1",
    )
    record_review(
        portfolio,
        idea_id=idea.idea_id,
        version=1,
        role=__import__(
            "research_os.portfolio.models", fromlist=["ReviewerRole"]
        ).ReviewerRole.METHODOLOGY,
    )
    portfolio.set_status(idea_id=idea.idea_id, status=IdeaStatus.VALIDATED)

    files = snapshot(runtime_db, runtime_project)
    for path, body in files.items():
        if path.endswith(".md"):
            assert body.startswith(HEADER_MARKER), path
    record = files[f"{BANK_ROOT}/ideas/{idea.idea_id}.md"]
    assert "sources retrieved: 1" in record
    assert "distinct reviewer models: 1" in record
    assert "executions performed: 0" in record


# ------------------------------------------------------------- what it is --
def test_curating_twice_with_nothing_changed_commits_nothing(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    seed_idea(portfolio, runtime_project)
    first = _curate(runtime_db, runtime_project, repository)
    second = _curate(runtime_db, runtime_project, repository)
    assert first.changed
    assert not second.changed
    assert second.digest == first.digest
    assert second.commit == first.commit


def test_the_snapshot_is_a_function_of_state_and_not_of_ordering(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """Determinism, asserted by rendering twice.

    Anything that let dictionary order reach a file would make the bank's diff
    noise and the snapshot digest meaningless -- and the digest is what makes
    curating idempotent.
    """

    for index in range(5):
        seed_idea(portfolio, runtime_project, title=f"idea {index}")
    first = snapshot(runtime_db, runtime_project)
    second = snapshot(runtime_db, runtime_project)
    assert first == second
    assert snapshot_digest(first) == snapshot_digest(second)


def test_a_change_moves_the_digest(
    portfolio: PortfolioStore, runtime_db: Database, runtime_project: str
) -> None:
    """The control for the test above, which would otherwise pass on a
    renderer that emitted a constant."""

    idea, _ = seed_idea(portfolio, runtime_project)
    before = snapshot_digest(snapshot(runtime_db, runtime_project))
    portfolio.set_status(
        idea_id=idea.idea_id,
        status=IdeaStatus.REJECTED,
        retire_reason="subsumed",
    )
    assert snapshot_digest(snapshot(runtime_db, runtime_project)) != before


def test_the_curator_refuses_a_tip_it_did_not_write(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """The check that covers the reserved-ref blind spot.

    The autonomous namespace is excluded from the coding pipeline's escape
    fingerprint, which is what lets curating and a coding run overlap. That
    exclusion is a blind spot, and this is what is on the other side of it:
    the Curator knows what it last wrote and will not build on anything else.
    """

    seed_idea(portfolio, runtime_project)
    _curate(runtime_db, runtime_project, repository)

    target = worktree_root() / runtime_project
    (target / "tampered.txt").write_text("somebody else was here\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=target, check=True, capture_output=True)
    subprocess.run(
        [
            "git",
            "-c",
            "user.email=x@y.invalid",
            "-c",
            "user.name=X",
            "commit",
            "-m",
            "not the curator",
        ],
        cwd=target,
        check=True,
        capture_output=True,
    )

    seed_idea(portfolio, runtime_project, title="something new to write")
    with pytest.raises(UnexpectedBankTipError, match="Refusing to commit"):
        _curate(runtime_db, runtime_project, repository)


def test_curating_does_not_make_a_concurrent_coding_run_report_an_escape(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """The shipping blocker an independent review found, closed and asserted.

    ``canonical_fingerprint`` hashes every ref in the project repository before
    a coding run and again after it, and any ref that moved is reported as an
    escape. The Curator commits to a branch in that same repository on its own
    schedule. Without the reserved namespace, running the portfolio and a
    coding cycle on one project at once would fail the coding cycle for
    something it did not do.
    """

    seed_idea(portfolio, runtime_project)
    before = canonical_fingerprint(repository)
    _curate(runtime_db, runtime_project, repository)
    seed_idea(portfolio, runtime_project, title="and another")
    _curate(runtime_db, runtime_project, repository)
    after = canonical_fingerprint(repository)

    assert not escaped(before, after), (
        f"the Curator moved a ref the escape check watches: "
        f"{sorted(set(before.items()) ^ set(after.items()))}"
    )
    assert not any("research-os/autonomous" in key for key in after)


def test_a_real_escape_is_still_detected(
    repository: Path,
) -> None:
    """The control for the exemption.

    An exemption that swallowed every ref would make the escape check
    vacuous, and a vacuous escape check is worse than none: it reports
    success.
    """

    before = canonical_fingerprint(repository)
    subprocess.run(
        ["git", "branch", "something-else"],
        cwd=repository,
        check=True,
        capture_output=True,
    )
    after = canonical_fingerprint(repository)
    assert escaped(before, after)


def test_the_bank_branch_carries_none_of_the_researchers_work(
    portfolio: PortfolioStore,
    runtime_db: Database,
    runtime_project: str,
    repository: Path,
) -> None:
    """A true orphan, and the first version of this was not one.

    Branching from HEAD put a copy of the researcher's `.research/` capsule on
    the bank branch -- which is the exact thing §12 of the architecture says
    must not happen, because a capsule directory on an autonomous branch is one
    the kernel validator would try to read and a person could merge without
    noticing. A smoke test against a real repository found it while the
    docstring claimed the right behaviour.
    """

    seed_idea(portfolio, runtime_project)
    _curate(runtime_db, runtime_project, repository)

    listing = subprocess.run(
        ["git", "ls-tree", "-r", "--name-only", AUTONOMOUS_BANK_BRANCH],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.split()
    assert listing
    assert all(name.startswith(BANK_ROOT) for name in listing), listing
    assert not any(name.startswith(".research/") for name in listing)
    assert "README.md" not in listing

    # And it shares no history with the science.
    merge_base = subprocess.run(
        ["git", "merge-base", "HEAD", AUTONOMOUS_BANK_BRANCH],
        cwd=repository,
        capture_output=True,
        text=True,
        check=False,
    )
    assert merge_base.returncode != 0, (
        "the bank branch must share no ancestor with the researcher's work"
    )
