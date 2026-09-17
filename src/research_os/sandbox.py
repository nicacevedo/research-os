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
belief that decisions get made on.

Which is why the *probe* uses the strict ``--unshare-user`` rather than
``--unshare-all``. ``--unshare-all`` is documented as equivalent to
``--unshare-user-try ...``, and ``-try`` means "continue without a user
namespace if you cannot get one" -- exactly the shape this paragraph rejects.
The invocation adds ``--unshare-user`` after ``--unshare-all`` so the strict
flag re-asserts it, and the probe validates the strict form, so the probe is
never laxer than the use. An adversarial review found the docstring claiming
``-try`` was "deliberately not used" while ``--unshare-all`` quietly included
it.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from collections.abc import Callable, Mapping, Sequence
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
    # `/etc/ssl/certs` and the config, not `/etc/ssl` -- which on Debian and
    # Ubuntu contains `private/`. Root-owned and mode 0710 here, so not
    # exploitable on this host, but binding a directory of private keys into a
    # sandbox for no reason is exactly what the allowlist above exists to
    # prevent. An adversarial review noticed the contradiction.
    "/etc/ssl/certs",
    "/etc/ssl/openssl.cnf",
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
    #: Paths *inside* a writable one that must nevertheless stay read-only.
    #:
    #: The one thing `writable` cannot express, and an adversarial review showed
    #: it is not optional. A declared experiment runs in the project checkout
    #: because that is where its code and data are, so the checkout has to be
    #: writable -- and that made `.git/hooks` and `.research/` writable inside
    #: "containment". Writing a `post-checkout` hook produced *zero* drift from
    #: `canonical_fingerprint`, which hashes `.research/**` and `git show-ref`
    #: and not `.git/config` or `.git/hooks`; the next `git worktree add` then
    #: ran it on the host.
    #:
    #: Rendered as `--ro-bind` *after* the writable binds, because later binds
    #: win. That ordering is the mechanism.
    protected: tuple[Path, ...] = ()
    #: Network access, as a capability rather than a default.
    network: bool = False
    #: Wall-clock ceiling, enforced by the caller's own timeout as well.
    wall_seconds: int | None = None
    #: Ceiling on concurrent processes, enforced by the *caller* via RLIMIT.
    #:
    #: Bubblewrap has no process cap of its own -- `--unshare-pid` bounds
    #: visibility, not count -- so this is applied by
    #: :func:`process_limit_preexec`, which a caller passes to
    #: ``subprocess.run(preexec_fn=...)``. An adversarial review pointed out
    #: that the field previously produced no flag and no limit while reading
    #: like a guarantee.
    max_processes: int | None = 512
    #: Extra environment the command needs, added to the kept allowlist.
    environment: Mapping[str, str] = field(default_factory=dict)

    def resolved_writable(self) -> tuple[Path, ...]:
        found = [self.workdir.resolve(), *(item.resolve() for item in self.writable)]
        return tuple(dict.fromkeys(found))

    def resolved_readable(self) -> tuple[Path, ...]:
        """Read-only inputs, minus anything a writable bind would shadow.

        A readable path *under* a writable one is dropped, not silently
        upgraded: later binds win, so binding it read-only and then binding its
        parent read-write leaves it writable while the caller believes it
        declared otherwise. An adversarial review executed that. Anything that
        genuinely must stay read-only inside a writable tree belongs in
        :attr:`protected`, which is bound after.
        """

        writable = self.resolved_writable()
        found: list[Path] = []
        for item in self.readable:
            candidate = item.resolve()
            if any(
                candidate == parent or parent in candidate.parents
                for parent in writable
            ):
                LOG.warning(
                    "%s was declared readable but lies inside the writable %s; "
                    "dropping it rather than binding it read-only under a "
                    "read-write parent, which would leave it writable. Use "
                    "`protected` if it must stay read-only.",
                    candidate,
                    next(
                        parent
                        for parent in writable
                        if candidate == parent or parent in candidate.parents
                    ),
                )
                continue
            found.append(candidate)
        return tuple(dict.fromkeys(found))

    def resolved_protected(self) -> tuple[Path, ...]:
        """Read-only paths inside a writable tree. Only ones that exist."""

        return tuple(
            dict.fromkeys(item.resolve() for item in self.protected if item.exists())
        )


