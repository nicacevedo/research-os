"""The adversarial containment suite, run rather than described.

`src/research_os/sandbox_audit.py` holds the attacks. This module runs them
through the same function `researchctl runtime containment-audit` runs, so the
suite that gates a release and the suite a researcher can re-run by hand are
one suite and cannot drift apart.

**What was here before was nothing.** The containment record on the development
machine said 27 adversarial checks had passed, `runtime doctor` turned that into
"containment validated", and `available_backend()` gated high-autonomy
execution on it. No code in this repository had ever run those checks: they had
been performed by hand, once, and the conclusion written into a JSON file.

Two properties are asserted here and they are different:

```text
every check HELD      the boundaries this system relies on are real
every check RAN       and none of them was skipped into looking real
```

The second is the one a green suite hides. A check that could not execute is
reported as `SKIP`, and a `SKIP` fails this module.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.sandbox import available_backend, unavailable_reason
from research_os.sandbox_audit import Check, audit, summarise

BACKEND = available_backend()
needs_sandbox = pytest.mark.skipif(
    BACKEND is None,
    reason=f"no containment technology is available here: {unavailable_reason()}",
)


@pytest.fixture(scope="module")
def audit_root() -> Iterator[Path]:
    """A root the sandbox does not shadow, and a canonical repository inside it.

    **Not `tmp_path`.** `_bubblewrap` mounts `--tmpfs /tmp`, so fixtures under
    pytest's temporary root are invisible inside the sandbox for a reason that
    has nothing to do with policy -- the attack cannot be *attempted*, and the
    check passes. `audit()` refuses a `/tmp` root outright for that reason, and
    an audit of this very file found the earlier fixture handing it one.

    The repository built here is what makes the automated gate as strong as the
    hand-run command: without a `--repo`, the suite never attacks the boundary
    the whole system exists to protect, and `record()` refuses to write a
    validation from such a run.
    """

    base = Path("/var/tmp")
    if not base.is_dir() or not os.access(base, os.W_OK):  # pragma: no cover
        pytest.skip("this host has no writable /var/tmp to build fixtures in")
    made = Path(tempfile.mkdtemp(prefix="research-os-adversarial-", dir=base))
    try:
        repo = made / "canonical"
        (repo / ".research").mkdir(parents=True)
        (repo / ".research" / "CLAIM-0001.yaml").write_text(
            "status: accepted\n", encoding="utf-8"
        )
        for args in (
            ["init", "-q", "--initial-branch=main", "."],
            ["config", "user.email", "t@example.invalid"],
            ["config", "user.name", "T"],
        ):
            subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
        subprocess.run(["git", "add", "-A"], cwd=repo, check=True, capture_output=True)
        subprocess.run(
            ["git", "commit", "-qm", "init"], cwd=repo, check=True, capture_output=True
        )
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


@pytest.fixture(scope="module")
def checks(audit_root: Path) -> tuple[Check, ...]:
    """Run the whole suite once; every test below reads the same evidence.

    Once, because several of these attacks take real time -- a fork storm, a
    cancellation with a timeout -- and running them per assertion would make
    the suite slow enough that somebody would be tempted to skip it.
    """

    return audit(audit_root / "run", [audit_root / "canonical"])


@needs_sandbox
def test_every_boundary_this_system_relies_on_held(checks: tuple[Check, ...]) -> None:
    """The headline, with the failures named rather than counted."""

    failed = [check for check in checks if check.held is False]
    assert not failed, "\n".join(
        f"{check.name}: {check.description} -- {check.evidence}" for check in failed
    )


@needs_sandbox
def test_no_check_was_skipped_into_looking_like_a_pass(
    checks: tuple[Check, ...],
) -> None:
    """A suite that half executed is not evidence that anything held.

    Two of these can legitimately not run and both name a missing host tool
    rather than a missing boundary, so they are reported here instead of being
    tolerated silently.
    """

    skipped = [check for check in checks if check.held is None]
    assert not skipped, "these checks did not execute: " + "; ".join(
        f"{check.name} ({check.evidence})" for check in skipped
    )


@needs_sandbox
def test_the_suite_covers_the_boundaries_the_release_gate_names(
    checks: tuple[Check, ...],
) -> None:
    """Coverage as an assertion, so deleting a check fails rather than passes.

    A suite that silently lost its credential checks would still report a
    number, and the number would go down by one in a place nobody reads.
    """

    required = {
        "canonical_repository_0",
        "closure_shebang_outside_policy",
        "closure_symlink_escape",
        "closure_hostile_declared_base",
        "closure_forged_worktree_pointer",
        "closure_console_script_runs",
        "inherited_fd_probe_is_capable",
        "capsule_fixture_unwritable",
        "outside_worktree",
        "symlink_out_of_worktree",
        "symlink_to_home",
        "path_traversal",
        "remount_read_only",
        "writable_set_bounded",
        "worktree_is_writable",
        "ssh_keys",
        "git_credentials",
        "gh_credentials",
        "aws_credentials",
        "netrc",
        "provider_credentials",
        "home_is_disposable",
        "environment_secrets",
        "inherited_file_descriptors",
        "host_pid_isolation",
        "network_denied",
        "loopback_denied",
        "network_is_a_real_capability",
        "nested_user_namespaces",
        "fork_storm_bounded",
        "no_survivors_after_cancellation",
        "host_residue_bounded",
        "overlay_discards_writes",
    }
    found = {check.name for check in checks}
    assert required <= found, required - found


@needs_sandbox
def test_the_control_checks_prove_the_denials_are_denials(
    checks: tuple[Check, ...],
) -> None:
    """Two of these attacks are supposed to *succeed*, and that is the point.

    A sandbox that denied everything -- including writing its own worktree, or
    reaching the network when the network was granted -- would pass every other
    check in this file while being useless. The controls are what stop "nothing
    worked" from reading as "everything held".
    """

    by_name = {check.name: check for check in checks}
    assert by_name["worktree_is_writable"].held is True
    assert by_name["network_is_a_real_capability"].held is True
    assert by_name["inherited_fd_probe_is_capable"].held is True
    assert by_name["closure_console_script_runs"].held is not False


def test_an_audit_root_under_tmp_is_refused(tmp_path: Path) -> None:
    """The sandbox's own tmpfs would hide the fixtures, so the answer is no.

    This is the defect that made three sibling tests vacuous for a release. It
    is refused loudly rather than documented, because a measurement that cannot
    measure should not be able to return a number.
    """

    with pytest.raises(ValueError, match="under /tmp"):
        audit(tmp_path)


def test_a_host_without_containment_reports_that_and_records_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The honest answer where nothing can be attacked, on every host.

    Runs everywhere, including a machine with no bubblewrap, because "we could
    not measure" has to be a distinguishable outcome rather than an empty pass.
    """

    import research_os.sandbox_audit as module

    monkeypatch.setattr(module, "available_backend", lambda: None)
    monkeypatch.setattr(module, "unavailable_reason", lambda: "no mechanism here")

    found = audit(Path("/var/tmp") / "research-os-nonexistent-audit-root")
    assert [check.name for check in found] == ["containment_available"]
    assert found[0].held is None
    assert found[0].status == "SKIP"
    assert "no mechanism here" in found[0].evidence
    assert not module.record(found, executable="/usr/bin/bwrap", version="0.12.0")


