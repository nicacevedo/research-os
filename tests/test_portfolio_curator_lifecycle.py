"""The Curator's checkout, in every state a real machine leaves it in.

The first live qualification, on 2026-09-24, failed ``portfolio_curate`` with

    GitError: git worktree remove --force <state>/curator/cg-sparse-regression
    failed in <canonical repository> (128): fatal: '<state>/curator/
    cg-sparse-regression' is not a working tree

and the cause was not exotic. The state home was the dogfood's, and its
Curator directory for ``cg-sparse-regression`` was a worktree of the *older
clone* the dogfood had used. The portfolio had since been enabled on the
canonical repository, so the Curator decided -- correctly -- that the
directory was not its worktree, and then asked the canonical repository to
remove a worktree that only the old clone knew about. Git refused, and it was
right to.

That failure came second. The Curator had already created the autonomous
branch in the canonical repository, an orphan root, one second before the
worktree step failed, and it recorded nothing. So every retry would have met a
branch "this system has no record of writing" and refused for good. That is
the compensating control for the reserved ref namespace doing its job, against
the Curator's own leftovers. The portfolio was wedged by its own first
attempt.

The repair has three parts, and an independent review of a first attempt is
why it has the second and third. The checkout path is keyed on the repository
as well as the project, so two repositories never share one -- the first
attempt instead removed the old clone's worktree *through the old clone*,
which the review showed could remove another installation's live checkout
mid-write and commit one repository's bank onto another's branch. The branch
is moved by compare-and-swap from a detached checkout, with the commit
recorded before it is published, so no failure can leave a Curator commit on
the branch unrecorded. And one ``flock`` per checkout serialises every
installation sharing a state home, which is also what makes a Git lock file
left in the checkout provably stale.

So this file drives the production Curator -- real Git, real directories, no
double for the lifecycle under test -- through each state and asserts three
things every time: curation succeeds (or refuses with a sentence, where
refusing is right), nothing is left registered that points nowhere or at the
wrong checkout, and the researcher's repository and capsule are
byte-for-byte what they were, apart from the one branch the Curator owns.
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_os.automation import gitutil
from research_os.portfolio import curator as curator_module
from research_os.portfolio import extensions as portfolio_extensions
from research_os.portfolio.allocation import CURATE, EXPLORE
from research_os.portfolio.curator import (
    BANK_ROOT,
    EMPTY_TREE,
    CuratorError,
    UnexpectedBankTipError,
    checkout_path,
    curate,
    worktree_root,
)
from research_os.portfolio.store import PortfolioStore
from research_os.runlock import lock_path
from research_os.runtime.clock import Clock
from research_os.runtime.daemon import Daemon, default_repo_resolver
from research_os.runtime.db import Database
from research_os.runtime.models import WorkStatus
from research_os.runtime.queue import WorkQueue
from research_os.runtime.refs import AUTONOMOUS_BANK_BRANCH
from research_os.runtime.store import RuntimeStore
from tests.portfolio_helpers import portfolio, seed_idea
from tests.runtime_graph_helpers import ScriptedRouter, make_capsule, make_config
from tests.runtime_helpers import RUNTIME_TABLES, pg_dsn, runtime_db, runtime_xdg

__all__ = ["pg_dsn", "portfolio", "runtime_db", "runtime_xdg"]

PROJECT = "cg-sparse-regression"
BANK_REF = f"refs/heads/{AUTONOMOUS_BANK_BRANCH}"


class FrozenClock(Clock):
    def now(self):  # type: ignore[override]
        from datetime import UTC, datetime

        return datetime(2026, 9, 24, 20, 43, tzinfo=UTC)

    def sleep(self, seconds: float) -> None:
        del seconds


# --------------------------------------------------------------- helpers --
def git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, check=True, capture_output=True, text=True
    ).stdout


def registered(repo: Path) -> dict[Path, str]:
    """This repository's worktree registrations, and each one's porcelain block."""

    found: dict[Path, str] = {}
    for block in git(repo, "worktree", "list", "--porcelain").strip().split("\n\n"):
        lines = block.splitlines()
        if lines and lines[0].startswith("worktree "):
            found[Path(lines[0].removeprefix("worktree ")).resolve()] = block
    return found


def target_of(repo: Path) -> Path:
    """The Curator's checkout for this project in ``repo``."""

    return checkout_path(PROJECT, repo)


def listed_as(repo: Path) -> Path:
    """That checkout's path as Git lists it, without following a link there."""

    return worktree_root().resolve() / target_of(repo).name


def legacy_path() -> Path:
    """Where the Curator kept every checkout before the repair: project id only."""

    return worktree_root() / PROJECT


