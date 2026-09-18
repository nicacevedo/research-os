"""The execution dependency closure: what a contained command needs to *run*.

The defect this file exists for was invisible until containment started really
executing things, and then it failed 112 tests at once with a message that
named the wrong file:

```text
bwrap: execvp pytest: No such file or directory
```

for a ``pytest`` that existed, was bound read-only, and was on the sandbox
PATH. ``<venv>/bin/pytest`` begins ``#!<venv>/bin/python3``, and the kernel
reports a missing *interpreter* by naming the *script*. The unit a sandbox has
to expose is therefore not the executable file, it is everything the kernel and
the program's runtime touch between ``execvp`` and the first instruction.

Two kinds of test, split the same way `test_sandbox.py` splits them.
**Construction** tests read the flags and run everywhere. **Behavioural** tests
actually execute a command inside the sandbox and are skipped where no
containment technology exists -- and the skip is the release-blocking evidence
rather than a gap.

The security property under test is always a conjunction, because either half
alone is easy:

```text
the command works   AND   no additional host authority appeared
```
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from research_os.sandbox import (
    SandboxMode,
    SandboxPreparationError,
    SandboxSpec,
    available_backend,
    contain,
    program_binding,
    unavailable_reason,
)

BACKEND = available_backend()
needs_sandbox = pytest.mark.skipif(
    BACKEND is None,
    reason=f"no containment technology is available here: {unavailable_reason()}",
)

REPO_VENV = Path(__file__).resolve().parent.parent / ".venv"


def _exposed_under(home: Path) -> list[Path]:
    """Host paths under ``home`` that policy deliberately binds into a sandbox.

    Their empty ancestors necessarily appear inside, because a bind mount needs
    its destination to exist. Enumerating them is what turns "HOME is absent"
    -- which is false -- into a property that is both true and worth asserting.
    """

    from research_os.automation.checks import uv_cache_dir, uv_readonly_paths

    found = [*uv_readonly_paths(["uv"])]
    cache = uv_cache_dir()
    if cache is not None:
        found.append(cache)
    return [path for path in found if path == home or home in path.parents]


def _must_stay_absent() -> list[Path]:
    """Deep paths under ``$HOME`` that must not be reachable from inside.

    This system's own state, which a bind of `~/.local` would expose wholesale
    while a check on top-level directory names would not notice.
    """

    home = Path.home()
    # The *real* paths, not the ones `conftest.py` redirects into pytest's
    # basetemp -- which is under `/tmp`, which the sandbox replaces with a
    # tmpfs. An audit found the first version of this list naming redirected
    # paths, so three of its four entries were absent inside for a reason that
    # had nothing to do with binding policy: the fix for one audit finding
    # reproduced the defect of another.
    return [
        home / ".local" / "state" / "research-os",
        home / ".local" / "share" / "research-os",
        home / ".local" / "state" / "research-os" / "containment" / "validated.json",
        home / ".ssh",
        home / ".bashrc",
    ]


def run_contained(
    argv: list[str], *, spec: SandboxSpec, timeout: int = 120
) -> subprocess.CompletedProcess[str]:
    """Execute ``argv`` inside real containment and return what happened."""

    prepared = contain(argv, spec=spec, mode=SandboxMode.REQUIRED)
    assert prepared.contained, prepared.detail
    return subprocess.run(
        list(prepared.argv),
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=timeout,
        env=prepared.environment,
    )


def _flags(argv: list[str], spec: SandboxSpec) -> list[str]:
    """The bubblewrap invocation, built without needing a working bwrap.

    Flag *order* is a security property here -- later binds win -- and order is
    deterministic, so it is tested directly rather than only where a backend
    exists.
    """

    from research_os.sandbox import NamespaceState, SandboxProbe, _bubblewrap

    backend = SandboxProbe(
        technology="bubblewrap",
        namespace_state=NamespaceState.AVAILABLE,
        security_eligible=True,
        executable="/usr/bin/bwrap",
        detail="assumed for a construction test",
    )
    return list(_bubblewrap(argv, spec=spec, backend=backend, environment={}).argv)


def output(completed: subprocess.CompletedProcess[str]) -> str:
    return (completed.stdout or "") + (completed.stderr or "")


@pytest.fixture(scope="module")
def venv(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """A real virtual environment inside a project directory that is not it.

    The *project* around it is the point: half the tests below are about the
    parent directory not coming along when the environment does.
    """

    project = tmp_path_factory.mktemp("parent-project")
    (project / "SECRET.txt").write_text("the project's private notes\n", "utf-8")
    target = project / ".venv"
    subprocess.run(
        [sys.executable, "-m", "venv", "--without-pip", str(target)],
        check=True,
        capture_output=True,
    )
    # A console script exactly as a packaging tool writes one: a Python file
    # whose `#!` line names the environment's own interpreter by its link name.
    script = target / "bin" / "demotool"
    script.write_text(
        f"#!{target / 'bin' / 'python3'}\n"
        "import sys\n"
        "print('demotool ran on', sys.version_info[:2])\n",
        encoding="utf-8",
    )
    script.chmod(0o755)
    # And one that reaches its interpreter through `env` instead.
    through_env = target / "bin" / "envtool"
    through_env.write_text(
        "#!/usr/bin/env python3\nprint('envtool ran')\n", encoding="utf-8"
    )
    through_env.chmod(0o755)
    return target


# --------------------------------------------------- 1. the categories run --
@needs_sandbox
def test_a_native_elf_command_runs(tmp_path: Path) -> None:
    """The case that always worked, pinned so a rewrite cannot lose it."""

    completed = run_contained(["git", "--version"], spec=SandboxSpec(workdir=tmp_path))
    assert completed.returncode == 0
    assert "git version" in completed.stdout


@needs_sandbox
def test_a_script_with_an_absolute_shebang_runs(tmp_path: Path) -> None:
    """``#!/bin/sh`` resolves because ``/bin`` is bound; nothing else is needed."""

    script = tmp_path / "tools" / "run.sh"
    script.parent.mkdir()
    script.write_text("#!/bin/sh\necho ran-under-sh\n", encoding="utf-8")
    script.chmod(0o755)

    completed = run_contained([str(script)], spec=SandboxSpec(workdir=tmp_path))
    assert completed.returncode == 0, output(completed)
    assert "ran-under-sh" in completed.stdout


@needs_sandbox
def test_a_virtualenv_console_script_runs(venv: Path, tmp_path: Path) -> None:
    """**The exact failure this release opened with**, as a behavioural test.

    A console script, its interpreter reached through a symbolic-link chain,
    and that interpreter's own installation prefix -- three separate host
    locations, none of them under the operating-system allowlist, all of them
    required before the first line of the script executes.
    """

    completed = run_contained(
        [str(venv / "bin" / "demotool")], spec=SandboxSpec(workdir=tmp_path)
    )
    assert completed.returncode == 0, output(completed)
    assert "demotool ran on" in completed.stdout


