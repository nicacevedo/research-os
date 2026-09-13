"""What a process killed mid-side-effect leaves behind, injected point by point.

Two irreversible side effects in Research OS happen *before* the record that
owns them used to exist: a research run creating an inner automation run, and an
experiment creating an isolated Git worktree in the researcher's repository. Both
were reported at the end of v1 as windows where a crash leaves an artifact
nothing in the store can name -- an orphan invisible to ``experiment cleanup``
and to ``storage --reclaim`` alike, because both start from the store.

These tests do not manufacture the final state and assert it is recoverable.
They inject a failure at each boundary in turn and then ask the ordinary
commands what they can see, because the interesting question is not whether an
orphan can be cleaned up once found, it is whether it can be found at all.

For every injection point exactly one of three things must hold:

* no artifact exists;
* the artifact is reachable through normal state; or
* ``storage --reclaim`` deterministically identifies and removes it.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from research_os import diagnostics
from research_os.automation import worktree as worktree_module
from research_os.automation.worktree import lock_path as worktree_lock_path
from research_os.errors import WorktreeError
from research_os.experiment.controller import ExperimentController
from research_os.experiment.models import ExecutionState
from research_os.experiment.store import ExperimentStore
from tests.test_experiment_execution import (
    PROJECT_ID,
    SUCCESS_SCRIPT,
    config,
    git_project,
)


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "xdg"
    for name, subdirectory in (
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
        ("RESEARCH_OS_STATE_HOME", "state"),
    ):
        path = root / subdirectory
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return root / "state"


class Killed(RuntimeError):
    """Stands in for the process dying at one exact point."""


@pytest.fixture
def research_state(research_home: Path) -> Path:
    """The same relocation, named for the half of this file that orchestrates."""

    return research_home


# -- experiment worktree / store registration ---------------------------------


def _prepared(tmp_path: Path) -> tuple[ExperimentController, Path]:
    project = git_project(tmp_path / "widget", script=SUCCESS_SCRIPT)
    return ExperimentController(config=config()), project


def test_a_crash_before_worktree_creation_leaves_a_record_and_no_worktree(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection point one. The record exists first, which is the whole fix."""

    controller, project = _prepared(tmp_path)
    monkeypatch.setattr(
        "research_os.experiment.controller.create_worktree",
        lambda **_: (_ for _ in ()).throw(Killed("killed before creation")),
    )
    with pytest.raises(Killed):
        controller.run(
            project_path=project,
            task_name="fit-model",
            parameters={"seed": 7},
            project_id=PROJECT_ID,
            execute=True,
        )

    run_ids = ExperimentStore.list_run_ids()
    assert len(run_ids) == 1, "the run must be on disk before its worktree can be"
    run = ExperimentStore.open(run_ids[0]).load()
    assert run.state is ExecutionState.FAILED
    assert "worktree creation failed" in (run.failure_reason or "")
    assert not Path(run.worktree_path or "").exists()
    # No phantom active experiment: the record says failed, not running.
    assert run.state not in {ExecutionState.RUNNING, ExecutionState.PREPARED}


