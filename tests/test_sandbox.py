"""OS-level containment: what it denies, and what it refuses to pretend.

Two kinds of test here, and the split is the point.

**Deterministic tests** exercise the policy, the argv construction and the
probe's honesty. They need no containment technology and therefore run
everywhere, which matters because the policy is the part that decides whether
model-written code executes at all.

**Adversarial tests** actually try to escape, and they can only run where a
technology is available. They skip with a reason otherwise -- and the reason is
itself the release-blocking evidence, not a gap in coverage. A test that
*claimed* to prove containment on a host with none would be worse than no test.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from research_os.automation.models import AcceptanceCommand
from research_os.sandbox import (
    NamespaceState,
    SandboxError,
    SandboxMode,
    SandboxSpec,
    available_backend,
    contain,
    probe,
    unavailable_reason,
)

BACKEND = available_backend()
needs_sandbox = pytest.mark.skipif(
    BACKEND is None,
    reason=f"no containment technology is available here: {unavailable_reason()}",
)


# ------------------------------------------------------------------- probe --
def test_every_technology_is_probed_and_reports_a_reason() -> None:
    """An unexplained "unavailable" is not a measurement anybody can act on."""

    results = probe()
    assert results, "nothing was probed"
    assert {item.technology for item in results} >= {"bubblewrap", "systemd-run"}
    for item in results:
        assert item.detail, f"{item.technology} reported no reason"


def test_bubblewrap_namespaces_are_probed_by_running_it_not_by_finding_it() -> None:
    """The failure this catches is invisible to ``which``.

    A present ``bwrap`` on a host whose kernel refuses unprivileged user
    namespaces is a binary that cannot isolate a single path -- which is the
    configuration Ubuntu 24.04 ships. Reporting it as available because the
    file exists is how a deployment ends up believing it is sandboxed.

    Asserts ``namespaces_ok`` rather than ``available``: since the security
    floor exists, ``available`` also depends on the version, and a probe that
    ran perfectly is no longer by itself a probe that may be used.
    """

    found = next(item for item in probe() if item.technology == "bubblewrap")
    import shutil

    from research_os.sandbox import _namespace_probe_argv

    if shutil.which("bwrap") is None:
        assert found.namespaces_ok is False
        assert found.available is False
        return
    # The binary is here. Whether it works is a fact about the kernel, and the
    # probe's answer must match what actually happens when *its own* invocation
    # is run.
    #
    # This used to run a hand-written `--ro-bind /usr /usr -- /bin/true`, which
    # on a usrmerged host has no `/lib64` inside the sandbox and so cannot
    # start any dynamically linked program. It agreed with the probe only
    # because the probe had the same defect; once the probe was fixed, the
    # hand-written command was the one still failing.
    completed = subprocess.run(
        _namespace_probe_argv(shutil.which("bwrap") or "bwrap"),
        check=False,
        capture_output=True,
        stdin=subprocess.DEVNULL,
        timeout=20,
    )
    assert found.namespaces_ok is (completed.returncode == 0)


def test_systemd_run_is_never_selected_even_when_present() -> None:
    """Its user-scope directives start the unit and do not bind.

    ``systemd-run --user -P -p ProtectHome=tmpfs -p PrivateNetwork=yes`` runs,
    reports success, and the process sees the real home and the real network.
    A containment that reports success without containing is worse than none,
    so it is probed, recorded, and never used.
    """

    found = next(item for item in probe() if item.technology == "systemd-run")
    assert found.available is False
    assert "ineffective" in found.detail


# ------------------------------------------------------------------ policy --
def test_required_refuses_rather_than_running_uncontained(tmp_path: Path) -> None:
    """The whole meaning of "required"."""

    spec = SandboxSpec(workdir=tmp_path)
    if BACKEND is None:
        with pytest.raises(SandboxError) as raised:
            contain(["/bin/true"], spec=spec, mode=SandboxMode.REQUIRED)
        message = str(raised.value)
        assert "requires OS-level containment" in message
        # It has to say what to do. A refusal that names no remedy is a wall.
        assert "sandbox.mode: preferred" in message
    else:
        prepared = contain(["/bin/true"], spec=spec, mode=SandboxMode.REQUIRED)
        assert prepared.contained is True


def test_preferred_runs_uncontained_and_says_so(tmp_path: Path) -> None:
    """Recorded, not silent.

    "The tests passed" and "the tests passed inside a sandbox" are different
    facts about a run, and the difference is what a reader needs in order to
    judge the exposure.
    """

    prepared = contain(
        ["/bin/true"], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.PREFERRED
    )
    assert prepared.contained is (BACKEND is not None)
    assert prepared.detail
    if BACKEND is None:
        assert prepared.argv == ("/bin/true",)
        assert prepared.technology == "none"


def test_off_does_not_even_probe(tmp_path: Path) -> None:
    prepared = contain(
        ["/bin/true"], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.OFF
    )
    assert prepared.contained is False
    assert prepared.argv == ("/bin/true",)
    assert "switched off by configuration" in prepared.detail


def test_high_autonomy_requires_containment(tmp_path: Path) -> None:
    """The one place the researcher's configured mode does not decide.

    Asserted against the two code paths that run code this system did not
    write, so that lowering the default in ``automation.yaml`` cannot quietly
    re-enable unattended uncontained execution.
    """

    from research_os.runtime.actions.coding import _controller
    from research_os.runtime.config import (
        BudgetDefaults,
        RuntimeConfig,
        RuntimeSettings,
    )
    from research_os.runtime.executors import LocalExecutor, build_executors

    config = RuntimeConfig(
        dsn="",
        artifacts_root=tmp_path / "artifacts",
        settings=RuntimeSettings(),
        budget=BudgetDefaults(),
        autonomy="high",
    )
    executors = build_executors(config, project_id=None, autonomy="high")
    local = executors["local"]
    assert isinstance(local, LocalExecutor)
    assert local.sandbox_mode is SandboxMode.REQUIRED

    lower = build_executors(config, project_id=None, autonomy="medium")
    assert lower["local"].sandbox_mode is not SandboxMode.REQUIRED  # type: ignore[union-attr]

    controller = _controller(None, autonomy="high")  # type: ignore[arg-type]
    assert controller.sandbox_mode is SandboxMode.REQUIRED
    assert _controller(None, autonomy="medium").sandbox_mode is None  # type: ignore[arg-type]


# ----------------------------------------------------- the argv it builds ----
@needs_sandbox
def test_the_environment_is_an_allowlist_not_an_inheritance(tmp_path: Path) -> None:
    """A variable nobody thought about must be absent, not present."""

    prepared = contain(
        ["/bin/true"],
        spec=SandboxSpec(workdir=tmp_path, environment={"EXPERIMENT_SEED": "1"}),
        mode=SandboxMode.REQUIRED,
    )
    argv = list(prepared.argv)
    assert "--clearenv" in argv
    setenv = {
        argv[index + 1]: argv[index + 2]
        for index, token in enumerate(argv)
        if token == "--setenv"
    }
    assert setenv["EXPERIMENT_SEED"] == "1"
    assert setenv["HOME"].startswith("/tmp/")
    assert (
        setenv["PATH"] == "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"
    )
    for forbidden in ("SSH_AUTH_SOCK", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        assert forbidden not in setenv


@needs_sandbox
def test_the_network_is_a_capability_and_not_a_default(tmp_path: Path) -> None:
    denied = contain(
        ["/bin/true"], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
    )
    granted = contain(
        ["/bin/true"],
        spec=SandboxSpec(workdir=tmp_path, network=True),
        mode=SandboxMode.REQUIRED,
    )
    assert "--unshare-all" in denied.argv
    assert "--share-net" not in denied.argv
    assert "--share-net" in granted.argv


@needs_sandbox
def test_writable_binds_come_after_the_read_only_operating_system(
    tmp_path: Path,
) -> None:
    """Later binds win, so the order is a correctness property.

    A writable path that happens to sit under a read-only bind would otherwise
    be shadowed by it, and the command would fail for a reason no one could
    explain.
    """

    # It has to exist: a bind mount needs a source, and the sandbox refuses to
    # create one rather than making host directories of its own.
    (tmp_path / "out").mkdir()
    prepared = contain(
        ["/bin/true"],
        spec=SandboxSpec(workdir=tmp_path, writable=(tmp_path / "out",)),
        mode=SandboxMode.REQUIRED,
    )
    argv = list(prepared.argv)
    last_readonly = max(
        index for index, token in enumerate(argv) if token == "--ro-bind"
    )
    first_writable = min(index for index, token in enumerate(argv) if token == "--bind")
    assert first_writable > last_readonly


# ------------------------------------------------------------- adversarial --
@pytest.fixture
def outside_tmp() -> Iterator[Path]:
    """A fixture directory the sandbox does **not** shadow.

    `_bubblewrap` mounts `--tmpfs /tmp`, so anything pytest's `tmp_path` builds
    is invisible inside the sandbox for a reason that has nothing to do with
    policy. An adversarial audit of these tests found three of them passing on
    exactly that: the attack could not be *attempted*, the command printed
    "denied", and the assertion was satisfied. They would have passed against a
    sandbox with no filesystem policy at all.

    `/var/tmp` is not in `_OS_PATHS` and not shadowed, so a path under it is
    denied by policy rather than hidden by a mount -- which is the property
    these tests are named for.
    """

    base = Path("/var/tmp")
    if not base.is_dir() or not os.access(base, os.W_OK):  # pragma: no cover
        pytest.skip("this host has no writable /var/tmp to build fixtures in")
    made = Path(tempfile.mkdtemp(prefix="research-os-sandbox-", dir=base))
    try:
        yield made
    finally:
        shutil.rmtree(made, ignore_errors=True)


def _run_contained(script: str, *, workdir: Path, network: bool = False) -> str:
    """Run a shell snippet inside the sandbox and return its combined output."""

    prepared = contain(
        ["/bin/sh", "-c", script],
        spec=SandboxSpec(workdir=workdir, network=network),
        mode=SandboxMode.REQUIRED,
    )
    completed = subprocess.run(
        list(prepared.argv),
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env=prepared.environment,
    )
    return (completed.stdout or "") + (completed.stderr or "")


@needs_sandbox
def test_the_canonical_capsule_cannot_be_written(outside_tmp: Path) -> None:
    """The escape ``docs/RUNTIME.md`` §16 describes, attempted for real."""

    canonical = outside_tmp / "canonical"
    (canonical / ".research").mkdir(parents=True)
    claim = canonical / ".research" / "CLAIM-0001.yaml"
    claim.write_text("status: draft\n", "utf-8")
    worktree = outside_tmp / "worktree"
    worktree.mkdir()

    output = _run_contained(
        f"echo accepted > {claim} && echo WROTE || echo denied",
        workdir=worktree,
    )
    assert "WROTE" not in output
    # The half that carries the property: the *host* file, read afterwards.
    assert claim.read_text("utf-8") == "status: draft\n"


@needs_sandbox
def test_git_refs_cannot_be_updated(outside_tmp: Path) -> None:
    """A push or a branch write is as much of an escape as a commit.

    An audit found the earlier version of this test vacuous twice over: the
    repository lived under `tmp_path`, which the sandbox's own tmpfs hides, so
    `cd` failed and the `&&` chain never reached `update-ref`; and the test then
    asserted only on the command's output, never re-reading the host's refs.
    Both halves are fixed -- the repository is somewhere the sandbox can see,
    and the refs are compared before and after.
    """

    repo = outside_tmp / "repo"
    for args in (
        ["init", "-q", "--initial-branch=main", str(repo)],
        ["-C", str(repo), "config", "user.email", "t@example.invalid"],
        ["-C", str(repo), "config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], check=True, capture_output=True)
    (repo / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(
        ["git", "-C", str(repo), "add", "-A"], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(repo), "commit", "-qm", "init"],
        check=True,
        capture_output=True,
    )

    def refs() -> str:
        return subprocess.run(
            ["git", "-C", str(repo), "show-ref"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout

    before = refs()
    assert before.strip(), "the fixture has no refs, so the test would prove nothing"
    worktree = outside_tmp / "worktree"
    worktree.mkdir()
    output = _run_contained(
        f"cd {repo} && git update-ref refs/heads/escaped HEAD && echo WROTE "
        "|| echo denied",
        workdir=worktree,
    )
    assert "WROTE" not in output
    assert refs() == before


@needs_sandbox
def test_a_path_outside_the_worktree_cannot_be_written(tmp_path: Path) -> None:
    """The property is that the **host** is unchanged, not that write() fails.

    This test used to assert only that the command printed "denied", and on
    this host it started printing "WROTE" the moment containment began working
    -- which looked like an escape and was not one. `tmp_path` lives under
    `/tmp`, the sandbox mounts `--tmpfs /tmp`, and binding the worktree back in
    creates its parent directories inside that tmpfs. So the write succeeded,
    into an ephemeral filesystem that vanishes with the sandbox, and the host
    file was never created.

    Both halves are now asserted, and they are different claims: a path the
    sandbox genuinely denies must be refused, and *no* path outside the
    worktree may leave anything behind on the host.
    """

    worktree = tmp_path / "worktree"
    worktree.mkdir()

    # Under the sandbox's own tmpfs: the write may succeed, and must not reach
    # the host.
    shadowed = tmp_path / "escaped"
    _run_contained(f"touch {shadowed} && echo WROTE || echo denied", workdir=worktree)
    assert not shadowed.exists(), "a write under /tmp reached the host"

    # Outside anything the sandbox provides: the write must be refused outright.
    denied = Path.home() / ".research-os-escape-probe"
    output = _run_contained(
        f"touch {denied} && echo WROTE || echo denied", workdir=worktree
    )
    assert "WROTE" not in output, output
    assert not denied.exists(), "a write under $HOME reached the host"


@needs_sandbox
def test_a_symlink_out_of_the_worktree_does_not_escape(outside_tmp: Path) -> None:
    """Containment has to survive the worktree's own contents.

    Scope enforcement refuses outbound symlinks, and a sandbox that relied on
    that would be relying on a check that runs at a different time.

    The fixture is outside `/tmp` deliberately. With it under `tmp_path` the
    link dangled inside the sandbox's own tmpfs, so `cat` failed for a reason
    unrelated to containment and the test passed without exercising the
    property it is named for. An audit found it.
    """

    outside = outside_tmp / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("SECRET\n", encoding="utf-8")
    worktree = outside_tmp / "worktree"
    worktree.mkdir()
    (worktree / "link").symlink_to(outside)
    # The link really does resolve on the host; only the sandbox may refuse it.
    assert (worktree / "link" / "secret.txt").read_text("utf-8") == "SECRET\n"

    output = _run_contained("cat link/secret.txt || echo denied", workdir=worktree)
    assert "SECRET" not in output
    # And writing through it changes nothing on the host.
    planted = outside / "planted.txt"
    _run_contained("echo x > link/planted.txt || echo denied", workdir=worktree)
    assert not planted.exists()


@needs_sandbox
def test_the_home_directory_and_ssh_keys_cannot_be_read(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    real_home = Path(os.path.expanduser("~"))
    output = _run_contained(
        f"ls {real_home} 2>&1; cat {real_home / '.ssh' / 'id_ed25519'} 2>&1; "
        f"echo HOME=$HOME",
        workdir=worktree,
    )
    assert "PRIVATE KEY" not in output
    assert str(real_home) not in output.split("HOME=")[-1]


@needs_sandbox
def test_a_secret_in_the_environment_is_not_inherited(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    os.environ["SANDBOX_TEST_SECRET"] = "leaked"
    try:
        output = _run_contained(
            "echo secret=${SANDBOX_TEST_SECRET:-absent}", workdir=worktree
        )
    finally:
        os.environ.pop("SANDBOX_TEST_SECRET", None)
    assert "secret=absent" in output


@needs_sandbox
def test_outbound_network_is_denied_unless_granted(tmp_path: Path) -> None:
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    denied = _run_contained(
        "getent hosts pypi.org >/dev/null 2>&1 && echo REACHED || echo denied",
        workdir=worktree,
    )
    assert "REACHED" not in denied


# ------------------------------------------------- the command runner path --
def test_an_acceptance_command_records_whether_it_was_contained(
    tmp_path: Path,
) -> None:
    """Every result carries the fact, whichever way it went."""

    from research_os.automation.checks import run_acceptance_command

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    result = run_acceptance_command(
        AcceptanceCommand(argv=["true"], description="trivial"),
        cwd=worktree,
        timeout_seconds=30,
        sandbox_mode=SandboxMode.PREFERRED,
    )
    assert result.ok
    assert result.contained is (BACKEND is not None)
    assert result.containment
    # The recorded argv is the project's command, not the sandbox wrapper: a
    # researcher reading a run wants to see what they declared.
    assert result.argv == ["true"]


def test_an_acceptance_command_under_required_reports_the_refusal(
    tmp_path: Path,
) -> None:
    from research_os.automation.checks import run_acceptance_command

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    if BACKEND is None:
        with pytest.raises(SandboxError):
            run_acceptance_command(
                AcceptanceCommand(argv=["true"], description="trivial"),
                cwd=worktree,
                timeout_seconds=30,
                sandbox_mode=SandboxMode.REQUIRED,
            )
    else:
        result = run_acceptance_command(
            AcceptanceCommand(argv=["true"], description="trivial"),
            cwd=worktree,
            timeout_seconds=30,
            sandbox_mode=SandboxMode.REQUIRED,
        )
        assert result.contained is True


def test_a_required_run_is_refused_before_a_worktree_exists(tmp_path: Path) -> None:
    """A refusal halfway through leaves a worktree, a branch and a stuck run.

    `SandboxError` is not an `AutomationError`, so a containment refusal raised
    from inside an acceptance command escaped the handler that fails a run
    cleanly and left it EXECUTING forever. The preflight makes that unreachable:
    the whole point of this pipeline is to execute code a model wrote, so a
    policy that requires containment on a host with none has nothing to offer
    the run and should say so before creating anything.
    """

    from research_os.automation.config import AutomationConfig
    from research_os.automation.controller import AutomationController
    from research_os.errors import PreflightError
    from tests.proposal_helpers import fake_config

    config: AutomationConfig = fake_config()
    controller = AutomationController(
        providers={}, config=config, sandbox_mode=SandboxMode.REQUIRED
    )
    if BACKEND is None:
        with pytest.raises(PreflightError, match="requires OS-level containment"):
            controller.start(project_path=tmp_path, goal="do something")
        # Nothing was created. The message names the remedy rather than being a
        # wall.
        assert not (tmp_path / ".git").exists()
    else:
        # On a host that can contain, the preflight is not what refuses -- the
        # project not being a repository is.
        with pytest.raises(PreflightError):
            controller.start(project_path=tmp_path, goal="do something")


def test_a_preferred_run_is_not_refused_by_the_preflight(tmp_path: Path) -> None:
    """`preferred` runs and records the absence; only `required` refuses."""

    from research_os.automation.controller import AutomationController
    from research_os.errors import PreflightError
    from tests.proposal_helpers import fake_config

    controller = AutomationController(
        providers={}, config=fake_config(), sandbox_mode=SandboxMode.PREFERRED
    )
    with pytest.raises(PreflightError) as raised:
        controller.start(project_path=tmp_path, goal="do something")
    assert "requires OS-level containment" not in str(raised.value), (
        "a preferred run was refused for a reason that only applies to required"
    )


# -- the program itself has to exist inside --------------------------------
def _flags(argv: list[str], spec: SandboxSpec) -> list[str]:
    """The bubblewrap invocation, built without needing a working bwrap.

    The host this was developed on cannot create user namespaces, so every
    test that *runs* a contained command is skipped here. The flag
    construction is still deterministic and is still where the mistakes are,
    so it is tested directly.
    """

    from research_os.sandbox import SandboxProbe, _bubblewrap

    backend = SandboxProbe(
        technology="bubblewrap",
        namespace_state=NamespaceState.AVAILABLE,
        security_eligible=True,
        executable="/usr/bin/bwrap",
        detail="assumed for a construction test",
    )
    return list(_bubblewrap(argv, spec=spec, backend=backend, environment={}).argv)


def _setenv(flags: list[str], name: str) -> str | None:
    for index, token in enumerate(flags):
        if token == "--setenv" and flags[index + 1] == name:
            return flags[index + 2]
    return None


def test_a_program_outside_the_os_allowlist_is_bound_in(tmp_path: Path) -> None:
    """Containment must not make the command disappear.

    `uv` -- what every acceptance command in this repository starts with --
    installs to `~/.local/bin`, which is neither in the read-only OS binds nor
    on the sandbox PATH. A contained `uv run pytest` exited 127 with
    "uv: not found", recorded as an acceptance failure of the code the worker
    had just written.
    """

    program = tmp_path / "tools" / "faketool"
    program.parent.mkdir(parents=True)
    program.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    program.chmod(0o755)
    workdir = tmp_path / "project"
    workdir.mkdir()

    flags = _flags([str(program), "--version"], SandboxSpec(workdir=workdir))

    assert ["--ro-bind", str(program), str(program)] == [
        flags[flags.index(str(program)) - 1],
        flags[flags.index(str(program))],
        flags[flags.index(str(program)) + 1],
    ]
    path = _setenv(flags, "PATH")
    assert path is not None and path.split(":")[0] == str(program.parent)


def test_a_system_program_adds_no_binding(tmp_path: Path) -> None:
    """The common case costs nothing: /bin/sh is already bound read-only."""

    workdir = tmp_path / "project"
    workdir.mkdir()
    flags = _flags(["/bin/sh", "-c", "true"], SandboxSpec(workdir=workdir))
    assert "/bin/sh" not in [
        flags[index + 1]
        for index, token in enumerate(flags)
        if token == "--ro-bind" and index + 1 < len(flags)
    ]
    path = _setenv(flags, "PATH")
    assert path is not None and not path.startswith("/bin:")


def test_uv_reads_its_interpreters_and_writes_nothing_on_the_host(
    monkeypatch, tmp_path
) -> None:
    """The uv data directory is read-only, and the cache is not in *this* list.

    An earlier version put both in `writable`, which was a host code-execution
    escape: the managed interpreter's stdlib directory is writable by this user
    and is what nine venvs on this machine execute, so contained model-written
    code could drop a `sitecustomize.py` there and the next `uv run` anywhere
    on the host would execute it outside the sandbox.

    **The cache is not absent from the sandbox**, and an audit was right that
    the previous wording here implied it was. A read-only cache does not work --
    measured: `Failed to initialize cache ... Permission denied` -- so it is
    handled by `uv_discarded_paths` instead, which exposes it as a throwaway
    overlay: readable wholesale, writable in appearance, every write destroyed
    with the sandbox. That is a real read exposure of the researcher's package
    cache and it is stated in `SECURITY.md`. What this test pins is narrower
    and still worth pinning: the cache never reaches the *read-only bind* list,
    where it would break uv, nor the writable one, where it was an escape.
    """

    from research_os.automation.checks import uv_readonly_paths

    home = tmp_path / "home"
    (home / ".cache" / "uv").mkdir(parents=True)
    (home / ".local" / "share" / "uv").mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("UV_CACHE_DIR", raising=False)
    monkeypatch.delenv("XDG_CACHE_HOME", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)

    paths = uv_readonly_paths(["uv", "run", "--frozen", "pytest"])

    assert paths == (home / ".local" / "share" / "uv",)
    assert home / ".cache" / "uv" not in paths, (
        "the package cache must not be in the read-only bind list -- uv cannot "
        "initialise a read-only cache. It is exposed as an overlay instead."
    )
    from research_os.automation.checks import uv_discarded_paths

    assert uv_discarded_paths(["uv", "run", "--frozen", "pytest"]) == (
        home / ".cache" / "uv",
    )
    assert uv_discarded_paths(["pytest"]) == ()
    # Only for uv. Every other program's needs are the caller's to declare.
    assert uv_readonly_paths(["pytest"]) == ()
    assert uv_readonly_paths([]) == ()


def test_the_uv_directories_never_reach_the_writable_set(tmp_path) -> None:
    """The property that matters, asserted where the spec is built.

    A unit test on `uv_readonly_paths` would pass while the call site put its
    result in `writable`, which is exactly the bug that happened.
    """

    from research_os.automation.checks import run_acceptance_command

    captured: dict[str, object] = {}
    real_contain = __import__(
        "research_os.automation.checks", fromlist=["contain"]
    ).contain

    def spy(argv, *, spec, mode):
        captured["writable"] = tuple(str(p) for p in spec.resolved_writable())
        captured["readable"] = tuple(str(p) for p in spec.resolved_readable())
        return real_contain(argv, spec=spec, mode=mode)

    import research_os.automation.checks as checks_module

    work = tmp_path / "project"
    work.mkdir()
    environment = tmp_path / "env"
    original = checks_module.contain
    checks_module.contain = spy  # type: ignore[assignment]
    try:
        run_acceptance_command(
            AcceptanceCommand(argv=["uv", "--version"], required=True),
            cwd=work,
            timeout_seconds=60,
            uv_project_environment=environment,
            sandbox_mode=SandboxMode.PREFERRED,
        )
    finally:
        checks_module.contain = original  # type: ignore[assignment]

    writable = captured.get("writable") or ()
    offenders = [path for path in writable if "/uv" in path and "env" not in path]
    assert offenders == [], f"uv host directories reached the writable set: {offenders}"


# -- Slurm is not contained by this process --------------------------------
def test_a_slurm_job_does_not_inherit_the_daemons_environment(tmp_path) -> None:
    """sbatch defaults to `--export=ALL`, which copies the runtime's secrets in."""

    from research_os.runtime.executors import ExecutionSpec, SlurmExecutor

    spec = ExecutionSpec(
        name="probe",
        argv=("echo", "hello"),
        cwd=str(tmp_path),
        env={"SEED": "17"},
        resources={"partition": "short"},
        timeout_seconds=60,
    )
    script = SlurmExecutor().build_script(spec, run_dir=tmp_path, job_name="ros-probe")

    assert "#SBATCH --export=NONE" in script
    # And the only environment the payload gets is the frozen spec's.
    assert "export SEED='17'" in script or "export SEED=17" in script