@needs_sandbox
def test_the_repositorys_own_pytest_runs_as_a_console_script(tmp_path: Path) -> None:
    """The real article, not a fixture resembling it.

    ``.venv/bin/pytest`` is what the acceptance path executes and what produced
    ``execvp pytest: No such file or directory``. A synthetic venv can differ
    from the one uv builds in ways a rewrite of this policy would not notice.
    """

    console = REPO_VENV / "bin" / "pytest"
    if not console.is_file():
        pytest.skip("this checkout has no .venv/bin/pytest to exercise")
    (tmp_path / "test_trivial.py").write_text(
        "def test_ok():\n    assert True\n", encoding="utf-8"
    )

    completed = run_contained(
        [str(console), "-q", "-p", "no:cacheprovider", "test_trivial.py"],
        spec=SandboxSpec(workdir=tmp_path),
    )
    assert completed.returncode == 0, output(completed)
    assert "1 passed" in completed.stdout


@needs_sandbox
def test_the_venv_interpreter_symlink_chain_resolves(
    venv: Path, tmp_path: Path
) -> None:
    """Every name in the chain is bound, not only the file at the end.

    Python finds ``pyvenv.cfg`` beside the interpreter it was *invoked as*. Bind
    only the final target and ``sys.prefix`` becomes the base installation, the
    environment's own ``site-packages`` disappears, and the failure surfaces as
    a missing import rather than as a missing mount.
    """

    interpreter = venv / "bin" / "python3"
    assert interpreter.is_symlink(), "fixture no longer exercises a symlink chain"

    completed = run_contained(
        [
            str(interpreter),
            "-c",
            "import sys; print(sys.prefix); print(sys.executable)",
        ],
        spec=SandboxSpec(workdir=tmp_path),
    )
    assert completed.returncode == 0, output(completed)
    assert completed.stdout.splitlines()[0] == str(venv)
    assert completed.stdout.splitlines()[1] == str(interpreter)


@needs_sandbox
def test_an_env_shebang_resolves_through_the_sandboxs_own_path(
    venv: Path, tmp_path: Path
) -> None:
    """``#!/usr/bin/env python3`` runs, and runs the interpreter policy chose.

    Resolution is against the PATH the sandbox will have, never the host's, so
    what gets bound and what gets executed are the same file by construction.
    """

    completed = run_contained(
        [str(venv / "bin" / "envtool")], spec=SandboxSpec(workdir=tmp_path)
    )
    assert completed.returncode == 0, output(completed)
    assert "envtool ran" in completed.stdout