#: The version at or above which a technology is not known to be exploitable.
#:
#: One entry per technology, each naming the advisory it comes from, because a
#: floor with no citation is a number nobody can re-check when it moves.
#:
#: **bubblewrap 0.12.0** -- CVE-2026-87766 / GHSA-pxhw-h44j-8pfx. During sandbox
#: setup, creating files or directories under the new root can follow a parent
#: symlink onto the host via ``/oldroot``, writing attacker-chosen paths outside
#: the sandbox as the launching user. Every version below 0.12.0 is affected and
#: upstream fixed it in 0.12.0.
#:
#: That threat model is exactly this system's: the coding pipeline runs
#: acceptance commands over a worktree a model has just written to, so "attacker
#: controlled filesystem content" is the ordinary case rather than the exotic
#: one. A sandbox that can be made to write outside itself during *setup* is not
#: a weaker boundary here, it is the absence of one.
SECURITY_FLOORS: Mapping[str, tuple[tuple[int, ...], str]] = {
    "bubblewrap": ((0, 12, 0), "CVE-2026-87766 / GHSA-pxhw-h44j-8pfx"),
}


def parse_version(text: str) -> tuple[int, ...] | None:
    """The leading dotted-numeric version in ``text``, or ``None``.

    Tolerant of what a real ``--version`` prints: a program name in front, a
    distribution suffix behind. ``bubblewrap 0.9.0`` and ``0.12.0-1ubuntu1``
    both parse; a string with no numeric version yields ``None``, which every
    caller must treat as "unknown", never as "old enough".
    """

    match = re.search(r"(\d+(?:\.\d+)*)", text)
    if match is None:
        return None
    return tuple(int(part) for part in match.group(1).split("."))


@dataclass(frozen=True, slots=True)
class SandboxProbe:
    """What one containment technology can do here, in four separate answers.

    They were one boolean, and collapsing them is the specific mistake this
    class now cannot make. A present ``bwrap``, a working user namespace, a
    version that is not known-exploitable, and a containment boundary that has
    actually been attacked and held are four different facts, and a deployment
    that has the first three has *not* been shown to be contained.

    .. code-block:: text

        executable              the binary is on PATH
        namespaces_ok           it ran, and got a real namespace with a uid map
        security_eligible       its version is at or above the known floor
        containment_validated   the adversarial suite ran against it, and held

    :attr:`available` is the conjunction of the middle two, and is what
    :func:`available_backend` selects on. A vulnerable binary that creates
    perfectly good namespaces is ``namespaces_ok=True, security_eligible=False``
    -- present, and not an acceptable production backend. It is reported that
    way rather than as absent, because "we do not have bubblewrap" and "we have
    a bubblewrap we decline to trust" call for different actions from whoever
    reads the report.
    """

    technology: str
    executable: str | None
    namespaces_ok: bool
    detail: str
    remedy: str = ""

    version: str | None = None
    """What ``--version`` reported, verbatim. ``None`` when it was not asked."""

    setuid: bool | None = None
    """Whether the binary is setuid. ``None`` when it could not be stat'd.

    Recorded because the two ways to get a user namespace on a restricted host
    have opposite security properties, and a report that did not distinguish
    them would let a setuid sandbox pass as the non-setuid one. bubblewrap
    removed setuid support in 0.12.0, so on this technology a binary that is
    both setuid and at the floor is a contradiction worth seeing.
    """

    security_eligible: bool = False
    security_detail: str = ""
    """Why the version is or is not acceptable, naming the advisory."""

    containment_validated: bool = False
    validation_detail: str = ""
    """Whether this exact binary has been attacked by the adversarial suite.

    Never inferred from the version. A backend can be at the security floor and
    still be misconfigured by the caller, and the only thing that establishes a
    boundary holds is trying to cross it.
    """

    @property
    def available(self) -> bool:
        """Whether this is an acceptable production containment backend.

        Deliberately excludes :attr:`containment_validated`. Validation is a
        property of a *deployment* having been tested, and gating ordinary
        operation on it would mean a fresh host could never run the suite that
        would validate it. What it must never exclude is
        :attr:`security_eligible`.
        """

        return self.namespaces_ok and self.security_eligible