def test_choosing_slurm_is_not_a_way_around_required_containment(tmp_path) -> None:
    """`executor: slurm` was a one-word bypass of `required`."""

    from research_os.runtime.executors import (
        ContainmentUnavailableError,
        ExecutionSpec,
        SlurmExecutor,
    )

    spec = ExecutionSpec(
        name="probe",
        argv=("echo", "hello"),
        cwd=str(tmp_path),
        timeout_seconds=60,
    )
    executor = SlurmExecutor(sandbox_mode=SandboxMode.REQUIRED)
    with pytest.raises(ContainmentUnavailableError, match="compute node"):
        executor.submit(spec, run_dir=tmp_path)


# -- the process cap is uid-scoped, so it must never reach an uncontained child
def test_the_process_cap_is_not_applied_to_an_uncontained_command(
    tmp_path: Path,
) -> None:
    """`RLIMIT_NPROC` counts the researcher's whole session, not a process tree.

    This repository has made this mistake twice. First `LimitNPROC=256` in a
    `systemd-run` probe ("fork: Resource temporarily unavailable"); then
    `process_limit_preexec` on the *uncontained* acceptance path, where the
    ceiling of 512 sat below this user's existing 1119 threads, so `uv` could
    not create its first thread and aborted with SIGABRT before running
    anything. Seventeen tests that shell out to a real `uv run` failed with
    "required acceptance commands failed".
    """

    from research_os.sandbox import process_limit_preexec

    spec = SandboxSpec(workdir=tmp_path, max_processes=512)

    assert process_limit_preexec(spec, contained=False) is None
    assert process_limit_preexec(spec, contained=True) is not None
    # And no ceiling means no preexec either way.
    unlimited = SandboxSpec(workdir=tmp_path, max_processes=None)
    assert process_limit_preexec(unlimited, contained=True) is None