# ------------------------------------------------- 2. the failures are shut --
def test_a_missing_interpreter_fails_before_execution(tmp_path: Path) -> None:
    """Named here, or reported by the kernel as the *script* being absent."""

    script = tmp_path / "run.py"
    script.write_text("#!/nonexistent/python9\nprint(1)\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert "/nonexistent/python9" in str(raised.value)
    assert script.name in str(raised.value)


@pytest.mark.parametrize(
    ("first_line", "expected"),
    [
        ("#!\n", "names no interpreter"),
        ("#!   \n", "names no interpreter"),
        ("#!python3\n", "not an absolute path"),
        ("#!/usr/bin/env\n", "no interpreter to find"),
        ("#!/usr/bin/env -S python3\n", "option"),
        ("#!/usr/bin/env FOO=bar python3\n", "through `env`"),
        ("#!/usr/bin/env some/path\n", "bare program name"),
        ("#!/usr/bin/env definitely-not-installed-anywhere\n", "sandbox's own PATH"),
    ],
)
def test_a_malformed_shebang_fails_closed(
    tmp_path: Path, first_line: str, expected: str
) -> None:
    """Every one of these is a case where guessing chooses a host file."""

    script = tmp_path / "run"
    script.write_text(first_line + "print(1)\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert expected in str(raised.value)


def test_a_shebang_longer_than_the_kernel_reads_fails_closed(tmp_path: Path) -> None:
    """The kernel truncates at 256 bytes; a guess past that runs something else."""

    script = tmp_path / "run"
    script.write_text("#!/usr/bin/python3 " + "x" * 400 + "\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert "longer than" in str(raised.value)


def test_a_symlink_cycle_in_the_program_fails_closed(tmp_path: Path) -> None:
    """A loop is refused rather than followed to whatever it lands on."""

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.symlink_to(second)
    second.symlink_to(first)
    script = tmp_path / "run"
    script.write_text(f"#!{first}\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError):
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )


# ---------------------------------------- 3. exposure is exactly the runtime --
def test_exposing_a_venv_does_not_expose_its_parent_project(venv: Path) -> None:
    """A virtual environment is a runtime. The project around it is not.

    This is the reason :func:`program_binding` refuses to bind a parent
    directory: ``uv`` lives in ``~/.local/bin`` under a ``~/.local`` that holds
    this system's own state, and a console script lives inside somebody's
    checkout.
    """

    elsewhere = venv.parent.parent / "unrelated-workdir"
    elsewhere.mkdir(exist_ok=True)
    binding = program_binding(
        [str(venv / "bin" / "demotool")], spec=SandboxSpec(workdir=elsewhere)
    )
    project = venv.parent
    assert venv in binding.paths
    assert project not in binding.paths
    assert not any(path == project for path in binding.paths)
    # Nothing bound reaches the project except by being the environment itself.
    for path in binding.paths:
        if project in path.parents:
            assert path == venv


@needs_sandbox
def test_the_parent_project_is_unreadable_while_the_venv_works(
    venv: Path, tmp_path: Path
) -> None:
    """The construction test above, executed: the secret beside it is gone."""

    secret = venv.parent / "SECRET.txt"
    assert secret.is_file()
    reader = venv / "bin" / "peek"
    reader.write_text(
        f"#!{venv / 'bin' / 'python3'}\n"
        "import pathlib\n"
        f"p = pathlib.Path({str(secret)!r})\n"
        "print('READ:' + p.read_text() if p.exists() else 'ABSENT')\n",
        encoding="utf-8",
    )
    reader.chmod(0o755)

    completed = run_contained([str(reader)], spec=SandboxSpec(workdir=tmp_path))
    assert completed.returncode == 0, output(completed)
    assert "ABSENT" in completed.stdout
    assert "private notes" not in output(completed)


@needs_sandbox
def test_the_virtualenv_is_readable_and_not_writable(
    venv: Path, tmp_path: Path
) -> None:
    """Read-only is the whole basis for exposing it.

    A writable ``site-packages`` is host code execution: the next command that
    uses this environment -- inside the sandbox or outside it -- imports
    whatever was left there.
    """

    probe = venv / "bin" / "writeprobe"
    probe.write_text(
        f"#!{venv / 'bin' / 'python3'}\n"
        "import pathlib, sys\n"
        "for target in (pathlib.Path(sys.prefix) / 'lib', pathlib.Path(sys.prefix) / 'bin',\n"
        "               pathlib.Path(sys.prefix) / 'pyvenv.cfg'):\n"
        "    try:\n"
        "        if target.is_dir():\n"
        "            (target / 'sitecustomize.py').write_text('import os\\n')\n"
        "        else:\n"
        "            target.write_text('tampered\\n')\n"
        "        print('WROTE', target)\n"
        "    except OSError as exc:\n"
        "        print('DENIED', target.name, type(exc).__name__)\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)

    completed = run_contained([str(probe)], spec=SandboxSpec(workdir=tmp_path))
    assert completed.returncode == 0, output(completed)
    assert "WROTE" not in completed.stdout, completed.stdout
    assert completed.stdout.count("DENIED") == 3
    # And the host agrees afterwards, which is the half a report cannot fake.
    assert not (venv / "lib" / "sitecustomize.py").exists()
    assert not (venv / "bin" / "sitecustomize.py").exists()
    assert "tampered" not in (venv / "pyvenv.cfg").read_text("utf-8")


def test_a_console_script_does_not_expose_its_neighbours(tmp_path: Path) -> None:
    """``~/.local/bin`` holds forty console scripts and no runtime.

    The directory is put on the sandbox PATH so a bare ``argv[0]`` still
    resolves; only the one file is bound.
    """

    toolbox = tmp_path / "bin"
    toolbox.mkdir()
    wanted = toolbox / "wanted"
    wanted.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    wanted.chmod(0o755)
    neighbour = toolbox / "credentials-helper"
    neighbour.write_text("#!/bin/sh\necho secret\n", encoding="utf-8")
    neighbour.chmod(0o755)

    binding = program_binding([str(wanted)], spec=SandboxSpec(workdir=tmp_path / "w"))
    assert binding.paths == (wanted,)
    assert binding.path_prefix == str(toolbox)
    assert neighbour not in binding.paths
    assert toolbox not in binding.paths


def test_a_system_program_still_costs_nothing(tmp_path: Path) -> None:
    """The common case adds no bind and no PATH entry."""

    binding = program_binding(["/bin/sh"], spec=SandboxSpec(workdir=tmp_path))
    assert binding.paths == ()
    assert binding.path_prefix is None


# ----------------------------------------------- 4. content cannot choose it --
def test_a_shebang_cannot_name_a_path_outside_policy(tmp_path: Path) -> None:
    """A ``#!`` line is file content a model wrote one step earlier.

    ``argv[0]`` is chosen by the command policy or by the researcher. The
    interpreter is chosen by whatever is in the file, so it is honoured only
    inside the operating system, inside what the caller declared, or inside the
    program's own runtime.
    """

    outsider = tmp_path / "elsewhere" / "python3"
    outsider.parent.mkdir()
    outsider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outsider.chmod(0o755)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    script = worktree / "run"
    script.write_text(f"#!{outsider}\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)], spec=SandboxSpec(workdir=worktree), mode=SandboxMode.REQUIRED
        )
    assert "does not get to choose" in str(raised.value)


def test_a_shebang_cannot_reach_an_unrelated_virtualenv(tmp_path: Path) -> None:
    """A self-declared runtime root somewhere else is still somewhere else."""

    other = tmp_path / "other-project" / ".venv"
    (other / "bin").mkdir(parents=True)
    (other / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    interpreter = other / "bin" / "python3"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    script = worktree / "run"
    script.write_text(f"#!{interpreter}\n", encoding="utf-8")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError):
        contain(
            [str(script)], spec=SandboxSpec(workdir=worktree), mode=SandboxMode.REQUIRED
        )


def test_a_malicious_interpreter_symlink_cannot_redirect_the_binding(
    tmp_path: Path,
) -> None:
    """Following the chain must not turn an approved name into an arbitrary file.

    The environment's interpreter is replaced with a link pointing at a file in
    the researcher's home. Every name in the chain gets bound, so a chain that
    leaves policy has to be refused rather than followed.
    """

    fake_home = tmp_path / "home"
    fake_home.mkdir()
    prize = fake_home / "id_rsa"
    prize.write_text("PRIVATE KEY\n", encoding="utf-8")
    prize.chmod(0o755)  # executable, so only *policy* can stop it
    environment = tmp_path / "proj" / ".venv"
    (environment / "bin").mkdir(parents=True)
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    (environment / "bin" / "python3").symlink_to(prize)
    script = environment / "bin" / "tool"
    script.write_text(f"#!{environment / 'bin' / 'python3'}\n", encoding="utf-8")
    script.chmod(0o755)
    worktree = tmp_path / "w"
    worktree.mkdir()

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)],
            spec=SandboxSpec(workdir=worktree),
            mode=SandboxMode.REQUIRED,
        )
    assert str(prize) in str(raised.value)
    assert "does not get to choose" in str(raised.value)


def test_a_spec_path_cannot_substitute_the_interpreter(tmp_path: Path) -> None:
    """``PATH`` in the spec wins *inside* the sandbox and decides nothing here.

    A variable that chose which host file gets bound would be a variable that
    chose what the sandbox exposes. It can make a command fail to find its
    interpreter, which is visible; it cannot make it find a different one.
    """

    decoy = tmp_path / "decoy"
    decoy.mkdir()
    planted = decoy / "python3"
    planted.write_text("#!/bin/sh\necho planted\n", encoding="utf-8")
    planted.chmod(0o755)
    worktree = tmp_path / "w"
    worktree.mkdir()
    script = worktree / "run"
    script.write_text("#!/usr/bin/env python3\n", encoding="utf-8")
    script.chmod(0o755)

    binding = program_binding(
        [str(script)],
        spec=SandboxSpec(workdir=worktree, environment={"PATH": str(decoy)}),
    )
    assert planted not in binding.paths
    assert decoy not in binding.paths


def test_a_relative_program_never_resolves_against_the_daemons_directory(
    tmp_path: Path,
) -> None:
    """``..`` is refused outright: it is how ``--ro-bind /`` was obtained once."""

    worktree = tmp_path / "w"
    worktree.mkdir()
    assert (
        program_binding(["../../bin/sh"], spec=SandboxSpec(workdir=worktree)).paths
        == ()
    )
    assert program_binding(["/"], spec=SandboxSpec(workdir=worktree)).paths == ()


# ------------------------------------------------ 5. the diagnoses are apart --
def test_the_five_failure_kinds_are_told_apart(tmp_path: Path) -> None:
    """One ``ENOENT`` used to stand for five different problems.

    Namespace denial, a missing ELF loader, a missing ``#!`` interpreter, a
    sandbox that could not be set up, and the command simply failing are five
    facts with five different responses, and reading any of them as the others
    is how this project lost an afternoon to a usrmerge symlink.
    """

    from research_os.sandbox import NamespaceState, classify_namespace_failure

    # Namespace denial, from the message the kernel gives, and told apart from
    # a probe that broke for some other reason.
    assert (
        classify_namespace_failure(
            "bwrap: Creating new namespace failed: Operation not permitted"
        )
        is NamespaceState.BLOCKED
    )
    assert (
        classify_namespace_failure("bwrap: execvp /bin/true: No such file or directory")
        is NamespaceState.PROBE_ERROR
    )

    # A missing `#!` interpreter names the interpreter.
    script = tmp_path / "s"
    script.write_text("#!/no/such/python\n", encoding="utf-8")
    script.chmod(0o755)
    with pytest.raises(SandboxPreparationError) as missing:
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert "/no/such/python" in str(missing.value)

    # A sandbox that cannot be set up names the path, not the command.
    with pytest.raises(SandboxPreparationError) as setup:
        contain(
            ["/bin/true"],
            spec=SandboxSpec(workdir=tmp_path, writable=(tmp_path / "absent",)),
            mode=SandboxMode.REQUIRED,
        )
    assert "declared writable and do not exist" in str(setup.value)

    # And a command that merely fails is not any of the above.
    if BACKEND is not None:
        completed = run_contained(["/bin/false"], spec=SandboxSpec(workdir=tmp_path))
        assert completed.returncode == 1
        assert output(completed) == ""


@needs_sandbox
def test_a_missing_elf_loader_is_named_rather_than_guessed(tmp_path: Path) -> None:
    """``PT_INTERP`` is read, so the *loader* is what the message says is absent.

    ``/usr/bin/true`` needs ``/lib64/ld-linux-x86-64.so.2``; ``/lib64`` is a
    usrmerge symlink; the namespace probe once bound neither and read the
    resulting ``ENOENT`` as "this kernel refuses user namespaces" for hours.
    """

    from research_os.sandbox import _elf_loader

    loader = _elf_loader(Path("/bin/sh").resolve())
    assert loader is not None, "this host's /bin/sh is static; the test proves nothing"
    assert loader.name.startswith("ld-")
    # And a script is not an ELF, so it reports no loader rather than a wrong one.
    script = tmp_path / "s"
    script.write_text("#!/bin/sh\n", encoding="utf-8")
    assert _elf_loader(script) is None


# ------------------------------------------- 6. nothing else came along too --
@needs_sandbox
def test_home_and_secrets_stay_absent_while_a_venv_script_runs(
    venv: Path, tmp_path: Path
) -> None:
    """The conjunction, in one test: the command works and nothing else appeared."""

    home = Path.home()
    secrets = [
        str(home / name)
        for name in (
            ".ssh",
            ".git-credentials",
            ".config/gh",
            ".aws",
            ".netrc",
            ".claude",
        )
    ]
    probe = venv / "bin" / "survey"
    probe.write_text(
        f"#!{venv / 'bin' / 'python3'}\n"
        "import pathlib\n"
        f"for name in {secrets!r}:\n"
        "    print('SECRET', name, pathlib.Path(name).exists())\n"
        f"home = pathlib.Path({str(home)!r})\n"
        "print('HOMEENTRIES', sorted(p.name for p in home.iterdir()) if home.is_dir()"
        " else [])\n"
        f"deep = {[str(item) for item in _must_stay_absent()]!r}\n"
        "print('DEEP', [f'{name} {pathlib.Path(name).exists()}' for name in deep])\n",
        encoding="utf-8",
    )
    probe.chmod(0o755)

    completed = run_contained([str(probe)], spec=SandboxSpec(workdir=tmp_path))
    assert completed.returncode == 0, output(completed)

    lines = completed.stdout.splitlines()
    for line in lines:
        if line.startswith("SECRET"):
            assert line.endswith("False"), line

    # `HOME` itself may *exist* inside, and this is the honest version of the
    # property rather than the convenient one. Binding the uv-managed
    # interpreter read-only -- which is the whole point of the closure -- makes
    # bubblewrap create the empty ancestor directories of that path, and one of
    # them is the researcher's home. What must hold is that nothing is *in* it
    # except the skeleton of what policy deliberately exposed.
    entries = next(line for line in lines if line.startswith("HOMEENTRIES"))
    visible = set(ast.literal_eval(entries[len("HOMEENTRIES ") :]))
    # **Exactly** what the closure had to bind, not merely a subset of what
    # policy might ever bind. An audit pointed out that `visible <= allowed`
    # would accept a bind of the whole of `~/.local`, because `~/.local/share/
    # uv` reduces to the same top-level name.
    binding = program_binding([str(probe)], spec=SandboxSpec(workdir=tmp_path))
    expected = {
        path.relative_to(home).parts[0]
        for path in binding.paths
        if path == home or home in path.parents
    }
    assert visible == expected, (visible, expected)
    # This spec declares nothing under `$HOME`, so the only entries that may
    # appear are the ancestors of what the *closure* had to bind -- the venv's
    # interpreter prefix. Anything else is a bind that should not have happened.

    # Top-level names are not enough on their own: `~/.local/share/uv` reduces
    # to `.local`, so a bind of the *whole* of `~/.local` -- which holds this
    # system's database and its containment record -- would satisfy the
    # assertion above unchanged. An audit pointed that out. So the deep paths
    # are named too.
    deep = next(line for line in lines if line.startswith("DEEP"))
    for entry in ast.literal_eval(deep[len("DEEP ") :]):
        assert entry.endswith("False"), entry


@needs_sandbox
def test_a_contained_command_cannot_create_another_user_namespace(
    tmp_path: Path,
) -> None:
    """Nested user namespaces are denied, and the denial is asserted by bwrap.

    A previous adversarial record noted that a contained process could still
    run ``unshare --user --map-root-user`` and be root inside the result --
    which is where published namespace escapes begin. Nothing this system
    contains needs one: acceptance commands are ``uv``, ``pytest`` and
    ``ruff``, and a declared experiment is a script.
    """

    unshare = shutil.which("unshare")
    if unshare is None:
        pytest.skip("util-linux `unshare` is not installed here")

    prepared = contain(
        [
            "/bin/sh",
            "-c",
            f"{unshare} --user --map-root-user /bin/true && echo NESTED || echo DENIED",
        ],
        spec=SandboxSpec(workdir=tmp_path),
        mode=SandboxMode.REQUIRED,
    )
    assert "--disable-userns" in prepared.argv
    assert "--assert-userns-disabled" in prepared.argv
    completed = subprocess.run(
        list(prepared.argv),
        check=False,
        capture_output=True,
        text=True,
        stdin=subprocess.DEVNULL,
        timeout=60,
        env=prepared.environment,
    )
    assert "NESTED" not in completed.stdout, output(completed)
    assert "DENIED" in completed.stdout, output(completed)


@needs_sandbox
def test_the_uv_cache_overlay_discards_every_write(tmp_path: Path) -> None:
    """A throwaway overlay: readable, writable in appearance, nothing survives.

    This is what makes a contained ``uv run`` possible with the network denied.
    It is *not* the read-write bind an earlier release shipped, which was a
    host code-execution escape, and the difference is exactly this test.
    """

    from research_os.sandbox import overlay_available

    if BACKEND is None or BACKEND.executable is None:
        pytest.skip("no backend")
    if not overlay_available(BACKEND.executable):
        pytest.skip("this host's kernel provides no unprivileged overlay")

    lower = tmp_path / "cache"
    lower.mkdir()
    (lower / "existing.txt").write_text("from the host\n", encoding="utf-8")
    worktree = tmp_path / "w"
    worktree.mkdir()

    completed = run_contained(
        [
            "/bin/sh",
            "-c",
            (
                f"cat {lower / 'existing.txt'} && "
                f"echo poisoned > {lower / 'planted.txt'} && "
                f"echo WROTE && cat {lower / 'planted.txt'}"
            ),
        ],
        spec=SandboxSpec(workdir=worktree, discarded=(lower,)),
    )
    assert completed.returncode == 0, output(completed)
    # Readable, and writable as far as the command can tell.
    assert "from the host" in completed.stdout
    assert "WROTE" in completed.stdout
    assert "poisoned" in completed.stdout
    # And the host has none of it.
    assert not (lower / "planted.txt").exists()
    assert sorted(item.name for item in lower.iterdir()) == ["existing.txt"]
    assert (lower / "existing.txt").read_text("utf-8") == "from the host\n"


@needs_sandbox
def test_a_linked_worktree_reaches_its_repository_read_only(tmp_path: Path) -> None:
    """The second defect of this release, both halves of it.

    A ``git worktree add`` checkout keeps its repository elsewhere, so a
    project whose checks include a ``git`` command failed with "not a git
    repository" naming a repository that was fine. Exposing it is necessary;
    exposing it *writable* would hand model-written code the canonical refs.
    """

    from research_os.sandbox import linked_worktree_paths

    main = tmp_path / "main"
    main.mkdir()
    for args in (
        ["init", "-q", "--initial-branch=main", "."],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)
    (main / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=main, check=True, capture_output=True
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "task"],
        cwd=main,
        check=True,
        capture_output=True,
    )

    exposed = linked_worktree_paths(worktree)
    assert exposed == (main / ".git",)
    spec = SandboxSpec(workdir=worktree, readable=exposed)

    # It works.
    completed = run_contained(["git", "status", "--porcelain"], spec=spec)
    assert completed.returncode == 0, output(completed)

    # And it is not a way to move a canonical ref.
    before = subprocess.run(
        ["git", "show-ref"], cwd=main, check=True, capture_output=True, text=True
    ).stdout
    attempt = run_contained(
        ["git", "update-ref", "refs/heads/main", "0" * 40],
        spec=spec,
    )
    assert attempt.returncode != 0, output(attempt)
    after = subprocess.run(
        ["git", "show-ref"], cwd=main, check=True, capture_output=True, text=True
    ).stdout
    assert before == after


