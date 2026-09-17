"""The runtime's coding action, driven end to end against a real controller.

Everything that tested this handler before substituted a stand-in for
``AutomationController``: one that creates no worktree, writes no branch and
runs no acceptance command. That is the right double for asking "is an escape
classified as ``POLICY_REFUSED``", and it is why nobody noticed that the
handler could not succeed.

``AutomationController`` isolates a coder by creating a Git worktree on a new
branch **in the canonical repository** -- that is what worktree isolation is.
``canonical_fingerprint`` hashed ``git show-ref`` into one opaque
``<git-refs>`` entry and compared it before and after, so every real run ended
with a ref the guard had not seen and was failed as "the coding run changed the
canonical checkout". The branch, the diff and the review were all on disk and
correct. The runtime's only code-writing capability returned
``POLICY_REFUSED`` every time, and an escape-detector that fires on every
honest run is not a detector.

These tests drive the real pipeline. They are the coverage whose absence was
the defect.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.models import RunState
from research_os.automation.store import RunStore
from research_os.runtime.failures import FailureClass
from research_os.runtime.store import RuntimeStore
from tests.automation_helpers import commit_all
from tests.fs_helpers import make_git_repo, write_minimal_capsule
from tests.test_auto_controller import BROKEN_MODULE, TINY_TEST, scripted

PROJECT_ID = "coding-pipeline-project"


@pytest.fixture
def coding_env(
    automation_home: Path,
    runtime_db: Any,
    pg_dsn: str,
    tmp_path: Path,
) -> dict[str, Any]:
    repo = make_git_repo(tmp_path / "project")
    write_minimal_capsule(repo, project_id=PROJECT_ID)
    (repo / "adder.py").write_text(BROKEN_MODULE, encoding="utf-8")
    (repo / "test_adder.py").write_text(TINY_TEST, encoding="utf-8")
    commit_all(repo)

    store = RuntimeStore(runtime_db)
    store.upsert_project(project_id=PROJECT_ID, repo_path=str(repo))
    run = store.create_run(project_id=PROJECT_ID, objective="whether add works")
    return {
        "db": runtime_db,
        "dsn": pg_dsn,
        "repo": repo,
        "artifacts_root": tmp_path / "artifacts",
        "store": store,
        "run": run,
        "state": {
            "run_id": run.run_id,
            "project_id": PROJECT_ID,
            "repo_path": str(repo),
            "objective": "whether add works",
            # `medium`: at `high` the controller requires containment, which
            # this host cannot provide, and the pipeline refuses before it
            # reaches anything this file is about.
            "autonomy": "medium",
            "cycle_index": 0,
            "artifacts": [],
            "notes": [],
        },
    }


def drive(env: dict[str, Any], monkeypatch: pytest.MonkeyPatch, **plan: Any) -> Any:
    """Run the production handler over a real controller with scripted providers."""

    from research_os.automation.controller import AutomationController
    from research_os.runtime.actions import coding
    from research_os.runtime.actions.coding import run_coding_task
    from tests.automation_helpers import fake_config
    from tests.runtime_graph_helpers import make_context, make_router

    def build(_context: Any, *, autonomy: str, authority: Any = None) -> Any:
        # The authority is threaded through exactly as production does, so
        # these runs really reserve and settle against the runtime ledger.
        providers: dict[str, Any] = {"fake": scripted()}
        if authority is not None:
            providers = authority.wrap(providers)
        return AutomationController(providers=providers, config=fake_config())

    monkeypatch.setattr(coding, "_controller", build)
    context = make_context(
        db=env["db"],
        repo=env["repo"],
        artifacts_root=env["artifacts_root"],
        dsn=env["dsn"],
        models=make_router(
            db=env["db"],
            artifacts_root=env["artifacts_root"],
            run_id=env["run"].run_id,
            project_id=PROJECT_ID,
            answers={},
        ),
        permitted=(),
    )
    return run_coding_task(
        env["state"], context, plan or {"rationale": "Implement add."}
    )


def refs(repo: Path) -> dict[str, str]:
    out = subprocess.run(
        ["git", "show-ref"], cwd=repo, capture_output=True, text=True, check=False
    ).stdout
    return {
        line.split(maxsplit=1)[1]: line.split(maxsplit=1)[0]
        for line in out.splitlines()
        if len(line.split(maxsplit=1)) == 2
    }


# -- the run that could never succeed ----------------------------------------


def test_a_real_coding_run_succeeds(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The whole point. Before the fix this returned POLICY_REFUSED, always."""

    outcome = drive(coding_env, monkeypatch)

    assert outcome.ok, outcome.detail
    assert outcome.data["state"] == str(RunState.READY_FOR_HUMAN)
    order = outcome.data["orders"][0]
    assert order["changed_paths"] == ["adder.py"]
    assert order["checks_passed"] is True
    assert order["branch"].startswith("automation/")
    assert outcome.data["reviews"][0]["verdict"] == "PASS"