def security_verdict(technology: str, version: str | None) -> tuple[bool, str]:
    """Whether a version clears this technology's known-security floor.

    Three answers, and the middle one is the one that matters. A version at or
    above the floor is eligible. A version below it is **not** eligible and is
    named with its advisory. A version that could not be determined is *also*
    not eligible, because "we could not tell" and "it is fine" are different
    facts and only one of them is a reason to run model-written code in it.

    A technology with no floor recorded is eligible: the floors table is a list
    of things known to be broken, not a list of things known to be good, and
    refusing everything unlisted would refuse podman for never having had a
    CVE entered here.
    """

    floor = SECURITY_FLOORS.get(technology)
    if floor is None:
        return True, "no known-security floor recorded for this technology"
    minimum, advisory = floor
    parsed = parse_version(version or "")
    if parsed is None:
        return False, (
            f"could not determine the version, so it cannot be shown to be at "
            f"or above {'.'.join(str(part) for part in minimum)} ({advisory}). "
            f"An undetermined version is treated as unsafe"
        )
    if parsed < minimum:
        return False, (
            f"{version} is below {'.'.join(str(part) for part in minimum)} and "
            f"is affected by {advisory}: during sandbox setup a parent symlink "
            f"can be followed out of the sandbox, writing attacker-chosen paths "
            f"on the host as the launching user. This runs acceptance commands "
            f"over a worktree a model has just written, so that is this "
            f"system's ordinary case. PRESENT_BUT_UNACCEPTABLE"
        )
    return True, (
        f"{version} is at or above "
        f"{'.'.join(str(part) for part in minimum)} ({advisory} fixed)"
    )