def test_an_ordinary_checkout_needs_no_repository_exposure(tmp_path: Path) -> None:
    """Nothing is returned for a repository whose ``.git`` is where it looks."""

    from research_os.sandbox import linked_worktree_paths

    repo = tmp_path / "repo"
    (repo / ".git").mkdir(parents=True)
    assert linked_worktree_paths(repo) == ()
    assert linked_worktree_paths(tmp_path / "not-a-repo") == ()


def test_a_forged_gitdir_pointer_cannot_name_an_arbitrary_directory(
    tmp_path: Path,
) -> None:
    """``.git`` is a file inside a worktree a model can write to."""

    from research_os.sandbox import linked_worktree_paths

    worktree = tmp_path / "w"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {Path.home()}\n", encoding="utf-8")
    assert linked_worktree_paths(worktree) == ()

    (worktree / ".git").write_text("gitdir: /\n", encoding="utf-8")
    assert linked_worktree_paths(worktree) == ()

    target = tmp_path / "secrets"
    target.mkdir()
    (worktree / ".git").write_text(f"gitdir: {target}\n", encoding="utf-8")
    assert linked_worktree_paths(worktree) == ()


def test_the_binding_never_returns_home_or_the_filesystem_root(tmp_path: Path) -> None:
    """A last guard, because no correct answer here is ever one of those."""

    home = Path.home()
    for argv in (["/bin/sh"], ["git"], [str(Path(sys.executable))]):
        binding = program_binding(argv, spec=SandboxSpec(workdir=tmp_path))
        for path in binding.paths:
            assert path != home
            assert path not in home.parents
            assert path != Path(path.root)