def test_an_uncontained_acceptance_command_actually_runs(tmp_path: Path) -> None:
    """The end-to-end form of the above, with a real child process.

    A unit test on the preexec factory would have passed while the acceptance
    path was broken, because the bug was at the call site. This one spawns
    something.
    """

    from research_os.automation.checks import run_acceptance_command

    work = tmp_path / "project"
    work.mkdir()
    result = run_acceptance_command(
        AcceptanceCommand(argv=["sh", "-c", "exec true"], required=True),
        cwd=work,
        timeout_seconds=60,
        sandbox_mode=SandboxMode.PREFERRED,
    )

    assert result.exit_code == 0, (
        f"an uncontained acceptance command did not run: exit {result.exit_code}, "
        f"{result.error}"
    )


def test_a_relative_program_resolves_against_the_sandbox_workdir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`./run.sh` means the same thing inside as it does to the caller.

    Resolving it in this process's working directory would bind whatever
    happened to sit beside the daemon under that name.
    """

    workdir = tmp_path / "project"
    workdir.mkdir()
    script = workdir / "run.sh"
    script.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    script.chmod(0o755)
    decoy = tmp_path / "elsewhere"
    decoy.mkdir()
    (decoy / "run.sh").write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
    monkeypatch.chdir(decoy)

    flags = _flags(["./run.sh"], SandboxSpec(workdir=workdir))

    # Inside the workdir, which is already bound, so it needs no bind of its own
    # and must not have picked up the decoy.
    assert str(decoy / "run.sh") not in flags
