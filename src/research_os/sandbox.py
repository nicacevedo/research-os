"""OS-level containment for commands this system did not write.

**The problem, stated exactly.** The coding pipeline runs a project's acceptance
commands *after* a write-enabled worker has edited files in scope. So ``pytest``
imports and executes Python a model wrote one step earlier, with the
researcher's environment, their SSH agent, their Git credentials and their
provider keys. Worktree isolation protects the canonical checkout from the
*builder*; it has never been an OS sandbox, and `SECURITY.md` has always said
so. What R5 changed is that nobody decides to run it any more, which is a real
escalation of the same exposure.

``runtime/actions/coding.py`` *detects* the consequence: it hashes the canonical
capsule and every Git ref before and after and fails the action on drift. That
is worth having and it is not prevention. It sees nothing that happens outside
the repository -- a key read, a request sent, a file written in the researcher's
home.

**What this module is.** One abstraction, one place, behind the command runners.
It answers two questions and nothing else:

```text
probe()          which containment technology can this host actually provide?
contain(argv)    the argv that runs that command inside it
```

Everything else -- which paths a command may write, whether it may reach the
network, how long it may run -- is a :class:`SandboxSpec` the *caller* builds
from what it knows. No sandbox-specific flag appears anywhere outside this file,
which is the property that makes it replaceable: the day rootless Podman is
available, a backend is added here and no handler changes.

**Deny by default.** A contained command gets its worktree read-write, the
explicit inputs it was given read-only, an isolated temporary directory, and a
read-only operating system. It gets no home directory, no SSH keys, no SSH
agent, no Git credentials, no provider credentials, no unrelated environment and
no network. Network is a capability the caller must ask for, not a default it
must remember to remove.

**It refuses to pretend.** Every backend here either provides containment or
reports that it cannot. There is no "best effort" mode that runs the command
anyway and calls it contained, because a false claim of containment is worse
than an honest absence: the second is a known risk and the first is a wrong
belief that decisions get made on. ``bwrap --unshare-user-try`` is exactly that
shape and is deliberately not used.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path

from research_os.errors import ResearchOSError

LOG = logging.getLogger("research_os.sandbox")

#: Read-only operating-system paths a contained command needs to run anything.
#:
#: Bound read-only when present. Deliberately a fixed list rather than "/ minus
#: the interesting parts": an allowlist that omits something makes a command
#: fail, which is visible, and a denylist that omits something exposes it, which
#: is not.
_OS_PATHS: tuple[str, ...] = (
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/lib32",
    "/etc/alternatives",
    "/etc/ssl",
    "/etc/ca-certificates",
    "/etc/ca-certificates.conf",
    "/etc/resolv.conf",
    "/etc/nsswitch.conf",
    "/etc/passwd",
    "/etc/group",
    "/etc/localtime",
)

#: Environment variables a contained command is given. Everything else is
#: dropped, because the default has to be that a secret is absent.
#:
#: ``PATH`` is rewritten rather than inherited: an inherited PATH points at
#: directories that are not bound inside the sandbox, so a command would fail
#: with "not found" for a reason that has nothing to do with the sandbox.
_KEPT_ENVIRONMENT: tuple[str, ...] = ("LANG", "LC_ALL", "TZ", "TERM")

_SANDBOX_PATH = "/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin"


class SandboxError(ResearchOSError):
    """Raised when containment was required and could not be provided."""


class SandboxMode(StrEnum):
    """How much the researcher insists on containment.

    Three values because there are genuinely three positions, and collapsing
    any two of them loses something:

    ``required``
        Run contained or do not run. What high-autonomy execution of
        model-written code uses, because the person is not watching.

    ``preferred``
        Contain where the host can, and run uncontained with the absence
        *recorded* where it cannot. For a researcher at the keyboard on a host
        with no mechanism, which is a legitimate position and the common one.

    ``off``
        Do not contain. A deliberate choice a researcher may make, recorded on
        the run so it is never a surprise afterwards.
    """

    REQUIRED = "required"
    PREFERRED = "preferred"
    OFF = "off"


@dataclass(frozen=True, slots=True)
class SandboxSpec:
    """What one contained command may reach.

    Built by the caller from what it knows, never inferred here. A sandbox that
    guessed which paths a command needs would be a sandbox that guessed wrong
    in the direction of permitting.
    """

    #: The command's working directory. Bound read-write; almost always the
    #: isolated worktree.
    workdir: Path
    #: Additional read-write paths. The declared output path, and the tool
    #: caches a build needs to write (``uv``'s project environment).
    writable: tuple[Path, ...] = ()
    #: Explicit inputs, read-only. Input artifacts, a shared dependency cache.
    readable: tuple[Path, ...] = ()
    #: Network access, as a capability rather than a default.
    network: bool = False
    #: Wall-clock ceiling, enforced by the caller's own timeout as well.
    wall_seconds: int | None = None
    #: Ceiling on processes, where the backend can express one.
    max_processes: int | None = 512
    #: Extra environment the command needs, added to the kept allowlist.
    environment: Mapping[str, str] = field(default_factory=dict)

    def resolved_writable(self) -> tuple[Path, ...]:
        found = [self.workdir.resolve(), *(item.resolve() for item in self.writable)]
        return tuple(dict.fromkeys(found))

    def resolved_readable(self) -> tuple[Path, ...]:
        writable = set(self.resolved_writable())
        return tuple(
            dict.fromkeys(
                item.resolve()
                for item in self.readable
                if item.resolve() not in writable
            )
        )


@dataclass(frozen=True, slots=True)
class SandboxProbe:
    """Whether one containment technology can actually be used on this host.

    ``available`` is the result of *running* the technology, not of finding its
    binary. A present ``bwrap`` on a host whose kernel refuses unprivileged user
    namespaces is a binary that cannot contain anything, and reporting it as
    available is how a deployment ends up believing it is sandboxed.
    """

    technology: str
    executable: str | None
    available: bool
    detail: str
    remedy: str = ""


def probe_bubblewrap() -> SandboxProbe:
    """Probe bubblewrap by asking it to contain ``true``.

    Actually invoked, because the failure this catches is invisible to anything
    short of invocation. On Ubuntu 24.04 and later,
    ``kernel.apparmor_restrict_unprivileged_userns`` is 1 by default, so a
    non-setuid ``bwrap`` gets an unprivileged user namespace it has no
    capabilities in and ``setting up uid map`` fails. The binary is present, its
    ``--version`` works, and it cannot isolate a single path.
    """

    executable = shutil.which("bwrap")
    if executable is None:
        return SandboxProbe(
            technology="bubblewrap",
            executable=None,
            available=False,
            detail="bwrap is not on PATH",
            remedy="install bubblewrap (apt install bubblewrap)",
        )
    argv = [
        executable,
        "--unshare-user",
        "--unshare-net",
        "--ro-bind",
        "/usr",
        "/usr",
        "--proc",
        "/proc",
        "--",
        "/bin/true",
    ]
    try:
        completed = subprocess.run(
            argv,
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return SandboxProbe(
            technology="bubblewrap",
            executable=executable,
            available=False,
            detail=f"bwrap could not be run: {exc}",
        )
    if completed.returncode == 0:
        return SandboxProbe(
            technology="bubblewrap",
            executable=executable,
            available=True,
            detail="verified: an unprivileged user namespace with a uid map",
        )
    reason = (completed.stderr or completed.stdout or "").strip().splitlines()
    first = reason[0] if reason else f"exit {completed.returncode}"
    remedy = ""
    if "uid map" in first or "RTM_NEWADDR" in first or "userns" in first.lower():
        remedy = (
            "this kernel refuses unprivileged user namespaces. Check "
            "`sysctl kernel.apparmor_restrict_unprivileged_userns` and "
            "`cat /proc/sys/kernel/unprivileged_userns_clone`. A host "
            "administrator can permit it, install an AppArmor profile granting "
            "bwrap the `userns` permission, or install rootless Podman."
        )
    return SandboxProbe(
        technology="bubblewrap",
        executable=executable,
        available=False,
        detail=first,
        remedy=remedy,
    )


def probe_ineffective_systemd_run() -> SandboxProbe:
    """Record that ``systemd-run --user`` is not a containment mechanism here.

    Probed and reported because it *looks* like one, which is the dangerous
    property. ``systemd-run --user -P -p ProtectHome=tmpfs -p
    PrivateNetwork=yes`` starts the unit successfully and the contained process
    sees the real home directory and the real network: the user manager's
    namespacing needs the same unprivileged user namespaces the kernel is
    refusing, and it does not fail the unit when it cannot get them.

    So this backend is never selected. It exists so that "we looked at
    systemd-run" is a recorded measurement rather than an omission, and so that
    nobody wires it up believing the directives bind.
    """

    executable = shutil.which("systemd-run")
    if executable is None:
        return SandboxProbe(
            technology="systemd-run",
            executable=None,
            available=False,
            detail="systemd-run is not on PATH",
        )
    return SandboxProbe(
        technology="systemd-run",
        executable=executable,
        available=False,
        detail=(
            "present, and its user-scope sandboxing directives are silently "
            "ineffective without unprivileged user namespaces: the unit starts "
            "and ProtectHome and PrivateNetwork do not bind. Not used, because "
            "a containment that reports success without containing is worse "
            "than none"
        ),
        remedy="",
    )


def probe_container_runtimes() -> tuple[SandboxProbe, ...]:
    """Report rootless Podman and Docker as absent-or-present, never as used.

    Neither is implemented as a backend. ``ARCHITECTURE.md`` §12 keeps both on
    the postponed list, and adopting one needs the measured requirement that
    list asks for. They are probed so that a host that *has* one is a recorded
    fact and the next release has somewhere obvious to start.
    """

    found: list[SandboxProbe] = []
    for name in ("podman", "docker"):
        executable = shutil.which(name)
        found.append(
            SandboxProbe(
                technology=name,
                executable=executable,
                available=False,
                detail=(
                    f"{name} is present but no backend is implemented for it"
                    if executable
                    else f"{name} is not on PATH"
                ),
            )
        )
    return tuple(found)


def probe() -> tuple[SandboxProbe, ...]:
    """Probe every technology, best candidate first."""

    return (
        probe_bubblewrap(),
        probe_ineffective_systemd_run(),
        *probe_container_runtimes(),
    )


def available_backend() -> SandboxProbe | None:
    """The first technology that actually works here, or ``None``."""

    for candidate in probe():
        if candidate.available:
            return candidate
    return None


def unavailable_reason() -> str:
    """One line a person can act on, assembled from every probe."""

    parts = []
    for candidate in probe():
        line = f"{candidate.technology}: {candidate.detail}"
        if candidate.remedy:
            line += f" -- {candidate.remedy}"
        parts.append(line)
    return "; ".join(parts)


@dataclass(frozen=True, slots=True)
class ContainedCommand:
    """One command, and whether it is actually contained.

    ``contained`` is the field every caller must record. A command that ran
    uncontained under ``preferred`` is a legitimate outcome and an important
    one: it is the difference between "the tests passed in a sandbox" and "the
    tests passed with access to your SSH keys".
    """

    argv: tuple[str, ...]
    environment: dict[str, str]
    contained: bool
    technology: str
    detail: str


def contain(
    argv: Sequence[str],
    *,
    spec: SandboxSpec,
    mode: SandboxMode,
    environment: Mapping[str, str] | None = None,
) -> ContainedCommand:
    """Return the argv and environment that run ``argv`` under ``mode``.

    Raises :class:`SandboxError` when ``mode`` is ``required`` and no technology
    is available. Under ``preferred`` the command is returned uncontained with
    ``contained=False`` and the reason, and under ``off`` it is returned
    uncontained without probing at all.
    """

    base_environment = dict(environment or {})
    if mode is SandboxMode.OFF:
        return ContainedCommand(
            argv=tuple(argv),
            environment=base_environment,
            contained=False,
            technology="none",
            detail="containment is switched off by configuration",
        )

    backend = available_backend()
    if backend is None:
        reason = unavailable_reason()
        if mode is SandboxMode.REQUIRED:
            raise SandboxError(
                "this command runs code this system did not write, and the "
                "configured policy requires OS-level containment, which this "
                f"host cannot provide. {reason}. Either make a mechanism "
                "available, or set `sandbox.mode: preferred` in automation.yaml "
                "and accept that the command runs with your environment."
            )
        LOG.warning("running uncontained: %s", reason)
        return ContainedCommand(
            argv=tuple(argv),
            environment=base_environment,
            contained=False,
            technology="none",
            detail=reason,
        )

    if backend.technology == "bubblewrap":
        return _bubblewrap(
            argv, spec=spec, backend=backend, environment=base_environment
        )
    raise SandboxError(  # pragma: no cover - available_backend only returns known ones
        f"no containment implementation for {backend.technology}"
    )


def _sandbox_environment(
    spec: SandboxSpec, inherited: Mapping[str, str]
) -> dict[str, str]:
    """The complete environment a contained command gets.

    An allowlist, so a variable nobody thought about is absent rather than
    present. ``HOME`` points into the isolated temporary space: a command that
    writes a dotfile should write it somewhere that disappears, and one that
    reads the researcher's dotfiles should find nothing.
    """

    built = {
        "PATH": _SANDBOX_PATH,
        "HOME": "/tmp/sandbox-home",
        "TMPDIR": "/tmp",
        "USER": "sandbox",
        "LOGNAME": "sandbox",
    }
    for name in _KEPT_ENVIRONMENT:
        value = inherited.get(name) or os.environ.get(name)
        if value:
            built[name] = value
    built.update(dict(spec.environment))
    return built


def _bubblewrap(
    argv: Sequence[str],
    *,
    spec: SandboxSpec,
    backend: SandboxProbe,
    environment: Mapping[str, str],
) -> ContainedCommand:
    """Build the bubblewrap invocation.

    The flag order matters in one respect: later binds win, so the read-only
    operating-system binds come first and the caller's writable paths last. A
    writable path that happens to sit under ``/usr`` would otherwise be shadowed
    by the read-only bind of ``/usr``.
    """

    assert backend.executable is not None
    flags: list[str] = [
        backend.executable,
        # One flag, six namespaces, and `--unshare-all` includes the network.
        # Network is then added back only when the caller asked for it, so
        # forgetting to think about the network denies it.
        "--unshare-all",
        # The sandbox dies when this process does. Without it, a command that
        # forks and returns leaves children running with the bind mounts alive.
        "--die-with-parent",
        # A new session, so the contained process has no controlling terminal
        # and cannot push characters into the researcher's shell with TIOCSTI.
        "--new-session",
        "--clearenv",
    ]
    if spec.network:
        flags.append("--share-net")
    built_environment = _sandbox_environment(spec, environment)
    for name, value in sorted(built_environment.items()):
        flags.extend(["--setenv", name, value])

    flags.extend(["--proc", "/proc", "--dev", "/dev"])
    # An isolated temporary space, and a home inside it. Both vanish with the
    # sandbox, which is what makes "write a dotfile" harmless.
    flags.extend(["--tmpfs", "/tmp"])

    for path in _OS_PATHS:
        if Path(path).exists():
            flags.extend(["--ro-bind", path, path])

    for path in spec.resolved_readable():
        flags.extend(["--ro-bind", str(path), str(path)])
    for path in spec.resolved_writable():
        flags.extend(["--bind", str(path), str(path)])

    flags.extend(["--dir", "/tmp/sandbox-home"])
    flags.extend(["--chdir", str(spec.workdir.resolve())])
    flags.append("--")
    flags.extend(str(token) for token in argv)
    return ContainedCommand(
        argv=tuple(flags),
        # The environment inside the sandbox is set by `--setenv`, so the
        # *outer* process needs almost nothing. PATH so bwrap itself resolves.
        environment={"PATH": os.environ.get("PATH", _SANDBOX_PATH)},
        contained=True,
        technology=backend.technology,
        detail=(
            f"bubblewrap: {len(spec.resolved_writable())} writable, "
            f"{len(spec.resolved_readable())} read-only, network "
            f"{'granted' if spec.network else 'denied'}"
        ),
    )