def test_the_sandbox_path_and_the_resolution_path_are_the_same_string() -> None:
    """The binding layer resolves ``env`` through the PATH the sandbox will set.

    Two functions building that string separately is how a binding layer and
    the sandbox it prepares come to disagree about which file a command runs.
    """

    from research_os.sandbox import _sandbox_environment, _search_path

    for prefix in (None, "/opt/tools/bin"):
        built = _sandbox_environment(
            SandboxSpec(workdir=Path("/tmp")), {}, extra_path=prefix
        )
        assert [str(item) for item in _search_path(prefix)] == built["PATH"].split(":")


def test_a_program_that_is_not_on_the_host_path_binds_nothing(tmp_path: Path) -> None:
    """ "Not on PATH" is the caller's diagnosis and a better one than a bind."""

    binding = program_binding(
        ["definitely-not-a-program-here"], spec=SandboxSpec(workdir=tmp_path)
    )
    assert binding.paths == ()
    assert binding.path_prefix is None
    assert "not on this host's PATH" in binding.detail


def test_a_directory_or_a_data_file_is_never_bound(tmp_path: Path) -> None:
    """``argv[0] = "/"`` once bound the whole filesystem read-only."""

    data = tmp_path / "notes.txt"
    data.write_text("hello\n", encoding="utf-8")
    assert program_binding([str(data)], spec=SandboxSpec(workdir=tmp_path)).paths == ()
    assert (
        program_binding([str(tmp_path)], spec=SandboxSpec(workdir=tmp_path)).paths == ()
    )
    # Executable but not a regular file is still not a program.
    if Path("/dev/null").exists():
        assert (
            program_binding(["/dev/null"], spec=SandboxSpec(workdir=tmp_path)).paths
            == ()
        )