def test_the_runs_own_branch_is_not_an_escape(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exemption is a namespace, and the run really does use it."""

    from research_os.runtime.actions.coding import (
        owned_ref_prefix,
        reserved_automation_run_id,
    )

    repo = coding_env["repo"]
    before = refs(repo)
    outcome = drive(coding_env, monkeypatch)
    assert outcome.ok, outcome.detail

    after = refs(repo)
    added = sorted(set(after) - set(before))
    assert added, "worktree isolation creates a branch; that is the whole mechanism"

    run_id = outcome.data["automation_run_id"]
    prefix = owned_ref_prefix(run_id)
    assert all(ref.startswith(prefix) for ref in added), added
    assert {ref: sha for ref, sha in after.items() if ref in before} == before, (
        "no pre-existing ref may move"
    )
    # The exemption has to be computable from the reservation alone, because
    # the attempt that adopts a crashed predecessor's branch has nothing else.
    # `reserved_automation_run_id` is a pure function of the key, and the key
    # is a pure function of (run, cycle, base commit, goal).
    from research_os.runtime.idempotency import idempotency_key

    key = idempotency_key(
        "coding.run",
        coding_env["state"]["run_id"],
        coding_env["state"]["cycle_index"],
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip(),
        "Implement add.",
    )
    assert owned_ref_prefix(reserved_automation_run_id(reservation_key=key)) == prefix


def test_the_diff_is_archived_as_an_artifact(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful run's diff reaches the content-addressed store."""

    outcome = drive(coding_env, monkeypatch)
    assert outcome.ok, outcome.detail
    assert [ref.role for ref in outcome.artifacts] == ["diff:T-001"]
    assert all(ref.size_bytes > 0 for ref in outcome.artifacts)


def test_the_canonical_checkout_is_unchanged(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fix must not have turned the guard off."""

    repo = coding_env["repo"]
    before = (repo / "adder.py").read_text(encoding="utf-8")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout

    outcome = drive(coding_env, monkeypatch)
    assert outcome.ok, outcome.detail

    assert (repo / "adder.py").read_text(encoding="utf-8") == before
    assert (
        subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo,
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        == head
    )


# -- the guard still guards ---------------------------------------------------


def test_a_capsule_write_during_the_run_is_still_an_escape(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A per-ref fingerprint must not have weakened the capsule half."""

    from research_os.automation.controller import AutomationController
    from research_os.runtime.actions import coding as coding_module

    repo = coding_env["repo"]
    original = AutomationController.execute

    def escaping(self: Any, store: Any) -> Any:
        forged = repo / ".research" / "reviews"
        forged.mkdir(parents=True, exist_ok=True)
        (forged / "REV-9999.yaml").write_text(
            "id: REV-9999\nreviewer_kind: human\nverdict: approve\n", encoding="utf-8"
        )
        return original(self, store)

    monkeypatch.setattr(AutomationController, "execute", escaping)
    outcome = drive(coding_env, monkeypatch)

    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "REV-9999" in outcome.data["canonical_drift"]
    del coding_module


def test_a_branch_outside_the_runs_namespace_is_still_an_escape(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The exemption is one namespace, not "any new ref".

    This is the case the single-hash version could not have expressed at all:
    it had no way to permit the run's own branch without permitting every
    branch.
    """

    from research_os.automation.controller import AutomationController

    repo = coding_env["repo"]
    original = AutomationController.execute

    def escaping(self: Any, store: Any) -> Any:
        subprocess.run(
            ["git", "branch", "smuggled/elsewhere"],
            cwd=repo,
            check=True,
            capture_output=True,
        )
        return original(self, store)

    monkeypatch.setattr(AutomationController, "execute", escaping)
    outcome = drive(coding_env, monkeypatch)

    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "smuggled/elsewhere" in outcome.data["canonical_drift"]


def test_moving_an_existing_ref_is_still_an_escape(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """A commit on the canonical branch is the escape that matters most."""

    from research_os.automation.controller import AutomationController

    repo = coding_env["repo"]
    original = AutomationController.execute

    def escaping(self: Any, store: Any) -> Any:
        (repo / "smuggled.txt").write_text("x", encoding="utf-8")
        commit_all(repo, "smuggled")
        return original(self, store)

    monkeypatch.setattr(AutomationController, "execute", escaping)
    outcome = drive(coding_env, monkeypatch)

    assert not outcome.ok
    assert outcome.failure_class is FailureClass.POLICY_REFUSED
    assert "refs/heads/" in outcome.data["canonical_drift"]


def test_the_drift_names_the_ref_rather_than_the_word_refs(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """An operator reading a refusal needs to know what moved.

    The opaque ``<git-refs>`` entry told them only that *something* had.
    """

    from research_os.automation.controller import AutomationController

    repo = coding_env["repo"]
    original = AutomationController.execute

    def escaping(self: Any, store: Any) -> Any:
        subprocess.run(
            ["git", "branch", "named/branch"], cwd=repo, check=True, capture_output=True
        )
        return original(self, store)

    monkeypatch.setattr(AutomationController, "execute", escaping)
    outcome = drive(coding_env, monkeypatch)

    drift = outcome.data["canonical_drift"]
    assert "named/branch" in drift
    assert "<git-refs>" not in drift


# -- idempotency over the real pipeline ---------------------------------------


def test_a_second_dispatch_of_the_same_task_reuses_the_first_run(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """One logical coding task, one automation run, however many dispatches.

    The reserved id is derived from the run, the cycle, the base commit and the
    goal -- none of which a retry changes -- so the second dispatch must find
    the first run rather than create a second worktree and a second branch.
    """

    first = drive(coding_env, monkeypatch)
    assert first.ok, first.detail
    branches_after_first = set(refs(coding_env["repo"]))

    second = drive(coding_env, monkeypatch)
    assert second.ok, second.detail
    assert second.data["automation_run_id"] == first.data["automation_run_id"], (
        "a retry must not start a second automation run for one logical task"
    )
    assert set(refs(coding_env["repo"])) == branches_after_first


def test_the_reserved_run_id_is_what_the_pipeline_actually_used(
    coding_env: dict[str, Any], monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reconciler re-derives the id; the run must really carry it."""

    from research_os.runtime.actions.coding import reserved_automation_run_id

    outcome = drive(coding_env, monkeypatch)
    assert outcome.ok, outcome.detail
    run_id = outcome.data["automation_run_id"]
    assert run_id.startswith("RUN-19700101T000000Z-")
    assert RunStore.open(run_id).load().run_id == run_id
    # Derivable from a key alone, with no clock and no store read.
    assert reserved_automation_run_id(reservation_key="k") != run_id