def fingerprint(repo: Path) -> dict[str, object]:
    """Everything about the researcher's repository the Curator must not move.

    Every ref but the bank branch, where HEAD is and what it names, the
    working tree's status, the repository's own config and every worktree
    registration but the Curator's -- and the capsule, byte for byte.
    """

    refs = sorted(
        line
        for line in git(repo, "for-each-ref", "--format=%(refname) %(objectname)")
        .strip()
        .splitlines()
        if not line.startswith(f"{BANK_REF} ")
    )
    capsule = {
        str(path.relative_to(repo)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted((repo / ".research").rglob("*"))
        if path.is_file()
    }
    others = {
        path: block
        for path, block in registered(repo).items()
        if path != listed_as(repo)
    }
    return {
        "head": git(repo, "rev-parse", "HEAD").strip(),
        "symbolic": git(repo, "symbolic-ref", "-q", "HEAD").strip(),
        "status": git(repo, "status", "--porcelain", "--ignored"),
        "refs": refs,
        "config": (repo / ".git" / "config").read_bytes(),
        "worktrees": others,
        "capsule": capsule,
    }


def assert_one_clean_registration(repo: Path) -> None:
    """Exactly one Curator checkout: present, detached, unlocked, not prunable.

    Detached, and the reserved branch checked out nowhere: the Curator moves
    the branch by compare-and-swap, so no checkout ever holds it.
    """

    target = listed_as(repo)
    entries = registered(repo)
    assert target in entries, entries
    block = entries[target]
    assert "detached" in block.splitlines(), block
    assert "prunable" not in block, block
    assert "locked" not in block, block
    assert (target / ".git").is_file()
    assert not any(
        f"branch {BANK_REF}" in other.splitlines() for other in entries.values()
    ), entries
    ours = [path for path in entries if path.parent == worktree_root().resolve()]
    assert ours == [target], ours


def bank_files(repo: Path) -> list[str]:
    return git(repo, "ls-tree", "-r", "--name-only", AUTONOMOUS_BANK_BRANCH).split()


def assert_bank_holds(repo: Path, store: PortfolioStore) -> None:
    """The bank on the branch is the portfolio's current ideas, all of them."""

    listing = bank_files(repo)
    ideas = store.list_ideas(project_id=PROJECT)
    assert ideas
    for idea in ideas:
        assert f"{BANK_ROOT}/ideas/{idea.idea_id}.md" in listing
    state = store.get_state(PROJECT)
    assert state is not None
    assert state.bank_commit == git(repo, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip()
    assert store.uncurated_count(PROJECT) == 0


def assert_rooted_on_the_curators_own_root(repo: Path) -> None:
    roots = git(repo, "rev-list", "--max-parents=0", AUTONOMOUS_BANK_BRANCH).split()
    assert len(roots) == 1
    assert git(repo, "rev-parse", f"{roots[0]}^{{tree}}").strip() == EMPTY_TREE


def reset_operational_database(db: Database) -> None:
    """What a second installation, or a fresh qualification database, starts from."""

    with db.tx() as conn:
        conn.execute(f"truncate {', '.join(RUNTIME_TABLES)} restart identity cascade")


def legacy_checkout(repo: Path) -> Path:
    """Build what the pre-repair Curator left: a checkout at the project-id path.

    On the bank branch, with a commit on it -- exactly the dogfood's state on
    2026-09-24. Made with Git directly, because the code that made it is the
    code being replaced.
    """

    env = ["-c", "user.name=Research OS Curator", "-c", "user.email=c@x.invalid"]
    root = git(repo, *env, "commit-tree", EMPTY_TREE, "-m", "Autonomous bank: root")
    git(repo, "branch", AUTONOMOUS_BANK_BRANCH, root.strip())
    target = legacy_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    git(repo, "worktree", "add", "-q", str(target), AUTONOMOUS_BANK_BRANCH)
    (target / BANK_ROOT).mkdir(parents=True)
    (target / BANK_ROOT / "IDEAS.md").write_text(
        "the dogfood's bank\n", encoding="utf-8"
    )
    git(target, "add", "-A")
    git(target, *env, "commit", "-q", "-m", "Autonomous bank: 1 file(s)")
    return target


@pytest.fixture
def canonical(tmp_path: Path) -> Path:
    return make_capsule(
        tmp_path / "column-generation-for-large-scale-feature-selection",
        project_id=PROJECT,
    )


@pytest.fixture
def older_clone(canonical: Path, tmp_path: Path) -> Path:
    """The dogfood's checkout: a clone of the canonical repository."""

    clone = tmp_path / "research-os-dogfood" / "project"
    clone.parent.mkdir(parents=True)
    subprocess.run(
        ["git", "clone", "-q", str(canonical), str(clone)],
        check=True,
        capture_output=True,
    )
    return clone


@pytest.fixture
def project(runtime_db: Database, canonical: Path) -> str:
    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    return PROJECT


def _curate(db: Database, repo: Path):
    return curate(db=db, project_id=PROJECT, repository=repo)


def _daemon(
    db: Database, dsn: str, tmp_path: Path, router: ScriptedRouter | None = None
) -> Daemon:
    """The control plane as `researchd` composes it: the repository comes from
    the operational ``projects`` table, not from the test."""

    portfolio_extensions.register()
    store = RuntimeStore(db)
    router = router or ScriptedRouter(answers={}, store=store)
    return Daemon(
        config=make_config(dsn, tmp_path / "artifacts"),
        db=db,
        repo_for=default_repo_resolver(store),
        models=lambda _run, _project, _work: router,
        clock=FrozenClock(),
        owner="curator-lifecycle-worker",
    )


def _drain(daemon: Daemon, passes: int = 6) -> None:
    for _ in range(passes):
        report = daemon.tick()
        if not report.did_something:
            break


def _work(db: Database, kind: str) -> list:
    with db.tx() as conn:
        rows = conn.execute(
            "select work_id from work_items where project_id = %s and kind = %s "
            "order by created_at, work_id",
            (PROJECT, kind),
        ).fetchall()
    queue = WorkQueue(db)
    return [queue.get(row["work_id"]) for row in rows]


# ------------------------------------------------- the live failure, first --
def test_the_september_24_failure_a_re_pointed_project_curates_first_time(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    canonical: Path,
    older_clone: Path,
) -> None:
    """The live state, through the daemon, with the production resolver.

    The state home holds the pre-repair Curator's checkout for this project
    -- registered by the dogfood's *older clone*, on that clone's bank
    branch. A fresh operational database -- the qualification's -- enables
    the same project on the canonical repository and curates. That curation
    is the one that failed live.
    """

    store = PortfolioStore(runtime_db)
    legacy = legacy_checkout(older_clone)
    old_clone_before = fingerprint(older_clone)
    old_bank_tip = git(older_clone, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip()
    old_registration = registered(older_clone)[legacy.resolve()]

    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    before = fingerprint(canonical)
    assert not gitutil.branch_exists(canonical, AUTONOMOUS_BANK_BRANCH)
    for n in range(12):
        seed_idea(store, PROJECT, title=f"a live candidate {n}")

    daemon = _daemon(runtime_db, pg_dsn, tmp_path)
    WorkQueue(runtime_db).enqueue(project_id=PROJECT, kind=CURATE, payload={})
    _drain(daemon)

    items = _work(runtime_db, CURATE)
    assert [item.status for item in items] == [WorkStatus.SUCCEEDED], [
        (item.status, item.last_error) for item in items
    ]
    assert not (items[0].result or {}).get("refused"), items[0].result
    assert_bank_holds(canonical, store)
    assert_one_clean_registration(canonical)
    assert_rooted_on_the_curators_own_root(canonical)
    assert fingerprint(canonical) == before

    # The dogfood's evidence is exactly where the dogfood left it: its clone,
    # its bank, and its checkout, still registered, still valid. Nothing in
    # this installation has any business touching another repository.
    assert fingerprint(older_clone) == old_clone_before
    assert registered(older_clone)[legacy.resolve()] == old_registration
    assert git(older_clone, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip() == old_bank_tip
    assert (legacy / BANK_ROOT / "IDEAS.md").read_text() == "the dogfood's bank\n"

    # --- and the retry that used to be refused for good ------------------
    seed_idea(store, PROJECT, title="a candidate after the first curation")
    WorkQueue(runtime_db).enqueue(
        project_id=PROJECT, kind=CURATE, payload={}, dedup_key="curate-again"
    )
    _drain(daemon)
    statuses = [item.status for item in _work(runtime_db, CURATE)]
    assert statuses == [WorkStatus.SUCCEEDED, WorkStatus.SUCCEEDED]
    assert_bank_holds(canonical, store)
    assert fingerprint(canonical) == before
    assert fingerprint(older_clone) == old_clone_before


def test_deleting_a_checkout_another_repository_registered_cross_links_nothing(
    runtime_db: Database,
    portfolio: PortfolioStore,
    canonical: Path,
    older_clone: Path,
) -> None:
    """``rm -rf`` of the stuck directory -- the obvious workaround for the live failure.

    With one path per project, the next checkout was made where the deleted
    one had been, and the clone's registration then pointed at it: valid,
    not prunable, and describing somebody else's checkout, which wedged the
    clone's own curation for good. With a path per repository the clone's
    registration only goes stale, and Git's own maintenance prunes it.
    """

    legacy = legacy_checkout(older_clone)
    shutil.rmtree(legacy)
    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    seed_idea(portfolio, PROJECT)
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert "prunable" in registered(older_clone)[legacy.resolve()]
    assert not legacy.exists()

    # And the clone, curated again by its own installation, curates.
    reset_operational_database(runtime_db)
    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(older_clone)
    )
    tip = git(older_clone, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip()
    portfolio.upsert_state(project_id=PROJECT)
    portfolio.record_bank_write(project_id=PROJECT, commit=tip, digest="the old bank")
    seed_idea(portfolio, PROJECT, title="the dogfood again")
    assert _curate(runtime_db, older_clone).changed
    assert_bank_holds(older_clone, portfolio)
    assert_one_clean_registration(canonical)


def test_a_checkout_per_repository_never_collides(
    runtime_db: Database, canonical: Path, older_clone: Path
) -> None:
    """Two checkouts of one project get two directories, by construction."""

    assert target_of(canonical) != target_of(older_clone)
    assert (
        target_of(canonical).parent == target_of(older_clone).parent == worktree_root()
    )
    assert target_of(canonical) == checkout_path(
        PROJECT, canonical / ".." / canonical.name
    )


# ---------------------------------------------------- the required cases --
def test_1_a_normal_registered_checkout_is_adopted_not_rebuilt(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    first = _curate(runtime_db, canonical)
    marker = target_of(canonical) / ".git"
    inode = marker.stat().st_ino

    seed_idea(portfolio, project, title="a second idea")
    second = _curate(runtime_db, canonical)

    assert first.changed and second.changed
    assert marker.stat().st_ino == inode  # the same checkout, not a new one
    parents = git(canonical, "rev-list", "--parents", "-n", "1", AUTONOMOUS_BANK_BRANCH)
    assert parents.split() == [second.commit, first.commit]
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_2_a_directory_git_no_longer_registers_is_replaced(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """The metadata went; the directory stayed."""

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    target = target_of(canonical)
    admin = Path(
        git(target, "rev-parse", "--path-format=absolute", "--git-dir").strip()
    )
    shutil.rmtree(admin)
    assert target.resolve() not in registered(canonical)
    assert (target / ".git").is_file()

    seed_idea(portfolio, project, title="written after the metadata vanished")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_2b_a_repository_re_cloned_in_place_leaves_a_gitlink_to_nothing(
    runtime_db: Database,
    portfolio: PortfolioStore,
    canonical: Path,
    older_clone: Path,
) -> None:
    """The repository deleted and cloned again at the same path.

    Same path, so the same checkout directory -- whose gitlink now names an
    administrative directory that went with the old ``.git``.
    """

    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    seed_idea(portfolio, PROJECT)
    _curate(runtime_db, canonical)
    target = target_of(canonical)
    shutil.rmtree(canonical)
    subprocess.run(
        ["git", "clone", "-q", str(older_clone), str(canonical)],
        check=True,
        capture_output=True,
    )
    assert target_of(canonical) == target
    assert (target / ".git").is_file()

    reset_operational_database(runtime_db)
    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    before = fingerprint(canonical)
    seed_idea(portfolio, PROJECT, title="after the re-clone")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_2c_a_directory_with_no_gitlink_at_all_is_replaced(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """What an interrupted ``worktree add`` leaves before it writes ``.git``."""

    before = fingerprint(canonical)
    target = target_of(canonical)
    (target / BANK_ROOT).mkdir(parents=True)
    (target / BANK_ROOT / "IDEAS.md").write_text("half a checkout\n", encoding="utf-8")

    seed_idea(portfolio, project)
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert "half a checkout" not in (target / BANK_ROOT / "IDEAS.md").read_text()
    assert fingerprint(canonical) == before


def test_3_a_registration_whose_directory_is_missing_is_cleared(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """Git: "is a missing but already registered worktree" -- until now, forever."""

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    shutil.rmtree(target_of(canonical))
    assert "prunable" in registered(canonical)[listed_as(canonical)]

    seed_idea(portfolio, project, title="written after the directory vanished")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_3b_a_registration_whose_gitlink_is_missing_is_cleared(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """An interrupted ``worktree remove``: directory there, ``.git`` gone.

    ``git worktree remove --force`` refuses that state ("validation failed"),
    so the one command the old code knew could not have cleared it.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    (target_of(canonical) / ".git").unlink()

    seed_idea(portfolio, project, title="written after an interrupted removal")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_4_repeated_cleanup_is_idempotent(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """Every damage, one after another, and a quiet pass between each."""

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    target = target_of(canonical)

    def missing_directory() -> None:
        shutil.rmtree(target)

    def missing_gitlink() -> None:
        (target / ".git").unlink()

    def missing_metadata() -> None:
        shutil.rmtree(
            Path(
                git(target, "rev-parse", "--path-format=absolute", "--git-dir").strip()
            )
        )

    def drifted_and_dirty() -> None:
        roots = git(canonical, "rev-list", "--max-parents=0", AUTONOMOUS_BANK_BRANCH)
        git(target, "checkout", "-q", "-f", "--detach", roots.split()[0])
        (target / "stray.txt").write_text("left by something\n", encoding="utf-8")

    def stale_index_lock() -> None:
        admin = Path(
            git(target, "rev-parse", "--path-format=absolute", "--git-dir").strip()
        )
        (admin / "index.lock").write_text("", encoding="utf-8")

    for n, damage in enumerate(
        (
            missing_directory,
            missing_gitlink,
            missing_metadata,
            drifted_and_dirty,
            stale_index_lock,
        )
        * 2
    ):
        damage()
        seed_idea(portfolio, project, title=f"idea after damage {n}")
        assert _curate(runtime_db, canonical).changed
        assert not _curate(runtime_db, canonical).changed
        assert_one_clean_registration(canonical)
        assert_bank_holds(canonical, portfolio)
        assert "stray.txt" not in bank_files(canonical)
    assert fingerprint(canonical) == before
    assert_rooted_on_the_curators_own_root(canonical)


def test_5_an_interrupted_curation_does_not_wedge_the_next(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Killed after the branch existed and before the checkout did.

    Produced by a real failure rather than a patched function: the Curator's
    checkout directory cannot be created, because something occupies the
    place its parent should be. The first curation has made the autonomous
    branch by then. The old code recorded nothing about it, so the retry met
    a branch "this system has no record of writing" and refused -- on every
    pass, for good.
    """

    before = fingerprint(canonical)
    root = worktree_root()
    root.parent.mkdir(parents=True, exist_ok=True)
    root.write_text("not a directory\n", encoding="utf-8")
    seed_idea(portfolio, project)
    # Raised through the repository lock's connection, which wraps it.
    with pytest.raises(Exception, match="File exists"):
        _curate(runtime_db, canonical)
    assert gitutil.branch_exists(canonical, AUTONOMOUS_BANK_BRANCH)

    root.unlink()
    result = _curate(runtime_db, canonical)
    assert result.changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)
    assert_rooted_on_the_curators_own_root(canonical)
    assert fingerprint(canonical) == before


def test_the_root_is_recorded_before_the_branch_exists(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Order, not just presence: record first, then create the ref.

    Recorded after, a failure between the two lines leaves exactly the
    unrecorded Curator branch that wedged the live qualification. The fault
    is injected where the record is written -- the database -- because that
    is the step whose failure the order exists for.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)

    def unavailable(self, **_kwargs):
        raise RuntimeError("the operational database went away")

    # Scoped rather than `monkeypatch.undo()`, which would also undo the
    # fixtures' state-home redirection and move the Curator's directory.
    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(PortfolioStore, "record_bank_intent", unavailable)
        with pytest.raises(Exception, match="went away"):
            _curate(runtime_db, canonical)
    assert not gitutil.branch_exists(canonical, AUTONOMOUS_BANK_BRANCH)

    assert _curate(runtime_db, canonical).changed
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_a_commit_is_never_on_the_branch_unrecorded(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Every commit, not only the root, is recorded before it is published.

    The watermark used to be written after the lock was released, so a
    database failure there left the Curator's own commit on the branch with
    no record -- and every later pass refused it, as "something else has
    written to this system's reserved ref namespace".
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    seed_idea(portfolio, project, title="written before the database went away")

    def unavailable(self, **_kwargs):
        raise RuntimeError("the operational database went away")

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(PortfolioStore, "record_bank_write", unavailable)
        with pytest.raises(RuntimeError, match="went away"):
            _curate(runtime_db, canonical)

    # The next pass accepts its own published commit, and confirms the bank.
    result = _curate(runtime_db, canonical)
    assert not result.changed or result.commit
    assert_bank_holds(canonical, portfolio)
    assert not _curate(runtime_db, canonical).changed
    assert fingerprint(canonical) == before


def test_a_publish_that_never_happened_is_not_mistaken_for_a_foreign_write(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Recorded, then killed before ``update-ref``: the tip is the intent's parent.

    The fault is injected at the one Git command that publishes, because a
    process that dies there leaves precisely this state and there is no
    other way to stop exactly between two lines.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    first = _curate(runtime_db, canonical)
    seed_idea(portfolio, project, title="intended but never published")
    real_git = curator_module.gitutil.git

    def killed_at_publish(args, **kwargs):
        if args[:1] == ["update-ref"] and args[1] == BANK_REF and len(args) == 4:
            raise KeyboardInterrupt("killed between the record and the publish")
        return real_git(args, **kwargs)

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(curator_module.gitutil, "git", killed_at_publish)
        with pytest.raises(KeyboardInterrupt):
            _curate(runtime_db, canonical)
    assert git(canonical, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip() == first.commit
    intended = portfolio.get_state(PROJECT).bank_commit
    assert intended != first.commit

    result = _curate(runtime_db, canonical)
    assert result.changed
    parents = git(canonical, "rev-list", "--parents", "-n", "1", AUTONOMOUS_BANK_BRANCH)
    assert parents.split()[1:] == [first.commit]
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_a_commit_whose_intent_cannot_be_recorded_is_not_published(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Record, *then* publish -- for every commit, as for the root.

    Published first, a failure to record leaves the Curator's commit on the
    branch with the watermark one behind, and every later pass refuses it.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    first = _curate(runtime_db, canonical)
    seed_idea(portfolio, project, title="whose intent is never recorded")

    def unavailable(self, **_kwargs):
        raise RuntimeError("the operational database went away")

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(PortfolioStore, "record_bank_intent", unavailable)
        with pytest.raises(Exception, match="went away"):
            _curate(runtime_db, canonical)
    assert git(canonical, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip() == first.commit
    assert _curate(runtime_db, canonical).changed
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_a_writer_that_moves_the_branch_mid_write_makes_the_publish_fail(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Compare-and-swap: the foreign commit survives, and is then refused.

    Between the tip check and the publish, something else commits to the
    reserved branch. A publish that was not conditional on the verified tip
    would overwrite that commit -- destroying the evidence the tip check
    exists to surface -- and root the bank's next commit on a history that
    silently skipped it.
    """

    seed_idea(portfolio, project)
    first = _curate(runtime_db, canonical)
    seed_idea(portfolio, project, title="written while somebody else was writing")
    env = ["-c", "user.name=X", "-c", "user.email=x@x.invalid"]
    real_git = curator_module.gitutil.git
    foreign: list[str] = []

    def interloper(args, **kwargs):
        if args[:1] == ["write-tree"] and not foreign:
            tree = git(canonical, "rev-parse", f"{first.commit}^{{tree}}").strip()
            made = git(
                canonical, *env, "commit-tree", tree, "-p", first.commit, "-m", "x"
            )
            git(canonical, "update-ref", BANK_REF, made.strip(), first.commit)
            foreign.append(made.strip())
        return real_git(args, **kwargs)

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(curator_module.gitutil, "git", interloper)
        with pytest.raises(Exception, match="expected"):
            _curate(runtime_db, canonical)
    assert git(canonical, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip() == foreign[0]
    with pytest.raises(UnexpectedBankTipError, match="Something else"):
        _curate(runtime_db, canonical)


def test_a_commit_that_lands_while_this_pass_waits_is_not_a_foreign_write(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """The watermark is read again under the locks.

    Another worker's whole curation lands after this pass read its state and
    before it took the locks. Checked against the stale watermark, that
    commit -- the Curator's own -- was refused as "something else has
    written to this system's reserved ref namespace", which is recorded as a
    security event and never retried.
    """

    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    seed_idea(portfolio, project, title="both workers see this")
    real_snapshot = curator_module.snapshot
    fired: list[bool] = []

    def another_worker_commits_first(db, project_id):
        if not fired:
            fired.append(True)
            seed_idea(portfolio, project, title="the other worker's idea")
            assert _curate(runtime_db, canonical).changed
        return real_snapshot(db, project_id)

    with pytest.MonkeyPatch.context() as race:
        race.setattr(curator_module, "snapshot", another_worker_commits_first)
        _curate(runtime_db, canonical)
    assert fired
    assert_bank_holds(canonical, portfolio)


def test_the_root_is_created_only_if_nobody_else_has_made_the_branch(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Create-only: a branch made between the check and the root is not replaced.

    The same compare-and-swap as every later commit, against "does not
    exist". Without it the Curator's root would overwrite whatever somebody
    created in the reserved namespace in the meantime.
    """

    seed_idea(portfolio, project)
    env = ["-c", "user.name=X", "-c", "user.email=x@x.invalid"]
    real_git = curator_module.gitutil.git
    planted: list[str] = []

    def interloper(args, **kwargs):
        # At the Curator's own `commit-tree EMPTY_TREE`: after the tip check,
        # before the root is published.
        if "commit-tree" in args and EMPTY_TREE in args and not planted:
            made = git(canonical, *env, "commit-tree", EMPTY_TREE, "-m", "x")
            git(canonical, "update-ref", BANK_REF, made.strip(), "")
            planted.append(made.strip())
        return real_git(args, **kwargs)

    with pytest.MonkeyPatch.context() as fault:
        fault.setattr(curator_module.gitutil, "git", interloper)
        with pytest.raises(Exception, match="already exists"):
            _curate(runtime_db, canonical)
    assert planted
    assert git(canonical, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip() == planted[0]
    with pytest.raises(UnexpectedBankTipError, match="Something else"):
        _curate(runtime_db, canonical)


def test_gits_own_initializing_lock_is_released_not_obeyed(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """``worktree add`` killed mid-way: registered, detached, locked "initializing".

    The lock is Git's, held while ``add`` works and removed when it finishes;
    one left behind has no owner, and the Curator's checkout lock proves it.
    Before this, the single ``--force`` was refused on every pass.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    target = target_of(canonical)
    gitutil.remove_worktree(repository=canonical, target=target)
    git(
        canonical,
        "worktree",
        "add",
        "-q",
        "--detach",
        "--lock",
        "--reason",
        "initializing",
        str(target),
        AUTONOMOUS_BANK_BRANCH,
    )
    seed_idea(portfolio, project, title="after the interrupted add")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert_bank_holds(canonical, portfolio)

    # And the same lock with the directory gone.
    shutil.rmtree(target)
    git(canonical, "worktree", "lock", "--reason", "initializing", str(target))
    seed_idea(portfolio, project, title="after the interrupted add, again")
    assert _curate(runtime_db, canonical).changed
    assert_one_clean_registration(canonical)
    assert fingerprint(canonical) == before


def test_6_and_9_a_cleanup_refused_by_a_person_blocks_nothing_else_and_retries(
    runtime_db: Database,
    pg_dsn: str,
    tmp_path: Path,
    canonical: Path,
) -> None:
    """A cleanup the Curator must not do, the work that carries on, and the retry.

    The checkout's directory is gone, and a person has *locked* its
    registration -- ``git worktree lock`` is a decision, and the Curator does
    not override one. So the curation fails, with a sentence saying how to
    lift it. While it is failed the portfolio's other work runs through the
    same queue. Once the lock is lifted the next curation succeeds, rather
    than meeting its own half-made branch and refusing it.
    """

    store = PortfolioStore(runtime_db)
    RuntimeStore(runtime_db).upsert_project(
        project_id=PROJECT, repo_path=str(canonical)
    )
    before = fingerprint(canonical)
    seed_idea(store, PROJECT)
    _curate(runtime_db, canonical)
    target = target_of(canonical)
    shutil.rmtree(target)
    git(canonical, "worktree", "lock", "--reason", "a person's decision", str(target))
    seed_idea(store, PROJECT, title="a candidate waiting for the bank")

    router = ScriptedRouter(
        answers={
            "blind_explorer": {
                "candidates": [
                    {
                        "title": "an unrelated direction",
                        "research_question": "Does the relaxation stay tight?",
                        "core_idea": "Tightness survives the reduced column set.",
                        "falsifier": "One instance with a strictly weaker bound.",
                    }
                ]
            }
        },
        store=RuntimeStore(runtime_db),
    )
    daemon = _daemon(runtime_db, pg_dsn, tmp_path, router)
    queue = WorkQueue(runtime_db)
    queue.enqueue(project_id=PROJECT, kind=CURATE, payload={}, dedup_key="curate-1")
    queue.enqueue(
        project_id=PROJECT,
        kind=EXPLORE,
        payload={"explorer": "blind_explorer", "reason": "unrelated work"},
    )
    _drain(daemon)

    (failed,) = _work(runtime_db, CURATE)
    assert failed.status is WorkStatus.FAILED
    assert "a person's decision" in (failed.last_error or "")
    assert "worktree unlock" in (failed.last_error or "")
    # Unrelated work was not held behind it.
    (explored,) = _work(runtime_db, EXPLORE)
    assert explored.status is WorkStatus.SUCCEEDED
    titles = {
        store.require_version(item.idea_id).title
        for item in store.list_ideas(project_id=PROJECT)
    }
    assert "an unrelated direction" in titles
    # The lock was honoured: the registration is where the person left it.
    assert "locked a person's decision" in registered(canonical)[listed_as(canonical)]
    assert fingerprint(canonical) == before

    # And *after* the failure, the portfolio is still running and still
    # schedulable: its own tick runs, and work asked for now is done now.
    # Enqueued after the failure deliberately -- work already in the queue
    # proves nothing about whether a failed curation stops new work.
    state = store.get_state(PROJECT)
    assert state is not None and state.status.name == "RUNNING"
    queue.enqueue(project_id=PROJECT, kind="portfolio_tick", payload={})
    queue.enqueue(
        project_id=PROJECT,
        kind=EXPLORE,
        payload={"explorer": "blind_explorer", "reason": "after the failure"},
        dedup_key="explore-after-the-failure",
    )
    _drain(daemon)
    (ticked,) = _work(runtime_db, "portfolio_tick")
    assert ticked.status is WorkStatus.SUCCEEDED, ticked.last_error
    explored_after = [
        item
        for item in _work(runtime_db, EXPLORE)
        if item.dedup_key == "explore-after-the-failure"
    ]
    assert [item.status for item in explored_after] == [WorkStatus.SUCCEEDED]
    state = store.get_state(PROJECT)
    assert state is not None and state.status.name == "RUNNING"

    git(canonical, "worktree", "unlock", str(target))
    queue.enqueue(project_id=PROJECT, kind=CURATE, payload={}, dedup_key="curate-2")
    _drain(daemon)
    statuses = [
        item.status
        for item in _work(runtime_db, CURATE)
        if item.dedup_key in {"curate-1", "curate-2"}
    ]
    assert statuses == [WorkStatus.FAILED, WorkStatus.SUCCEEDED]
    retried = next(
        item for item in _work(runtime_db, CURATE) if item.dedup_key == "curate-2"
    )
    assert not (retried.result or {}).get("refused"), retried.result
    assert_bank_holds(canonical, store)
    assert_one_clean_registration(canonical)
    assert fingerprint(canonical) == before


def test_a_second_installation_sharing_the_state_home_waits_its_turn(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Two installations, one state home, one checkout: one writer at a time.

    ``repository_lock`` lives in one operational database, so it does not
    serialise two -- and the first live qualification ran exactly that
    configuration. Another process holding the checkout's ``flock`` is that
    other installation, mid-write.
    """

    before = fingerprint(canonical)
    seed_idea(portfolio, project)
    lock = lock_path(f"curator-{target_of(canonical).name}")
    lock.parent.mkdir(parents=True, exist_ok=True)
    holder = subprocess.Popen(
        [
            sys.executable,
            "-c",
            (
                "import fcntl, os, sys\n"
                f"fd = os.open({str(lock)!r}, os.O_CREAT | os.O_RDWR, 0o600)\n"
                "fcntl.flock(fd, fcntl.LOCK_EX)\n"
                "print('held', flush=True)\n"
                "sys.stdin.read()\n"
            ),
        ],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "held"
        busy = _curate(runtime_db, canonical)
        assert not busy.changed
        assert "another writer" in busy.detail
        assert not gitutil.branch_exists(canonical, AUTONOMOUS_BANK_BRANCH)
        assert not target_of(canonical).exists()
    finally:
        holder.communicate(input="", timeout=30)
    assert _curate(runtime_db, canonical).changed
    assert_bank_holds(canonical, portfolio)
    assert fingerprint(canonical) == before


def test_a_pre_repair_checkout_holding_the_branch_does_not_block_the_bank(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """The old layout in *this* repository: a checkout with the branch checked out.

    ``git worktree add <branch>`` refuses a branch in use elsewhere, and did
    so for a checkout whose directory was missing too. The Curator never
    checks the branch out now, so the old checkout holding it is irrelevant
    -- and it is left alone, because removing it is not needed.
    """

    legacy = legacy_checkout(canonical)
    tip = git(canonical, "rev-parse", AUTONOMOUS_BANK_BRANCH).strip()
    portfolio.upsert_state(project_id=PROJECT)
    portfolio.record_bank_write(project_id=PROJECT, commit=tip, digest="the old bank")
    seed_idea(portfolio, project)

    assert _curate(runtime_db, canonical).changed
    assert_bank_holds(canonical, portfolio)
    parents = git(canonical, "rev-list", "--parents", "-n", "1", AUTONOMOUS_BANK_BRANCH)
    assert parents.split()[1:] == [tip]
    assert legacy.resolve() in registered(canonical)


# ------------------------------------------ what cleanup must never delete --
def test_a_symlink_where_the_checkout_belongs_is_refused_and_left_alone(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """Pointed at the researcher's own repository, which is the worst case.

    A cleanup that resolved the link would find the canonical repository
    registered -- as its main worktree -- and a cleanup that deleted
    unconditionally would delete the science.
    """

    before = fingerprint(canonical)
    target = target_of(canonical)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(canonical, target_is_directory=True)
    seed_idea(portfolio, project)

    with pytest.raises(CuratorError, match="symbolic link"):
        _curate(runtime_db, canonical)
    assert target.is_symlink()
    assert (canonical / ".research" / "project.yaml").is_file()
    assert fingerprint(canonical) == before


def test_a_symlink_to_another_worktree_of_the_repository_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
    tmp_path: Path,
) -> None:
    """A link to a coding run's worktree must not be "removed" through the link.

    Resolved, it *is* a registered worktree of this repository with a
    gitlink, and ``git worktree remove`` would take the coding run's checkout
    with it -- which the pre-repair Curator did.
    """

    other = tmp_path / "a-coding-run"
    git(canonical, "worktree", "add", "-q", "-b", "automation/run-x", str(other))
    before = fingerprint(canonical)
    target = target_of(canonical)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.symlink_to(other, target_is_directory=True)
    seed_idea(portfolio, project)

    with pytest.raises(CuratorError, match="symbolic link"):
        _curate(runtime_db, canonical)
    assert (other / ".git").is_file()
    assert other.resolve() in registered(canonical)
    assert fingerprint(canonical) == before


def test_a_repository_of_its_own_where_the_checkout_belongs_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
) -> None:
    """A ``.git`` *directory* is somebody's repository, not a Curator checkout."""

    before = fingerprint(canonical)
    target = target_of(canonical)
    target.mkdir(parents=True)
    git(target, "init", "-q")
    (target / "notes.md").write_text("somebody's work\n", encoding="utf-8")
    seed_idea(portfolio, project)

    with pytest.raises(CuratorError, match="repository of its own"):
        _curate(runtime_db, canonical)
    assert (target / "notes.md").read_text() == "somebody's work\n"
    assert (target / ".git").is_dir()
    assert fingerprint(canonical) == before


def test_another_repositorys_worktree_in_the_curators_directory_is_refused(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
    older_clone: Path,
) -> None:
    """Refused with a sentence, and not removed through its owner.

    The first version of this repair removed it through the repository that
    registered it. An independent review showed that repository's owner may
    be another installation, mid-write in that very checkout, and that the
    removal then committed its bank onto this repository's branch.
    """

    target = target_of(canonical)
    target.parent.mkdir(parents=True, exist_ok=True)
    git(older_clone, "worktree", "add", "-q", "--detach", str(target))
    clone_before = fingerprint(older_clone)
    registration = registered(older_clone)[target.resolve()]
    before = fingerprint(canonical)
    seed_idea(portfolio, project)

    with pytest.raises(CuratorError, match="worktree of"):
        _curate(runtime_db, canonical)
    assert registered(older_clone)[target.resolve()] == registration
    assert fingerprint(older_clone) == clone_before
    assert fingerprint(canonical) == before


def test_cleanup_for_one_project_leaves_every_other_projects_checkout_alone(
    portfolio: PortfolioStore,
    runtime_db: Database,
    project: str,
    canonical: Path,
    tmp_path: Path,
) -> None:
    """The directory next door is another project's bank, and stays."""

    neighbour_repo = make_capsule(tmp_path / "neighbour", project_id="neighbour-study")
    RuntimeStore(runtime_db).upsert_project(
        project_id="neighbour-study", repo_path=str(neighbour_repo)
    )
    seed_idea(portfolio, "neighbour-study")
    curate(db=runtime_db, project_id="neighbour-study", repository=neighbour_repo)
    neighbour = checkout_path("neighbour-study", neighbour_repo)
    neighbour_before = fingerprint(neighbour_repo)

    seed_idea(portfolio, project)
    _curate(runtime_db, canonical)
    shutil.rmtree(target_of(canonical))
    seed_idea(portfolio, project, title="again")
    assert _curate(runtime_db, canonical).changed

    assert (neighbour / ".git").is_file()
    assert neighbour.resolve() in registered(neighbour_repo)
    assert fingerprint(neighbour_repo) == neighbour_before


def test_a_foreign_bank_branch_is_still_refused_after_the_repair(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """The repair records the Curator's own commits; it does not adopt anyone's.

    A branch that exists with no record is still the state an attacker
    arranges, and recovering from the Curator's leftovers must not become a
    way to root the bank on somebody else's commit.
    """

    git(canonical, "branch", AUTONOMOUS_BANK_BRANCH)
    seed_idea(portfolio, project)
    with pytest.raises(UnexpectedBankTipError, match="no record of writing it"):
        _curate(runtime_db, canonical)
    with pytest.raises(UnexpectedBankTipError, match="no record of writing it"):
        _curate(runtime_db, canonical)


def test_the_intent_rule_accepts_only_the_curators_own_unpublished_commit(
    portfolio: PortfolioStore, runtime_db: Database, project: str, canonical: Path
) -> None:
    """A recorded commit whose parent is the tip, and nothing looser.

    A foreign commit *on top of* the Curator's is refused, and so is a
    recorded commit whose parent is some other commit.
    """

    seed_idea(portfolio, project)
    first = _curate(runtime_db, canonical)
    env = ["-c", "user.name=X", "-c", "user.email=x@x.invalid"]
    tree = git(canonical, "rev-parse", f"{first.commit}^{{tree}}").strip()
    foreign = git(canonical, *env, "commit-tree", tree, "-p", first.commit, "-m", "x")
    git(canonical, "update-ref", BANK_REF, foreign.strip(), first.commit)
    seed_idea(portfolio, project, title="after a foreign commit")
    with pytest.raises(UnexpectedBankTipError, match="Something else"):
        _curate(runtime_db, canonical)

    # A watermark naming a commit whose parent is not the tip.
    git(canonical, "update-ref", BANK_REF, first.commit, foreign.strip())
    unrelated = git(canonical, *env, "commit-tree", EMPTY_TREE, "-m", "unrelated")
    portfolio.record_bank_intent(project_id=PROJECT, commit=unrelated.strip())
    with pytest.raises(UnexpectedBankTipError, match="Something else"):
        _curate(runtime_db, canonical)