def _bwrap_version(executable: str) -> str | None:
    """What ``bwrap --version`` prints, or ``None`` if it cannot be asked."""

    try:
        completed = subprocess.run(
            [executable, "--version"],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        LOG.debug("bwrap --version failed: %s", exc)
        return None
    text = (completed.stdout or completed.stderr or "").strip()
    return text or None


def _is_setuid(executable: str) -> bool | None:
    """Whether the binary carries the setuid bit, or ``None`` if unknowable."""

    try:
        import stat

        return bool(os.stat(executable).st_mode & stat.S_ISUID)
    except OSError as exc:  # pragma: no cover - a binary that vanished
        LOG.debug("could not stat %s: %s", executable, exc)
        return None


def containment_root() -> Path:
    """Where records of passed adversarial containment runs live.

    A public root function, named the way every other writable family in this
    product names one, because ``tests/conftest.py`` inventories exactly those
    and redirects them under pytest's temporary root. A private helper here
    would have been a writable path the isolation guard did not know about --
    which is what the guard caught on the first run of this code, exactly as
    intended.
    """

    from research_os.paths import state_home

    return state_home() / "containment"


def _validation_stamp_path() -> Path:
    """The one file under :func:`containment_root` that holds the records."""

    return containment_root() / "validated.json"


def _binary_identity(executable: str, version: str | None) -> str:
    """What a validation record is keyed by.

    The binary's *content*, not its path. A validation earned by one
    ``/usr/bin/bwrap`` must not be inherited by a different binary that has
    since been installed at the same path -- which is exactly what an upgrade
    does, and exactly when a stale "validated" would be most misleading.
    """

    import hashlib

    digest = hashlib.sha256()
    try:
        with Path(executable).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return f"unreadable:{executable}:{version or 'unknown'}"
    return f"{digest.hexdigest()}:{version or 'unknown'}"


def record_containment_validation(
    executable: str, version: str | None, *, detail: str
) -> None:
    """Record that the adversarial suite ran against this binary and it held.

    Called by the adversarial containment suite, never by a probe. The
    separation is the point: a probe reports what it measured, and no amount of
    measuring a binary's version establishes that a boundary was attacked.
    """

    import json

    path = _validation_stamp_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    existing[_binary_identity(executable, version)] = detail
    path.write_text(json.dumps(existing, indent=2, sort_keys=True), encoding="utf-8")


def containment_validation(executable: str, version: str | None) -> tuple[bool, str]:
    """Whether this exact binary has a passing adversarial-suite record."""

    import json

    path = _validation_stamp_path()
    try:
        records = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False, (
            "the adversarial containment suite has not been run against this "
            "binary on this host, so containment is measured but not proven"
        )
    detail = records.get(_binary_identity(executable, version))
    if not detail:
        return False, (
            "no adversarial containment record for this exact binary. A record "
            "is keyed by the binary's content hash, so an upgrade correctly "
            "invalidates the previous one"
        )
    return True, str(detail)


def probe_bubblewrap() -> SandboxProbe:
    """Probe bubblewrap on four axes, and report them separately.

    **Namespaces are measured by running it**, because the failure is invisible
    to anything short of invocation. On Ubuntu 24.04 and later
    ``kernel.apparmor_restrict_unprivileged_userns`` is 1 by default, so a
    non-setuid ``bwrap`` gets an unprivileged user namespace it has no
    capabilities in and ``setting up uid map`` fails. The binary is present, its
    ``--version`` works, and it cannot isolate a single path.

    **Security eligibility is measured by version**, because that failure is
    invisible to invocation -- the vulnerable binary runs perfectly and contains
    perfectly, right up until the setup path is pointed at a symlink. A probe
    that only ran the thing would report it working, which is exactly what the
    advisory says it will do.

    Both are reported even when the first fails. A host whose kernel refuses
    namespaces *and* whose bwrap is vulnerable needs to know both, because
    fixing only the kernel would turn a binary that cannot escape into one that
    can.
    """

    executable = shutil.which("bwrap")
    if executable is None:
        return SandboxProbe(
            technology="bubblewrap",
            executable=None,
            namespaces_ok=False,
            detail="bwrap is not on PATH",
            remedy="install bubblewrap (apt install bubblewrap)",
        )

    version = _bwrap_version(executable)
    setuid = _is_setuid(executable)
    eligible, security_detail = security_verdict("bubblewrap", version)
    if eligible and setuid:
        # bubblewrap removed setuid support in 0.12.0. A binary claiming to be
        # at the floor while still setuid is not the binary the floor describes,
        # and the conservative reading of a contradiction is to decline.
        eligible = False
        security_detail = (
            f"{version} reports itself at or above the security floor, yet the "
            f"binary is setuid -- support for which upstream removed in 0.12.0. "
            f"The two cannot both be true, so this is not accepted"
        )

    common = {
        "technology": "bubblewrap",
        "executable": executable,
        "version": version,
        "setuid": setuid,
        "security_eligible": eligible,
        "security_detail": security_detail,
    }
    validated, validation_detail = containment_validation(executable, version)

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
            **common,
            namespaces_ok=False,
            detail=f"bwrap could not be run: {exc}",
            containment_validated=validated,
            validation_detail=validation_detail,
        )

    if completed.returncode == 0:
        detail = "an unprivileged user namespace with a uid map"
        remedy = ""
        if not eligible:
            # The dangerous state, and the one the report has to be loudest
            # about: everything works, and it must not be used.
            detail = (
                f"namespaces work, but this build is NOT security-eligible: "
                f"{security_detail}"
            )
            remedy = (
                "install a bubblewrap at or above the security floor, or a "
                "vendor package carrying the backport, before enabling "
                "containment. Do not install an AppArmor userns profile for a "
                "vulnerable binary: that grants it the namespace it needs and "
                "leaves the escape intact"
            )
        return SandboxProbe(
            **common,
            namespaces_ok=True,
            detail=detail,
            remedy=remedy,
            containment_validated=validated,
            validation_detail=validation_detail,
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
        if not eligible:
            remedy = (
                "two separate things are wrong here and the order matters. "
                f"First, {security_detail}. Second, this kernel refuses "
                f"unprivileged user namespaces. Granting the namespace before "
                f"replacing the binary would produce a working sandbox with a "
                f"known escape, which is worse than the present state -- so "
                f"replace the binary first"
            )
    return SandboxProbe(
        **common,
        namespaces_ok=False,
        detail=first,
        remedy=remedy,
        containment_validated=validated,
        validation_detail=validation_detail,
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
            namespaces_ok=False,
            detail="systemd-run is not on PATH",
        )
    return SandboxProbe(
        technology="systemd-run",
        executable=executable,
        namespaces_ok=False,
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
                namespaces_ok=False,
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


class _Unset:
    """Distinguishes "not probed yet" from "probed, and nothing works"."""


#: The probe's answer, computed once per process.
#:
#: Probing *runs* bubblewrap, which is a subprocess. A coding run executes
#: several acceptance commands and each one asks whether containment is
#: available, so without this a run pays a process spawn per command to
#: re-establish a fact about the kernel that cannot change underneath it.
#:
#: Per process rather than per call, and never persisted: a daemon restarted
#: after an administrator permitted user namespaces must see the new answer, and
#: it will, because it is a new process.
_BACKEND: SandboxProbe | None | _Unset = _Unset()


def available_backend(*, refresh: bool = False) -> SandboxProbe | None:
    """The first technology that actually works here, or ``None``.

    Cached per process; ``refresh=True`` re-probes, which only a test or a
    diagnostic wants.
    """

    global _BACKEND
    if refresh or isinstance(_BACKEND, _Unset):
        found: SandboxProbe | None = None
        for candidate in probe():
            if candidate.available:
                found = candidate
                break
        _BACKEND = found
    return _BACKEND if not isinstance(_BACKEND, _Unset) else None


def unavailable_reason() -> str:
    """One line a person can act on, assembled from every probe.

    A technology that runs but is not security-eligible says so explicitly
    rather than joining the "not available" list, because the two call for
    opposite actions: one wants the thing installed, the other wants the
    installed thing replaced.
    """

    parts = []
    for candidate in probe():
        line = f"{candidate.technology}: {candidate.detail}"
        if candidate.namespaces_ok and not candidate.security_eligible:
            line = (
                f"{candidate.technology}: PRESENT_BUT_UNACCEPTABLE -- "
                f"{candidate.security_detail}"
            )
        if candidate.remedy:
            line += f" -- {candidate.remedy}"
        parts.append(line)
    return "; ".join(parts)


def process_limit_preexec(
    spec: SandboxSpec, *, contained: bool
) -> Callable[[], None] | None:
    """A ``preexec_fn`` that caps a **contained** command's process count.

    Bubblewrap cannot express this, so the caller does, in the child between
    ``fork`` and ``exec``. Returns ``None`` when the spec sets no ceiling, when
    the platform has no ``RLIMIT_NPROC``, or when the command is **not**
    contained -- so a caller can pass the result straight through.

    **``contained=False`` returns ``None``, and that is the whole point of the
    argument.** ``RLIMIT_NPROC`` is counted per ``(user namespace, uid)``, not
    per process tree. Inside a bubblewrap sandbox the command has its own user
    namespace, so the count starts near zero and ``max_processes`` is a real
    per-sandbox ceiling. Outside one the count is the researcher's *entire*
    session -- every shell, editor, browser tab and language server they have
    open. On the machine this was found on, ``ps -u $USER -L | wc -l`` was 1119
    against a default ceiling of 512, so setting the limit meant the child could
    not create its first thread: ``uv`` aborted with ``SIGABRT`` before running
    anything, and seventeen tests that shell out to a real ``uv run`` failed
    with "required acceptance commands failed".

    This is the second time this repository has made this exact mistake. The
    first was ``LimitNPROC=256`` in a ``systemd-run`` probe, which produced
    "fork: Resource temporarily unavailable"; the lesson did not generalise
    because the second attempt was in different code. It is written down here
    now: **there is no per-process-tree process limit in POSIX rlimits.** A cap
    on process creation requires either a namespace of its own or a cgroup, and
    a uid-scoped rlimit applied to somebody's login session is not a sandbox
    control, it is a way to break their session.

    Within a sandbox the limit is still blunt -- it stops a fork bomb and would
    also stop a legitimate build that spawns more, hence the generous default --
    and it bounds runaway process creation rather than partitioning anything.
    """

    if spec.max_processes is None or not contained:
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX only
        return None
    ceiling = int(spec.max_processes)

    def apply() -> None:  # pragma: no cover - runs in the forked child
        _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        wanted = min(ceiling, hard) if hard > 0 else ceiling
        resource.setrlimit(resource.RLIMIT_NPROC, (wanted, hard))

    return apply


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
    spec: SandboxSpec,
    inherited: Mapping[str, str],
    *,
    extra_path: str | None = None,
) -> dict[str, str]:
    """The complete environment a contained command gets.

    An allowlist, so a variable nobody thought about is absent rather than
    present. ``HOME`` points into the isolated temporary space: a command that
    writes a dotfile should write it somewhere that disappears, and one that
    reads the researcher's dotfiles should find nothing.

    ``extra_path`` is prepended to PATH, and is how the directory of a
    bound-in program becomes reachable. See :func:`_program_binding`. The
    caller's own ``PATH``, if the spec sets one, still wins: ``spec.environment``
    is applied last, because a spec that names a PATH means it.
    """

    built = {
        "PATH": f"{extra_path}:{_SANDBOX_PATH}" if extra_path else _SANDBOX_PATH,
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


def _program_binding(
    argv: Sequence[str], *, spec: SandboxSpec
) -> tuple[tuple[str, ...], str | None]:
    """Read-only binds and a PATH prefix so ``argv[0]`` exists inside.

    An adversarial review ran the arithmetic that this function exists to fix.
    :data:`_SANDBOX_PATH` is the four standard system directories, and the
    read-only binds are :data:`_OS_PATHS`; ``uv`` -- the program every
    acceptance command in this repository starts with -- installs to
    ``~/.local/bin``, which is in neither. So containment did not deny the
    command, it made it *vanish*: ``exit 127``, "uv: not found", recorded as an
    acceptance failure of the code the worker had just written. Turning
    containment on would have failed every check in the repository and the
    failures would have looked like the project's.

    The binding is the resolved file, at its own path, and nothing else. Not the
    directory: ``~/.local/bin`` holds whatever else the researcher installed,
    and one program being reachable is the requirement. The returned prefix is
    that file's directory, prepended to PATH so a relative ``argv[0]`` resolves
    the same way it did outside.

    Nothing is bound when the program already resolves inside the sandbox --
    under :data:`_OS_PATHS` or under a path the caller made readable or
    writable -- so the common case adds no flags.
    """

    program = argv[0] if argv else ""
    if not program:
        return (), None
    # No `..` segment, ever. `Path("/work") / "../../etc/shadow"` resolves out
    # of the workdir, and `.resolve()` follows symlinks with no root check, so a
    # final adversarial review got `--ro-bind /etc/shadow` and `--ro-bind /` out
    # of this function by controlling argv[0]. On the acceptance path the
    # command policy's literal allowlist closed it; the local experiment
    # executor has no such policy.
    if ".." in Path(program).parts:
        return (), None
    if os.sep in str(program):
        # A path, which a relative one resolves against the *sandbox's* working
        # directory and not this process's. `./run.sh` means the same thing
        # inside as it does to the caller writing the spec, and resolving it
        # here against `os.getcwd()` would bind whatever happens to sit beside
        # the daemon.
        real = (spec.workdir / program).resolve()
    else:
        located = shutil.which(program)
        if not located:
            # Not resolvable out here either. The caller reports "not on PATH"
            # against the host, which is the accurate diagnosis; inventing a
            # bind for a path that does not exist would turn it into a bwrap
            # error.
            return (), None
        real = Path(located).resolve()
    # A regular, executable file. Not a directory (`argv[0] = "/"` bound the
    # whole filesystem read-only), not a device, not a dangling symlink.
    # Binding something this process cannot even execute buys nothing and is
    # exactly how an arbitrary path gets inside.
    if not real.is_file() or not os.access(real, os.X_OK):
        return (), None
    already = [Path(item) for item in _OS_PATHS]
    already.extend(spec.resolved_readable())
    already.extend(spec.resolved_writable())
    already.append(spec.workdir.resolve())
    for root in already:
        if real == root or root in real.parents:
            return (), None
    return ("--ro-bind", str(real), str(real)), str(real.parent)


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
        # `--unshare-all` is `--unshare-user-try`, which continues *without* a
        # user namespace if it cannot get one. This re-asserts the strict form,
        # so the sandbox either has its own user namespace or fails -- which is
        # what the probe validates.
        "--unshare-user",
        # The sandbox dies when this process does. Without it, a command that
        # forks and returns leaves children running with the bind mounts alive.
        "--die-with-parent",
        # A new session, so the contained process has no controlling terminal
        # and cannot push characters into the researcher's shell with TIOCSTI.
        "--new-session",
        "--clearenv",
    ]
    if spec.network:
        # `--share-net` is the *host* network namespace, not a filtered one:
        # bubblewrap has no packet filter and this build adds none. A command
        # granted network can therefore reach 127.0.0.1 as well as the
        # internet, which on a runtime host means the PostgreSQL port and any
        # local development server. An adversarial review found the previous
        # comment here claiming more than that -- "network is then added back"
        # reads like a dial with a middle setting, and there is no middle
        # setting. `network=True` is "this command is on the host's network";
        # the only containment for what it can reach is not granting it.
        flags.append("--share-net")
    program_bind, program_dir = _program_binding(argv, spec=spec)
    built_environment = _sandbox_environment(spec, environment, extra_path=program_dir)
    for name, value in sorted(built_environment.items()):
        flags.extend(["--setenv", name, value])

    flags.extend(["--proc", "/proc", "--dev", "/dev"])
    # An isolated temporary space, and a home inside it. Both vanish with the
    # sandbox, which is what makes "write a dotfile" harmless.
    flags.extend(["--tmpfs", "/tmp"])

    for path in _OS_PATHS:
        if Path(path).exists():
            flags.extend(["--ro-bind", path, path])
    # The program itself, when it lives outside the list above. Before the
    # caller's binds, so a caller who made its directory writable still wins.
    flags.extend(program_bind)

    for path in spec.resolved_readable():
        flags.extend(["--ro-bind", str(path), str(path)])
    for path in spec.resolved_writable():
        flags.extend(["--bind", str(path), str(path)])
    # Last, so they win. `.git` and `.research` inside a writable checkout are
    # the two things a declared experiment must not be able to touch, and
    # nothing earlier can express that.
    for path in spec.resolved_protected():
        flags.extend(["--ro-bind", str(path), str(path)])

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
            f"{len(spec.resolved_readable())} read-only, "
            f"{len(spec.resolved_protected())} protected, network "
            f"{'granted' if spec.network else 'denied'}"
        ),
    )
