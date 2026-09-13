"""One writer per run, across processes.

The failure this defends against was real and silent. A research run was
cancelled in one terminal while it was still executing in another: ``cancel``
reported CANCELLED, the executing process finished its next task, wrote the
record again, and the run ended READY_FOR_HUMAN. No error, no log line, and the
researcher's decision simply gone.

Every test of contention here uses a process that *genuinely holds the lock*,
not a file with a pid written into it. An earlier version of this file did the
latter, and an independent reviewer pointed out what that costs: it passed
against an implementation with a demonstrable two-holder race, because staggered
interpreter startup never lands inside the window. A test of a race that cannot
observe the race is worse than no test, because it is reported as coverage.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.errors import RunLockedError
from research_os.runlock import lock_path, run_lock

RUN_ID = "RUN-20260912T101500Z-0a1b2c3d"

#: Take the lock, announce it, and hold until killed or told to stop.
HOLDER = """
import sys, time
from research_os.runlock import run_lock
with run_lock(sys.argv[1], action="holding for a test"):
    print("HELD", flush=True)
    time.sleep(float(sys.argv[2]))
"""

#: Contend for the lock at a shared wall-clock instant, then report the outcome.
RACER = """
import sys, time
from research_os.runlock import run_lock
from research_os.errors import RunLockedError

deadline = float(sys.argv[2])
while time.time() < deadline:
    pass
try:
    with run_lock(sys.argv[1], action="racing") as path:
        print("HELD", flush=True)
        time.sleep(0.4)
except RunLockedError:
    print("REFUSED", flush=True)
"""


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


def spawn(script: str, *args: str) -> subprocess.Popen[str]:
    return subprocess.Popen(
        [sys.executable, "-c", script, *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=dict(os.environ),
    )


def await_held(process: subprocess.Popen[str]) -> None:
    """Block until the child reports it holds the lock."""

    assert process.stdout is not None
    line = process.stdout.readline()
    assert line.strip() == "HELD", f"child never took the lock: {line!r}"


# -- the shape of the lock ---------------------------------------------------


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


# -- contention, with a process that really holds it -------------------------


def test_a_lock_another_process_holds_is_refused(automation_home: Path) -> None:
    holder = spawn(HOLDER, RUN_ID, "30")
    try:
        await_held(holder)
        with (
            pytest.raises(RunLockedError) as exit_info,
            run_lock(RUN_ID, action="research cancel"),
        ):
            pass
        message = str(exit_info.value)
        assert "another process" in message
        assert "holding for a test" in message, "the refusal must say what holds it"
        assert str(holder.pid) in message
    finally:
        holder.kill()
        holder.wait(timeout=30)


def test_a_lock_is_free_once_its_holder_is_killed(automation_home: Path) -> None:
    """SIGKILL runs no cleanup code. The kernel has to be the one releasing it.

    This is why the lock is a ``flock`` and not a file this module manages: a
    lock whose release depends on the holder's own code is a lock that outlives
    a crash, and every scheme for detecting that had a race in it.
    """

    holder = spawn(HOLDER, RUN_ID, "60")
    await_held(holder)
    holder.send_signal(signal.SIGKILL)
    holder.wait(timeout=30)

    with run_lock(RUN_ID, action="research cancel"):
        owner = json.loads(lock_path(RUN_ID).read_text(encoding="utf-8"))
        assert owner["pid"] == os.getpid()
        assert owner["action"] == "research cancel"


def test_a_stray_file_that_is_not_a_lock_does_not_block_a_run(
    automation_home: Path,
) -> None:
    """Leftover bytes are not a lock. Only the kernel's answer is."""

    target = lock_path(RUN_ID)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("{not json", encoding="utf-8")
    with run_lock(RUN_ID, action="research cancel"):
        assert json.loads(target.read_text(encoding="utf-8"))["pid"] == os.getpid()


def test_exactly_one_of_many_racers_takes_a_free_lock(automation_home: Path) -> None:
    """Five processes released at one wall-clock instant; one must win.

    Synchronised on a shared deadline rather than on process start, so they
    contend at the same moment instead of being separated by however long an
    interpreter takes to boot. That separation is what made the previous version
    of this test pass against provably racy code.
    """

    lock_path(RUN_ID).parent.mkdir(parents=True, exist_ok=True)
    deadline = time.time() + 2.0
    racers = [spawn(RACER, RUN_ID, str(deadline)) for _ in range(5)]
    outcomes = [process.communicate(timeout=90)[0] for process in racers]

    held = [item for item in outcomes if "HELD" in item]
    refused = [item for item in outcomes if "REFUSED" in item]
    assert len(held) == 1, f"{len(held)} processes believed they held the lock"
    assert len(held) + len(refused) == 5


def test_many_racers_are_all_refused_while_one_holder_keeps_it(
    automation_home: Path,
) -> None:
    """The complement: a lock that is held stays held, for everyone."""

    holder = spawn(HOLDER, RUN_ID, "30")
    try:
        await_held(holder)
        deadline = time.time() + 1.5
        racers = [spawn(RACER, RUN_ID, str(deadline)) for _ in range(4)]
        outcomes = [process.communicate(timeout=90)[0] for process in racers]
        assert all("REFUSED" in item for item in outcomes), outcomes
    finally:
        holder.kill()
        holder.wait(timeout=30)


# -- the reported failure, end to end ----------------------------------------


def test_a_cancel_from_another_process_is_refused_while_a_run_executes(
    research_home: Path, tmp_path: Path
) -> None:
    """The reported failure, with a real second process running the real CLI."""

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
            env=dict(os.environ),
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
