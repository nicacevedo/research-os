"""One writer per run, across processes.

The failure this defends against was real and silent. A research run was
cancelled in one terminal while it was still executing in another: ``cancel``
reported CANCELLED, the executing process finished its next task, wrote the
record again, and the run ended READY_FOR_HUMAN. No error, no log line, and the
researcher's decision simply gone.

The lock is deliberately reentrant within one process -- ``research start
--run`` plans and then executes, and both take it -- so every test of refusal
here uses a genuinely different process, which is the case that actually
happens.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.errors import RunLockedError
from research_os.runlock import lock_path, run_lock

RUN_ID = "RUN-20260912T101500Z-0a1b2c3d"


@pytest.fixture
def research_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    root = tmp_path / "xdg"
    mapping = {
        "RESEARCH_OS_CONFIG_HOME": root / "config",
        "RESEARCH_OS_DATA_HOME": root / "data",
        "RESEARCH_OS_CACHE_HOME": root / "cache",
        "RESEARCH_OS_STATE_HOME": root / "state",
    }
    for name, path in mapping.items():
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    yield mapping["RESEARCH_OS_STATE_HOME"]


def test_the_lock_is_reentrant_within_one_process(automation_home: Path) -> None:
    """``research start --run`` plans and then executes. Both take the lock."""

    with run_lock(RUN_ID, action="research start"):
        with run_lock(RUN_ID, action="research run"):
            assert lock_path(RUN_ID).is_file()
        # The inner block must not have released the outer block's lock.
        assert lock_path(RUN_ID).is_file()
    assert not lock_path(RUN_ID).exists()


def test_two_runs_do_not_block_each_other(automation_home: Path) -> None:
    other = "RUN-20260912T101500Z-ffffffff"
    with (
        run_lock(RUN_ID, action="research run"),
        run_lock(other, action="research run"),
    ):
        assert lock_path(RUN_ID).is_file()
        assert lock_path(other).is_file()


def test_the_lock_is_released_when_the_body_raises(automation_home: Path) -> None:
    with pytest.raises(ValueError), run_lock(RUN_ID, action="research run"):
        raise ValueError("the run failed")
    assert not lock_path(RUN_ID).exists()


def test_a_lock_held_by_a_living_process_is_refused(automation_home: Path) -> None:
    """The cross-process case, which is the one that actually happens."""

    target = lock_path(RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps(
            {
                "run_id": RUN_ID,
                "action": "research run",
                "pid": os.getppid(),
                "created_at": "2026-09-12T10:15:00Z",
            }
        ),
        encoding="utf-8",
    )
    with (
        pytest.raises(RunLockedError) as exit_info,
        run_lock(RUN_ID, action="research cancel"),
    ):
        pass
    message = str(exit_info.value)
    assert "another process" in message
    assert "research run" in message, "the refusal must say what holds it"
    assert str(os.getppid()) in message
    # Refusing must not disturb the owner's lock.
    assert json.loads(target.read_text(encoding="utf-8"))["pid"] == os.getppid()


def test_a_lock_left_by_a_dead_process_is_taken_over(automation_home: Path) -> None:
    """Otherwise a crash makes a run permanently untouchable."""

    target = lock_path(RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"run_id": RUN_ID, "action": "research run", "pid": 2**31 - 1}),
        encoding="utf-8",
    )
    with run_lock(RUN_ID, action="research cancel"):
        owner = json.loads(target.read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["action"] == "research cancel"


def test_an_unreadable_lock_is_treated_as_stale(automation_home: Path) -> None:
    """A truncated lock names no owner, so no owner can be proved alive."""

    target = lock_path(RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with run_lock(RUN_ID, action="research cancel"):
        assert json.loads(target.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_a_cancel_from_another_process_is_refused_while_a_run_executes(
    research_home: Path, tmp_path: Path
) -> None:
    """The reported failure, end to end, with a real second process.

    The parent holds the run's lock, exactly as an executing ``research run``
    would. A separate ``researchctl research cancel`` must fail loudly and leave
    the record alone, rather than succeed and be overwritten moments later.
    """

    from research_os.research.models import ResearchState
    from tests.research_helpers import (
        init_repo,
        make_controller,
        plan_payload,
        scripted,
        task,
    )

    repo = init_repo(tmp_path / "project")
    controller = make_controller(scripted(plan=plan_payload(tasks=[task()])))
    store, run = controller.start(project_path=repo, goal="Read the field.")

    environment = dict(os.environ)
    with run_lock(run.run_id, action="research run"):
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "research_os.cli",
                "research",
                "cancel",
                run.run_id,
                "--reason",
                "stop now",
            ],
            capture_output=True,
            check=False,
            text=True,
            env=environment,
            cwd=str(tmp_path),
        )

    assert result.returncode != 0, result.stdout
    assert "another process" in result.stderr
    assert store.load().state is ResearchState.PLAN_READY


def test_a_cancel_succeeds_once_the_run_is_no_longer_held(
    research_home: Path, tmp_path: Path
) -> None:
    """The refusal must be about contention, not a permanent block."""

    from research_os.research.models import ResearchState
    from tests.research_helpers import (
        init_repo,
        make_controller,
        plan_payload,
        scripted,
        task,
    )

    repo = init_repo(tmp_path / "project")
    controller = make_controller(scripted(plan=plan_payload(tasks=[task()])))
    store, run = controller.start(project_path=repo, goal="Read the field.")

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "research_os.cli",
            "research",
            "cancel",
            run.run_id,
            "--reason",
            "changed my mind",
        ],
        capture_output=True,
        check=False,
        text=True,
        env=dict(os.environ),
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert store.load().state is ResearchState.CANCELLED


def test_only_one_of_many_claimants_takes_over_a_stale_lock(
    automation_home: Path,
) -> None:
    """Found by an independent reviewer: the obvious takeover is racy.

    Unlink-then-create lets two processes both unlink and both create, with the
    second unlink removing the first's *fresh* lock -- two live holders, which
    is the silent double-writer this module exists to prevent. Real processes,
    started together, all racing one stale lock.
    """

    target = lock_path(RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(
        json.dumps({"run_id": RUN_ID, "action": "research run", "pid": 2**31 - 1}),
        encoding="utf-8",
    )

    claimant = (
        "import os, sys, time\n"
        "from research_os.runlock import run_lock\n"
        "from research_os.errors import RunLockedError\n"
        "try:\n"
        "    with run_lock(sys.argv[1], action='claim'):\n"
        "        print('HELD', os.getpid(), flush=True)\n"
        "        time.sleep(1.5)\n"
        "except RunLockedError:\n"
        "    print('REFUSED', flush=True)\n"
    )
    processes = [
        subprocess.Popen(
            [sys.executable, "-c", claimant, RUN_ID],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=dict(os.environ),
        )
        for _ in range(6)
    ]
    outputs = [process.communicate(timeout=60)[0] for process in processes]

    held = [item for item in outputs if "HELD" in item]
    assert len(held) == 1, f"{len(held)} processes believed they held the lock"