def test_a_record_is_refused_unless_every_check_ran_and_held(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The gate on the record itself, which is the claim everything rests on."""

    import research_os.sandbox_audit as module

    written: list[tuple[str, str | None, str]] = []
    monkeypatch.setattr(
        module,
        "record_containment_validation",
        lambda executable, version, *, detail: written.append(
            (executable, version, detail)
        ),
    )

    holding = (
        Check("canonical_repository_0", "the repository held", True, ""),
        Check("b", "b held", True, ""),
    )
    assert module.record(holding, executable="/x", version="0.12.0")
    assert len(written) == 1

    for spoiled in (
        (*holding, Check("c", "c failed", False, "")),
        (*holding, Check("c", "c did not run", None, "")),
    ):
        assert not module.record(spoiled, executable="/x", version="0.12.0")
    assert len(written) == 1, "a spoiled suite still wrote a record"

    # And a sweep that attacked no canonical repository is not a validation,
    # however clean it is: the boundary the record is about was never tried.
    assert not module.record(
        (Check("b", "b held", True, ""),), executable="/x", version="0.12.0"
    )
    assert len(written) == 1


def test_the_summary_names_what_went_wrong_rather_than_only_counting() -> None:
    """A number is not a report. The names are what somebody acts on."""

    mixed = (
        Check("held", "", True, ""),
        Check("broke", "", False, ""),
        Check("absent", "", None, "no tool"),
    )
    summary = summarise(mixed)
    assert "1/3" in summary
    assert "FAILED: broke" in summary
    assert "could not run: absent" in summary