def test_environment_is_not_consulted_for_the_program_lookup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare ``argv[0]`` resolves against the *host* PATH the daemon has.

    That is the caller's PATH and the same one ``run_acceptance_command``
    already checks with ``shutil.which``; the point of pinning it is that the
    two must not diverge, because one deciding the command exists while the
    other decides where it is would bind a different file than the one checked.
    """

    planted = tmp_path / "bin"
    planted.mkdir()
    tool = planted / "plantedtool"
    tool.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    tool.chmod(0o755)
    workdir = tmp_path / "elsewhere"
    workdir.mkdir()
    monkeypatch.setenv("PATH", f"{planted}:{os.environ['PATH']}")

    binding = program_binding(["plantedtool"], spec=SandboxSpec(workdir=workdir))
    assert binding.paths == (tool,)
    assert shutil.which("plantedtool") == str(tool)


# ------------------------------------- 7. what two independent reviews found --
def test_a_pyvenv_cfg_cannot_name_the_filesystem_root_as_its_base(
    tmp_path: Path,
) -> None:
    """The CRITICAL finding of this release, as a regression.

    ``pyvenv.cfg`` sits in a directory a model has just written to, so its text
    is attacker input. ``pathlib`` does not normalise ``..``:
    ``Path("/etc/..")`` has ``.parent == /etc`` and ``.name == ".."``, so it
    passed every textual guard -- the home check, the exposure filter, the root
    check -- while the kernel resolved it to ``/``.

    Measured before the fix: ``--ro-bind /etc/.. /etc/..`` mounts the host root
    read-only inside the sandbox, and ``~/.ssh/id_ed25519`` was readable.
    """

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    for declared in (
        "/etc/../bin",
        "/etc/..",
        "/var/..",
        "/home/..",
        "/opt/../usr/bin",
        "/",
        str(Path.home()),
        str(Path.home() / ".local"),
    ):
        environment = worktree / f".venv-{abs(hash(declared))}"
        (environment / "bin").mkdir(parents=True)
        (environment / "pyvenv.cfg").write_text(
            f"home = {declared}\n", encoding="utf-8"
        )
        interpreter = environment / "bin" / "python"
        interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        interpreter.chmod(0o755)

        binding = program_binding(
            [str(interpreter)], spec=SandboxSpec(workdir=tmp_path / "elsewhere")
        )
        for path in binding.paths:
            assert ".." not in path.parts, (declared, path)
            assert path != Path("/"), declared
            assert path != Path.home(), declared
            assert path != Path.home() / ".local", declared


def test_a_runtime_root_is_never_one_of_this_systems_own_directories(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A marker file is positive evidence and it is not sufficient on its own.

    The only thing keeping ``~/.local`` out of the bind set used to be that no
    ``~/.local/pyvenv.cfg`` happens to exist on this machine -- and ``~/.local``
    is where this system keeps its database and its own containment record. An
    invariant held by an accident of host layout is not an invariant.

    **And an audit found the first version of this test resting on the same
    accident.** It asserted that those directories are not runtime roots on a
    host where none of them carries a marker -- so it passed with the denylist
    deleted, because the marker test alone answered False. The markers are
    planted here, so the denylist is the only thing that can produce the
    expected answer.
    """

    from research_os.sandbox import _is_runtime_root, _runtime_root

    home = tmp_path / "home"
    state = home / ".local" / "state" / "research-os"
    state.mkdir(parents=True)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: home))
    # `_forbidden_roots` asks `research_os.paths` where this system's own state
    # lives, and `conftest.py` redirects those into pytest's basetemp. Point
    # them at the fake home too, or the denylist is being asked about a
    # different machine than the one the test built.
    for variable, target in (
        ("RESEARCH_OS_STATE_HOME", state),
        ("RESEARCH_OS_DATA_HOME", home / ".local" / "share" / "research-os"),
        ("RESEARCH_OS_CONFIG_HOME", home / ".config" / "research-os"),
        ("RESEARCH_OS_CACHE_HOME", home / ".cache" / "research-os"),
    ):
        monkeypatch.setenv(variable, str(target))

    for forbidden in (
        home,
        home / ".local",
        home / ".config",
        home / ".cache",
        home / ".ssh",
        state,
    ):
        forbidden.mkdir(parents=True, exist_ok=True)
        # Plant the marker, so the only remaining reason to refuse is policy.
        (forbidden / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
        (forbidden / "bin").mkdir(exist_ok=True)
        assert (forbidden / "pyvenv.cfg").is_file()
        assert not _is_runtime_root(forbidden), forbidden
        assert _runtime_root(forbidden / "bin" / "python3") is None, forbidden

    # The control: an ordinary directory with the same marker *is* a runtime
    # root, so the refusals above are the denylist and not a broken marker test.
    ordinary = tmp_path / "project" / ".venv"
    (ordinary / "bin").mkdir(parents=True)
    (ordinary / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    assert _is_runtime_root(ordinary)
    assert _runtime_root(ordinary / "bin" / "python3") == ordinary


def test_the_filesystem_root_is_refused_even_carrying_a_marker() -> None:
    """`/lib/python3.*/os.py` exists on a usrmerged host, so `/` looks like one."""

    from research_os.sandbox import _is_runtime_root

    assert not _is_runtime_root(Path("/"))


def test_each_guard_on_a_declared_base_refuses_on_its_own(tmp_path: Path) -> None:
    """The end property is pinned; an audit asked whether the clauses are.

    Two independent guards close the critical defect -- `_declared_base` refuses
    a `..` segment, and `_absolute` normalises one away. Either alone is
    sufficient, which is the right design and means the end-to-end test cannot
    tell if one regresses. So each is exercised directly.
    """

    from research_os.sandbox import _absolute, _declared_base

    # `_absolute` normalises, so `/etc/..` can never leave this function as a
    # path with a `..` in it.
    assert _absolute(Path("/etc/..")) == Path("/")
    assert _absolute(Path("/usr/local/../bin")) == Path("/usr/bin")
    assert ".." not in _absolute(Path("/var/../etc/..")).parts

    # `_declared_base` refuses the segment outright, before normalisation.
    environment = tmp_path / ".venv"
    (environment / "bin").mkdir(parents=True)
    for declared in ("/etc/../bin", "/usr/../usr/bin", "/../usr"):
        (environment / "pyvenv.cfg").write_text(
            f"home = {declared}\n", encoding="utf-8"
        )
        assert _declared_base(environment) is None, declared

    # And the control: a real base installation is still accepted, so the
    # refusals above are the `..` clause rather than a function that says no.
    (environment / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    assert _declared_base(environment) == Path("/usr")


def test_a_worktree_pointer_cannot_name_a_repository_that_disowns_it(
    tmp_path: Path,
) -> None:
    """The HIGH finding of this release, as a regression.

    ``.git`` in a linked worktree is a *file*, inside a directory a contained
    acceptance command could write. The only policy was that the path it named
    ended in ``.git``, so pointing it at any other repository on the host --
    ``gitdir: /home/you/private-client-repo/.git`` -- exposed that repository's
    whole object store, its branches and its config, read-only, inside the
    sandbox.

    Git writes the reverse link when it creates a worktree. Forging one end is
    easy; forging both needs write access to the repository being aimed at,
    which is the access this is denying.
    """

    from research_os.sandbox import linked_worktree_paths

    victim = tmp_path / "someone-elses-repo"
    (victim / ".git").mkdir(parents=True)
    (victim / ".git" / "config").write_text("[remote]\n", encoding="utf-8")

    liar = tmp_path / "worktree"
    liar.mkdir()
    (liar / ".git").write_text(f"gitdir: {victim / '.git'}\n", encoding="utf-8")
    assert linked_worktree_paths(liar) == ()

    # A forged per-worktree directory whose back-pointer names someone else.
    forged = tmp_path / "forged" / ".git"
    forged.mkdir(parents=True)
    (forged / "gitdir").write_text(f"{tmp_path / 'other' / '.git'}\n", encoding="utf-8")
    (liar / ".git").write_text(f"gitdir: {forged}\n", encoding="utf-8")
    assert linked_worktree_paths(liar) == ()

    # And `..` in the pointer, which is how the sibling defect worked.
    (liar / ".git").write_text(
        f"gitdir: {victim / '.git' / '..' / '.git'}\n", encoding="utf-8"
    )
    assert linked_worktree_paths(liar) == ()


def test_a_real_worktree_still_reaches_its_own_repository(tmp_path: Path) -> None:
    """The control for the test above: the legitimate case must keep working."""

    from research_os.sandbox import linked_worktree_paths

    main = tmp_path / "main"
    main.mkdir()
    for args in (
        ["init", "-q", "--initial-branch=main", "."],
        ["config", "user.email", "t@example.invalid"],
        ["config", "user.name", "T"],
    ):
        subprocess.run(["git", *args], cwd=main, check=True, capture_output=True)
    (main / "f.txt").write_text("one\n", encoding="utf-8")
    subprocess.run(["git", "add", "-A"], cwd=main, check=True, capture_output=True)
    subprocess.run(
        ["git", "commit", "-qm", "init"], cwd=main, check=True, capture_output=True
    )
    worktree = tmp_path / "wt"
    subprocess.run(
        ["git", "worktree", "add", "-q", str(worktree), "-b", "task"],
        cwd=main,
        check=True,
        capture_output=True,
    )
    assert linked_worktree_paths(worktree) == (main / ".git",)


def test_a_crlf_shebang_fails_closed_rather_than_binding_the_wrong_file(
    tmp_path: Path,
) -> None:
    """The kernel does not treat a carriage return as whitespace, and neither may this.

    ``str.split(None, 1)`` does. So a file saved with CRLF endings made this
    resolve and bind ``/bin/sh`` while the kernel exec'd ``/bin/sh\\r`` and
    returned ENOENT naming the *script* -- reintroducing exactly the
    misdiagnosis this module exists to remove.
    """

    script = tmp_path / "run.sh"
    script.write_bytes(b"#!/bin/sh\r\necho hi\r\n")
    script.chmod(0o755)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(script)], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert "CRLF" in str(raised.value)


def test_a_spec_path_cannot_diverge_from_the_path_the_binding_resolved(
    tmp_path: Path,
) -> None:
    """A caller's ``PATH`` must not be able to select a different interpreter.

    ``spec.environment`` used to be applied last and win, so the PATH a command
    resolves ``env python3`` against *inside* the sandbox could differ from the
    one the binding layer resolved it against out here -- meaning the file bound
    and the file run could be two different files. The sandbox's PATH is now
    final.
    """

    from research_os.sandbox import _sandbox_environment, _search_path

    spec = SandboxSpec(workdir=tmp_path, environment={"PATH": "/attacker/bin"})
    built = _sandbox_environment(spec, {}, extra_path="/opt/tools/bin")
    assert "/attacker/bin" not in built["PATH"]
    assert [str(item) for item in _search_path("/opt/tools/bin")] == built[
        "PATH"
    ].split(":")


def test_a_missing_elf_loader_fails_closed_and_names_the_loader(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Binding a loader that is not there produces bubblewrap's own ENOENT.

    Which is the class of message this release set out to replace with the name
    of the file that is actually missing.
    """

    import research_os.sandbox as module

    missing = Path("/usr/lib/research-os-no-such-loader.so.2")
    monkeypatch.setattr(module, "_elf_loader", lambda _path: missing)

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            ["/bin/true"], spec=SandboxSpec(workdir=tmp_path), mode=SandboxMode.REQUIRED
        )
    assert str(missing) in str(raised.value)


# --------------------------------- 8. protected, and the failure path ---------
def test_protected_paths_are_bound_after_the_writable_ones(tmp_path: Path) -> None:
    """The ordering *is* the mechanism, and nothing tested it.

    ``protected`` exists because an adversarial review wrote a `post-checkout`
    hook into `.git/hooks` from inside "containment", got zero drift from
    `canonical_fingerprint`, and had it executed on the host by the next
    `git worktree add`. The guarantee is entirely "later binds win", and until
    now no test asserted the order.
    """

    checkout = tmp_path / "checkout"
    (checkout / ".git" / "hooks").mkdir(parents=True)
    (checkout / ".research").mkdir()

    flags = _flags(
        ["/bin/true"],
        SandboxSpec(
            workdir=checkout,
            protected=(checkout / ".git", checkout / ".research"),
        ),
    )
    writable_at = max(index for index, token in enumerate(flags) if token == "--bind")
    for guarded in (checkout / ".git", checkout / ".research"):
        at = [
            index
            for index, token in enumerate(flags)
            if token == "--ro-bind" and flags[index + 1] == str(guarded)
        ]
        assert at, f"{guarded} was never bound read-only"
        assert min(at) > writable_at, f"{guarded} is shadowed by the writable bind"


def test_only_protected_paths_that_exist_are_bound(tmp_path: Path) -> None:
    """A bind needs a source; an absent `.research` is not an error."""

    checkout = tmp_path / "checkout"
    (checkout / ".git").mkdir(parents=True)
    spec = SandboxSpec(
        workdir=checkout, protected=(checkout / ".git", checkout / ".research")
    )
    assert spec.resolved_protected() == (checkout / ".git",)


@needs_sandbox
def test_a_protected_git_directory_cannot_be_written_inside_a_writable_checkout(
    tmp_path: Path,
) -> None:
    """The escape that created `protected`, attempted for real.

    The checkout is writable -- it has to be, it is where an experiment's code
    and data live -- and the two directories inside it that decide what happens
    on the *host* afterwards are not.
    """

    checkout = tmp_path / "checkout"
    (checkout / ".git" / "hooks").mkdir(parents=True)
    (checkout / ".research").mkdir()
    hook = checkout / ".git" / "hooks" / "post-checkout"
    claim = checkout / ".research" / "CLAIM-0001.yaml"
    claim.write_text("status: draft\n", encoding="utf-8")

    prepared = contain(
        [
            "/bin/sh",
            "-c",
            (
                f"(printf '#!/bin/sh\\ntouch /tmp/pwned\\n' > {hook} && echo WROTE-HOOK "
                "|| echo denied-hook); "
                f"(echo accepted > {claim} && echo WROTE-CLAIM || echo denied-claim); "
                f"(echo ok > {checkout / 'data.txt'} && echo WROTE-DATA "
                "|| echo denied-data)"
            ),
        ],
        spec=SandboxSpec(
            workdir=checkout, protected=(checkout / ".git", checkout / ".research")
        ),
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
    seen = output(completed)
    assert "WROTE-HOOK" not in seen, seen
    assert "WROTE-CLAIM" not in seen, seen
    # The control: the rest of the checkout really is writable, so the two
    # denials above are the policy and not a sandbox that refuses everything.
    assert "WROTE-DATA" in seen, seen
    assert not hook.exists()
    assert claim.read_text("utf-8") == "status: draft\n"
    assert (checkout / "data.txt").read_text("utf-8") == "ok\n"


def test_a_preparation_failure_is_reported_against_the_command(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`run_acceptance_command` must not raise it out of the function.

    A host that cannot contain anything is a refusal no repair can change, and
    it keeps raising. A command whose closure could not be built is a fact about
    that command, and belongs on its result beside "not on PATH" -- with the
    missing file named, which is the whole point.
    """

    from research_os.automation.checks import run_acceptance_command
    from research_os.automation.models import AcceptanceCommand

    if BACKEND is None:
        pytest.skip("this host cannot contain, so preparation never runs")
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    script = worktree / "brokentool"
    script.write_text("#!/nonexistent/interpreter9\n", encoding="utf-8")
    script.chmod(0o755)
    # `run_acceptance_command` checks `which` before it builds a sandbox, and
    # the acceptance-command grammar takes only a bare program name -- so the
    # command has to be findable for the preparation layer to be reached at all.
    monkeypatch.setenv("PATH", f"{worktree}:{os.environ['PATH']}")

    result = run_acceptance_command(
        AcceptanceCommand(argv=["brokentool"], required=True),
        cwd=worktree,
        timeout_seconds=30,
        sandbox_mode=SandboxMode.REQUIRED,
        sandbox_readable=(),
    )
    assert result.exit_code is None
    assert result.error is not None
    assert "sandbox could not be prepared" in result.error
    assert "/nonexistent/interpreter9" in result.error


def test_a_preparation_failure_is_not_reported_as_containment_being_unavailable(
    tmp_path: Path,
) -> None:
    """`LocalExecutor` must tell the two apart.

    `ContainmentUnavailableError` is classified as a refusal; an executor fault
    is classified as `REPAIR` and puts the item back on the queue. Reporting a
    missing interpreter as "this host cannot contain" invites a repair worker to
    fix something no repair can change, and reporting it the other way invites
    one to react to a fact about the host.
    """

    from research_os.runtime.executors import LocalExecutor
    from research_os.runtime.interfaces import ExecutionSpec

    if BACKEND is None:
        pytest.skip("this host cannot contain, so preparation never runs")
    workdir = tmp_path / "project"
    workdir.mkdir()
    script = workdir / "run.py"
    script.write_text("#!/nonexistent/interpreter9\n", encoding="utf-8")
    script.chmod(0o755)
    run_dir = tmp_path / "run"
    (run_dir / "logs").mkdir(parents=True)

    executor = LocalExecutor()
    executor.sandbox_mode = SandboxMode.REQUIRED  # type: ignore[misc]
    handle = executor.submit(
        ExecutionSpec(name="broken", argv=(str(script),), cwd=str(workdir)),
        run_dir=run_dir,
    )
    assert handle.finished
    assert handle.exit_code is None
    assert "sandbox could not be prepared" in handle.detail
    assert "/nonexistent/interpreter9" in handle.detail


# ----------------------------- 9. what the second review found in the fixes --
def test_a_gitdir_inside_the_worktree_is_refused(tmp_path: Path) -> None:
    """The reverse link is not enough when the attacker writes both ends.

    A second review showed the first fix being satisfied by its own premise: a
    worker can copy a plausible per-worktree directory *into* the worktree,
    write ``gitdir`` so it agrees with the pointer, and then aim ``commondir``
    at any repository on the host. Git never puts a worktree's Git directory
    inside the worktree, and the shared directory always contains it.
    """

    from research_os.sandbox import linked_worktree_paths

    # The layout is *internally consistent* on purpose, so that the only reason
    # left to refuse is the clause this test is named for. An audit found the
    # first version of this fixture also violating the `commondir`-contains-
    # `gitdir` rule, which meant a different guard fired first and the test
    # would have passed with its own clause deleted.
    worktree = tmp_path / "worktree"
    forged = worktree / "fake" / ".git" / "worktrees" / "wt"
    forged.mkdir(parents=True)
    (worktree / ".git").write_text(f"gitdir: {forged}\n", encoding="utf-8")
    (forged / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (forged / "commondir").write_text("../..\n", encoding="utf-8")
    # Both ends agree and the `commondir` relation holds, exactly as git writes
    # it. It is still refused, because the Git directory is inside the worktree
    # -- which is the directory the contained command can write.
    assert linked_worktree_paths(worktree) == ()


def test_a_commondir_that_does_not_contain_the_worktree_directory_is_refused(
    tmp_path: Path,
) -> None:
    """Git's layout is ``<common>/worktrees/<name>``, and that is the check.

    The relation was already being computed, only to decide how many paths to
    return. Making it a requirement is what stops ``commondir`` naming an
    unrelated repository.
    """

    from research_os.sandbox import linked_worktree_paths

    victim = tmp_path / "victim"
    (victim / ".git").mkdir(parents=True)
    real = tmp_path / "elsewhere" / ".git" / "worktrees" / "wt"
    real.mkdir(parents=True)
    worktree = tmp_path / "worktree"
    worktree.mkdir()
    (worktree / ".git").write_text(f"gitdir: {real}\n", encoding="utf-8")
    (real / "gitdir").write_text(f"{worktree / '.git'}\n", encoding="utf-8")
    (real / "commondir").write_text(f"{victim / '.git'}\n", encoding="utf-8")

    assert linked_worktree_paths(worktree) == ()


def test_the_elf_loaders_own_symlink_chain_is_checked_hop_by_hop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Only the loader's first name used to be tested against the trusted set.

    ``_link_chain`` then followed up to forty links and appended every hop, so
    a loader inside a trusted directory could point anywhere. The loader now
    recurses through the same per-hop treatment the interpreter gets.
    """

    import research_os.sandbox as module

    worktree = tmp_path / "worktree"
    worktree.mkdir()
    outside = tmp_path / "outside" / "prize"
    outside.parent.mkdir()
    outside.write_bytes(Path("/bin/sh").resolve().read_bytes())
    outside.chmod(0o755)
    loader = worktree / "ld-fake.so.2"
    loader.symlink_to(outside)

    binary = worktree / "program"
    binary.write_bytes(Path("/bin/sh").resolve().read_bytes())
    binary.chmod(0o755)
    monkeypatch.setattr(
        module, "_elf_loader", lambda path: loader if path == binary else None
    )

    with pytest.raises(SandboxPreparationError) as raised:
        contain(
            [str(binary)], spec=SandboxSpec(workdir=worktree), mode=SandboxMode.REQUIRED
        )
    assert str(outside) in str(raised.value)


def test_an_absent_protected_path_is_documented_as_uncovered(tmp_path: Path) -> None:
    """The honest version of a trade, pinned so it cannot drift silently.

    A review found `.research` writable in projects that have no capsule yet.
    Mounting an empty read-only tmpfs there was implemented and reverted:
    bubblewrap creates the destination, the destination is inside the
    read-write worktree, and every contained check in every capsule-less
    project would leave an empty directory behind on the host. A sandbox that
    writes to the host is a worse defect than the one being closed.

    So the behaviour is: absent protected paths emit no flag at all, and
    nothing is created. If that ever changes, this test should be the thing
    that notices.
    """

    checkout = tmp_path / "checkout"
    checkout.mkdir()
    flags = _flags(
        ["/bin/true"],
        SandboxSpec(
            workdir=checkout, protected=(checkout / ".git", checkout / ".research")
        ),
    )
    for absent in (checkout / ".git", checkout / ".research"):
        assert str(absent) not in flags
    assert not (checkout / ".git").exists()
    assert not (checkout / ".research").exists()


def test_the_policy_digest_covers_every_module_that_shapes_the_spec() -> None:
    """A record must not outlive a change to what this system binds.

    Two reviews found this list short. It is asserted rather than described,
    because the failure mode is silent: the record simply stays valid.
    """

    from research_os.sandbox import _POLICY_SOURCES, _policy_identity

    root = Path(__file__).resolve().parent.parent / "src" / "research_os"
    for name in _POLICY_SOURCES:
        assert (root / name).is_file(), name
    for required in (
        "sandbox.py",
        "automation/checks.py",
        "runtime/executors.py",
        "paths.py",
        "automation/config.py",
    ):
        assert required in _POLICY_SOURCES, required
    assert len(_policy_identity()) == 16