def test_a_crash_between_the_lock_and_the_worktree_is_reclaimable(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection point two: the lock is taken, the worktree never appears.

    The state that used to be permanent. Nothing was on disk except a lock file
    named after a digest, and because the path it guards could never be created
    again, the experiment was blocked forever by an owner that had died.
    """

    controller, project = _prepared(tmp_path)
    taken: list[Path] = []

    def die_after_lock(**kwargs: object) -> None:
        taken.append(Path(str(kwargs["target"])))
        raise Killed("killed after the lock, before git")

    # A scoped patch, never ``monkeypatch.undo()``: undo would also revert the
    # ``research_home`` fixture's environment and point the next assertion at
    # the researcher's real state directory.
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(worktree_module, "add_worktree", die_after_lock)
        with pytest.raises(Killed):
            controller.run(
                project_path=project,
                task_name="fit-model",
                parameters={"seed": 7},
                project_id=PROJECT_ID,
                execute=True,
            )

    target = taken[0]
    assert not target.exists()
    # create_worktree's own except-clause released the lock on the way out.
    assert not worktree_lock_path(target).exists()

    run = ExperimentStore.open(ExperimentStore.list_run_ids()[0]).load()
    assert run.state is ExecutionState.FAILED
    # And the path is usable again rather than permanently owned by a dead run.
    second, _run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    assert second.load().state is ExecutionState.COMPLETED


def test_a_lock_left_by_a_killed_process_is_found_and_released(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection point two, with no unwinding at all -- the SIGKILL shape.

    ``create_worktree`` releases the lock in an ``except``. A killed process
    runs no ``except``, so the honest injection leaves the lock in place and
    asks whether anything can still find it.
    """

    controller, project = _prepared(tmp_path)

    def die_hard(**kwargs: object) -> None:
        raise Killed("killed with the lock held")

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(worktree_module, "add_worktree", die_hard)
        # Suppress the unwinding the real kill would not have performed.
        injected.setattr(
            worktree_module, "release_worktree_lock", lambda _target: False
        )
        with pytest.raises(Killed):
            controller.run(
                project_path=project,
                task_name="fit-model",
                parameters={"seed": 7},
                project_id=PROJECT_ID,
                execute=True,
            )

    run = ExperimentStore.open(ExperimentStore.list_run_ids()[0]).load()
    target = Path(run.worktree_path or "")
    assert not target.exists(), "no worktree was ever created"
    assert worktree_lock_path(target).exists(), "but its lock outlived the process"

    # The report must see it, and reclaim must remove it.
    usage = {item.name: item for item in diagnostics.storage_usage()}
    assert "worktrees" in usage
    result = diagnostics.reclaim()
    assert run.run_id in result.run_ids
    assert not worktree_lock_path(target).exists()


def test_a_crash_after_worktree_creation_is_found_by_reclaim(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection point three: the worktree exists, execution never happened.

    Before the fix this was the orphan proper -- a directory, a lock and a Git
    registration in the researcher's own repository, with no store record naming
    any of them.
    """

    controller, project = _prepared(tmp_path)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            ExperimentController,
            "_run_locally",
            lambda *_a, **_k: (_ for _ in ()).throw(Killed("killed after creation")),
        )
        with pytest.raises(Killed):
            controller.run(
                project_path=project,
                task_name="fit-model",
                parameters={"seed": 7},
                project_id=PROJECT_ID,
                execute=True,
            )

    run = ExperimentStore.open(ExperimentStore.list_run_ids()[0]).load()
    target = Path(run.worktree_path or "")
    assert target.is_dir(), "the worktree really was created"
    assert run.state is ExecutionState.PREPARED

    registered = subprocess.run(
        ["git", "worktree", "list"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(target) in registered

    usage = {item.name: item for item in diagnostics.storage_usage()}
    assert usage["worktrees"].reclaimable > 0
    result = diagnostics.reclaim()
    assert run.run_id in result.run_ids
    assert not target.exists()
    assert not worktree_lock_path(target).exists()
    still = subprocess.run(
        ["git", "worktree", "list"],
        cwd=project,
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert str(target) not in still, "the Git registration went too"


def test_a_crash_during_cleanup_leaves_the_run_reclaimable_again(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Injection point five: cleanup itself dies. Reclaim must be repeatable."""

    controller, project = _prepared(tmp_path)
    _store, run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    target = Path(run.worktree_path or "")
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            "research_os.experiment.controller.release_worktree",
            lambda *_a, **_k: (_ for _ in ()).throw(Killed("killed mid cleanup")),
        )
        with pytest.raises(Killed):
            controller.cleanup(ExperimentStore.open(run.run_id))

    assert target.is_dir(), "nothing was released"
    result = diagnostics.reclaim()
    assert run.run_id in result.run_ids
    assert not target.exists()


def test_reclaim_never_removes_a_run_lock(research_home: Path, tmp_path: Path) -> None:
    """The one file in Research OS that must survive every cleanup path.

    Worktree locks and run locks share a directory and have opposite lifecycles.
    A ``flock`` is held on an inode, so unlinking a run lock's *name* while it is
    held lets the next arrival create and lock a different inode at the same
    path -- two writers, one run. That race was measured for real, and every
    recovery path added since has to be checked against it.
    """

    from research_os.runlock import lock_path as run_lock_path
    from research_os.runlock import run_lock

    controller, project = _prepared(tmp_path)
    _store, _run, _packet = controller.run(
        project_path=project,
        task_name="fit-model",
        parameters={"seed": 7},
        project_id=PROJECT_ID,
        execute=True,
    )
    with run_lock("RUN-20260101T000000Z-abcdef01", action="a bystander"):
        pass
    bystander = run_lock_path("RUN-20260101T000000Z-abcdef01")
    assert bystander.is_file()
    before = bystander.stat().st_ino

    diagnostics.reclaim()

    assert bystander.is_file(), "reclaim deleted a run lock"
    assert bystander.stat().st_ino == before, "reclaim replaced a run lock's inode"


def test_a_worktree_lock_release_refuses_a_path_that_is_not_a_worktree_lock(
    research_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The derivation is the guard, so prove the guard is actually load-bearing."""

    from research_os.automation.worktree import release_worktree_lock

    monkeypatch.setattr(
        worktree_module, "lock_path", lambda _t: Path("/tmp/run-RUN-x.lock")
    )
    with pytest.raises(WorktreeError, match="not a worktree lock"):
        release_worktree_lock(Path("/anything"))


# -- outer run / inner run dispatch -------------------------------------------


def _research(tmp_path: Path):
    """A research run whose single task is delegated to an automation run."""

    from tests.test_research import analysis_task, plan_payload, start

    return start(tmp_path, plan=plan_payload(tasks=[analysis_task()]))


def _reserved(store) -> tuple[str, ...]:
    from research_os.research.controller import ResearchController

    return tuple(
        item.automation_run_id for item in ResearchController.reserved_dispatches(store)
    )


def test_a_crash_before_reservation_leaves_nothing_at_all(
    research_state: Path, tmp_path: Path
) -> None:
    """Injection point one. Nothing was reserved, so nothing can be orphaned."""

    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            ResearchController,
            "_delegated_attempts",
            staticmethod(
                lambda *_a, **_k: (_ for _ in ()).throw(Killed("killed before"))
            ),
        )
        with pytest.raises(Killed):
            controller.execute(store)

    assert _reserved(store) == ()
    assert ExperimentStore.list_run_ids() == ()
    from research_os.automation.store import RunStore

    assert RunStore.list_run_ids() == (), "no inner run directory was created"


def test_a_crash_after_reservation_but_before_creation_names_a_run_that_is_absent(
    research_state: Path, tmp_path: Path
) -> None:
    """Injection point two.

    The reservation is on disk and the directory is not. The recorded answer has
    to distinguish those, because "reserved and missing" needs no recovery while
    "reserved and present" does, and guessing between them is how an orphan gets
    left behind.
    """

    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore
    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            AutomationController,
            "start",
            lambda *_a, **_k: (_ for _ in ()).throw(Killed("killed inside start")),
        )
        with pytest.raises(Killed):
            controller.execute(store)

    reserved = ResearchController.reserved_dispatches(store)
    assert len(reserved) == 1, "the parent must name what it was about to start"
    assert reserved[0].automation_run_id.startswith("RUN-")
    assert reserved[0].task_id == "T-001"
    assert reserved[0].present is False
    assert RunStore.list_run_ids() == ()


def test_a_crash_after_the_inner_directory_exists_leaves_it_reachable(
    research_state: Path, tmp_path: Path
) -> None:
    """Injection point three, the orphan proper.

    The inner run directory exists and the parent has not yet written the
    ``automation_run_started`` event. Before the reservation this was the
    unreachable state: the id was known only to the dead process, and the id is
    a digest over a timestamp nobody recorded, so it could not be recomputed.
    """

    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore, runs_root
    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    real_start = AutomationController.start

    def start_then_die(self, **kwargs):
        real_start(self, **kwargs)
        raise Killed("killed after start returned, before the parent recorded it")

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(AutomationController, "start", start_then_die)
        with pytest.raises(Killed):
            controller.execute(store)

    created = RunStore.list_run_ids()
    assert len(created) == 1, "an inner run directory really was created"

    reserved = ResearchController.reserved_dispatches(store)
    assert len(reserved) == 1
    assert reserved[0].present is True
    # The decisive property: the parent's ledger names the directory on disk.
    assert reserved[0].automation_run_id == created[0]
    assert (runs_root() / created[0]).is_dir()
    assert reserved[0].state, "and its state can be read back"


def test_an_interrupted_dispatch_is_accounted_for_by_the_parent(
    research_state: Path, tmp_path: Path
) -> None:
    """Injection point four, the ordinary shape: an exception unwinds normally.

    ``_delegate_to_automation`` accounts for the inner run in a ``finally``, so
    an interruption that still unwinds leaves nothing reserved-and-unaccounted.
    Asserted explicitly, because a discovery routine that reported this as an
    orphan would send a researcher looking for a problem that is not there.
    """

    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore
    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(
            AutomationController,
            "execute",
            lambda *_a, **_k: (_ for _ in ()).throw(Killed("killed before returning")),
        )
        with pytest.raises(Killed):
            controller.execute(store)

    created = RunStore.list_run_ids()
    assert len(created) == 1
    assert ResearchController.reserved_dispatches(store) == ()
    assert ResearchController.delegated_run_ids(store, "T-001") == created


def test_a_kill_that_runs_no_cleanup_still_leaves_the_inner_run_named(
    research_state: Path, tmp_path: Path
) -> None:
    """Injection point four, the SIGKILL shape: no ``finally`` runs at all.

    The distinction matters because the ``finally`` is what makes the ordinary
    case safe, and a killed process runs none. Modelled by dropping the finish
    event the dying process would never have written. What remains is only the
    reservation -- which is precisely why it has to carry the id.
    """

    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore, runs_root
    from research_os.research.controller import ResearchController
    from research_os.research.store import ResearchStore

    controller, store, _run, _repo = _research(tmp_path)
    real_append = ResearchStore.append_event

    def append_unless_finishing(self, event: str, **fields: object) -> None:
        if event in {"automation_run_started", "automation_run_finished"}:
            raise Killed("killed before the parent could record the dispatch")
        real_append(self, event, **fields)

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(ResearchStore, "append_event", append_unless_finishing)
        injected.setattr(
            AutomationController,
            "execute",
            lambda *_a, **_k: (_ for _ in ()).throw(Killed("never reached")),
        )
        with pytest.raises(Killed):
            controller.execute(store)

    created = RunStore.list_run_ids()
    assert len(created) == 1, "the inner run directory outlived the process"
    reserved = ResearchController.reserved_dispatches(store)
    assert [item.automation_run_id for item in reserved] == list(created)
    assert reserved[0].present is True
    assert (runs_root() / created[0]).is_dir()


def test_a_dispatch_that_finished_is_not_reported_as_reserved(
    research_state: Path, tmp_path: Path
) -> None:
    """The other half of the property: no false positives on the ordinary path."""

    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    run = controller.execute(store)

    assert run.task("T-001").status.value == "done"
    assert ResearchController.reserved_dispatches(store) == ()


def test_spend_by_a_dispatch_that_never_reported_is_still_charged(
    research_state: Path, tmp_path: Path
) -> None:
    """A run killed inside ``start`` can still have spent model calls.

    Accounting that reads only the ``started`` event cannot see them, which is a
    budget that counts successes. Reading the reservation instead is what makes
    the spend recoverable from disk.
    """

    from research_os.automation.controller import AutomationController
    from research_os.automation.store import RunStore
    from research_os.research.controller import ResearchController

    controller, store, _run, _repo = _research(tmp_path)
    real_start = AutomationController.start

    def start_then_die(self, **kwargs):
        real_start(self, **kwargs)
        raise Killed("killed after start returned")

    with pytest.MonkeyPatch.context() as injected:
        injected.setattr(AutomationController, "start", start_then_die)
        with pytest.raises(Killed):
            controller.execute(store)

    inner_id = RunStore.list_run_ids()[0]
    inner_store = RunStore.open(inner_id)
    inner = inner_store.load()
    inner_store.save(inner.model_copy(update={"model_calls_used": 3}))

    assert ResearchController.delegated_run_ids(store, "T-001") == (inner_id,)
    assert ResearchController._delegated_total(store, "T-001") == 3


def test_a_reserved_id_that_is_not_a_run_id_is_refused(
    research_state: Path, tmp_path: Path
) -> None:
    """A reservation is an identity, so it still has to be one this store can create."""

    from research_os.automation.config import load_config
    from research_os.automation.controller import AutomationController
    from research_os.errors import PreflightError

    _controller, _store, _run, repo = _research(tmp_path)
    inner = AutomationController(providers={}, config=load_config(None))
    with pytest.raises(PreflightError, match="is not a run id"):
        inner.start(
            project_path=repo,
            goal="anything",
            reserved_run_id="../../etc/passwd",
        )


def test_the_derived_worktree_id_is_a_pure_function_of_the_run_id() -> None:
    """It is written down before it is used, so it may not read the clock.

    It did. Two calls a second apart returned different ids, which was invisible
    while the only caller was the one creating the worktree, and became a
    recorded path that pointed at nothing the moment the record was written
    first. The defensive equality check in ``run`` is what caught it.
    """

    from research_os.experiment.controller import _worktree_run_id

    run_id = "XRUN-20260913T193649Z-50aa2017"
    first = _worktree_run_id(run_id)
    assert first == _worktree_run_id(run_id)
    assert first.startswith("RUN-20260913T193649Z-")

    from research_os.automation.models import RUN_ID_RE

    assert RUN_ID_RE.fullmatch(first), "the shared helpers validate this shape"
    assert _worktree_run_id("XRUN-20260913T193649Z-ffffffff") != first
