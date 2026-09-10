"""Tests for the runtime run store: atomic state and an append-only ledger."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_os.automation.models import AutomationRun, RunState, utc_now
from research_os.automation.store import (
    RunStore,
    make_run_id,
    runs_root,
    worktrees_root,
)
from research_os.errors import RunNotFoundError, RunStoreError

COMMIT = "b" * 40


def make_run(**updates: object) -> AutomationRun:
    now = "2026-09-09T10:15:00Z"
    data: dict[str, object] = {
        "run_id": make_run_id(project_path="/tmp/p", goal="g", created_at=now),
        "project_path": "/tmp/p",
        "goal": "g",
        "base_commit": COMMIT,
        "created_at": now,
        "updated_at": now,
    }
    data.update(updates)
    return AutomationRun.model_validate(data)


def test_run_ids_are_deterministic_in_their_inputs() -> None:
    first = make_run_id(project_path="/a", goal="g", created_at="2026-09-09T10:15:00Z")
    again = make_run_id(project_path="/a", goal="g", created_at="2026-09-09T10:15:00Z")
    other = make_run_id(project_path="/a", goal="h", created_at="2026-09-09T10:15:00Z")
    assert first == again
    assert first != other
    assert first.startswith("RUN-20260909T101500Z-")


def test_run_directories_live_under_the_state_home(automation_home: Path) -> None:
    assert runs_root() == automation_home / "runs"
    assert worktrees_root() == automation_home / "worktrees"


def test_create_lays_out_the_run_directory(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    for name in ("context", "prompts", "model_outputs", "checks", "reviews", "plan"):
        assert (store.directory / name).is_dir()
    assert store.run_file.is_file()
    assert store.events_file.is_file()
    assert (store.directory / "worktrees.json").is_file()
    assert store.directory.parent == runs_root()


def test_create_refuses_to_reuse_a_run_directory(automation_home: Path) -> None:
    run = make_run()
    RunStore.create(run)
    with pytest.raises(RunStoreError, match="already exists"):
        RunStore.create(run)


def test_state_round_trips_through_the_store(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    saved = store.save(store.load().model_copy(update={"state": RunState.PLANNING}))
    assert saved.state is RunState.PLANNING
    assert store.load().state is RunState.PLANNING


def test_save_leaves_no_temporary_files_behind(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.save(store.load())
    leftovers = [item.name for item in store.directory.iterdir() if ".tmp" in item.name]
    assert leftovers == []


def test_save_replaces_the_document_whole(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.save(store.load().model_copy(update={"plan_summary": "first"}))
    store.save(store.load().model_copy(update={"plan_summary": "second"}))
    payload = json.loads(store.run_file.read_text(encoding="utf-8"))
    assert payload["plan_summary"] == "second"


def test_events_are_appended_and_never_rewritten(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.append_event("first", detail="a")
    before = store.events_file.read_text(encoding="utf-8")
    store.append_event("second", detail="b")
    after = store.events_file.read_text(encoding="utf-8")
    assert after.startswith(before)
    records = list(store.iter_events())
    assert [item["event"] for item in records] == ["first", "second"]
    assert [item["seq"] for item in records] == [1, 2]
    assert all(item["run_id"] == store.run_id for item in records)


def test_saving_state_does_not_touch_the_ledger(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.append_event("only")
    before = store.events_file.read_text(encoding="utf-8")
    store.save(store.load().model_copy(update={"state": RunState.PREFLIGHTED}))
    assert store.events_file.read_text(encoding="utf-8") == before


def test_open_reports_a_missing_run(automation_home: Path) -> None:
    with pytest.raises(RunNotFoundError, match="no automation run"):
        RunStore.open("RUN-20260909T101500Z-deadbeef")


def test_corrupt_state_is_a_clean_error(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.run_file.write_text("{not json", encoding="utf-8")
    with pytest.raises(RunStoreError, match="cannot parse"):
        store.load()


def test_structurally_invalid_state_is_a_clean_error(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    store.run_file.write_text(json.dumps({"run_id": "nope"}), encoding="utf-8")
    with pytest.raises(RunStoreError, match="invalid run state"):
        store.load()


def test_corrupt_ledger_line_is_a_clean_error(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    with store.events_file.open("a", encoding="utf-8") as handle:
        handle.write("not json\n")
    with pytest.raises(RunStoreError, match="corrupt event ledger"):
        list(store.iter_events())


def test_artifact_paths_cannot_escape_the_run_directory(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    with pytest.raises(RunStoreError, match="escapes the run directory"):
        store.path("..", "elsewhere.txt")


def test_artifacts_are_written_run_relative(automation_home: Path) -> None:
    store = RunStore.create(make_run())
    stored = store.write_text("prompts/INV-0001.txt", "hello")
    assert stored == "prompts/INV-0001.txt"
    assert (store.directory / stored).read_text(encoding="utf-8") == "hello"


def test_list_run_ids_is_chronological(automation_home: Path) -> None:
    first = make_run(
        run_id=make_run_id(
            project_path="/a", goal="g", created_at="2026-09-09T10:15:00Z"
        ),
        created_at="2026-09-09T10:15:00Z",
        updated_at="2026-09-09T10:15:00Z",
    )
    second = make_run(
        run_id=make_run_id(
            project_path="/a", goal="g", created_at="2026-09-10T10:15:00Z"
        ),
        created_at="2026-09-10T10:15:00Z",
        updated_at="2026-09-10T10:15:00Z",
    )
    RunStore.create(second)
    RunStore.create(first)
    assert RunStore.list_run_ids() == (first.run_id, second.run_id)


def test_no_runs_is_an_empty_listing(automation_home: Path) -> None:
    assert RunStore.list_run_ids() == ()


def test_updated_at_advances_on_save(automation_home: Path) -> None:
    store = RunStore.create(make_run(updated_at="2020-01-01T00:00:00Z"))
    saved = store.save(store.load())
    assert saved.updated_at != "2020-01-01T00:00:00Z"
    assert saved.updated_at <= utc_now()


@pytest.mark.parametrize(
    "run_id",
    ["../../etc", "RUN-nope", "", "runs", "RUN-20260909T101500Z-XYZ"],
)
def test_a_malformed_run_id_never_becomes_a_path(
    automation_home: Path, run_id: str
) -> None:
    with pytest.raises(RunNotFoundError, match="not a Research OS run id"):
        RunStore.open(run_id)
