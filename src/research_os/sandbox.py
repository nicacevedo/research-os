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


class SandboxPreparationError(SandboxError):
    """Raised when containment is available but *this command* cannot be set up.

    A different failure from its parent, and the distinction is the one that
    cost this release a day. :class:`SandboxError` means "this host cannot
    contain anything" -- nothing a repair worker does can change it, so the
    runtime classifies it as a refusal rather than a fault. This means "the
    host can contain, and the execution closure for this particular argv could
    not be built" -- a missing interpreter, a `#!` line naming a path policy
    will not expose, a symbolic-link cycle. That is a property of one command,
    it is reported against that command, and it names the exact file that was
    missing instead of letting the kernel report `No such file or directory`
    against a binary that is present.
    """


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
    #: Paths a command may write, whose writes are **thrown away**.
    #:
    #: Rendered as bubblewrap's ``--overlay-src`` plus ``--tmp-overlay``: the
    #: host directory is the lower layer, a tmpfs is the upper one, and the
    #: upper one vanishes with the sandbox. Reads see the host's content;
    #: writes appear to succeed and reach nothing.
    #:
    #: It exists for exactly one shape of problem, and `uv`'s package cache is
    #: it. A contained command is denied the network, so `uv run` can only
    #: install from a cache -- and a *read-only* cache does not work, measured:
    #: uv exits with ``Failed to initialize cache ... Permission denied``
    #: before running anything. The previous release removed the cache from the
    #: read-only set for that reason and recorded that uv would use the
    #: sandbox's own tmpfs ``HOME`` instead, "verified to work from empty with
    #: ``UV_OFFLINE=1``". That claim is false for any project with a
    #: dependency, and this release measured it: a cold cache with no network
    #: cannot install ``pytest``, so every ``uv run`` check failed. A lock file
    #: exists so that dependency resolution does not need the network; it does
    #: not conjure the wheels.
    #:
    #: The overlay gives uv a cache it can initialise, populated, and discarded
    #: -- verified: five packages installed offline with the network denied,
    #: and nothing new in the host cache afterwards. It is strictly safer than
    #: the read-write bind an earlier release shipped, which was a host
    #: code-execution escape, because no write survives the run to be executed
    #: by anything.
    discarded: tuple[Path, ...] = ()
    #: Network access, as a capability rather than a default.
    network: bool = False
    #: Wall-clock ceiling. **Recorded here, enforced by the caller**, which
    #: passes it to ``subprocess.run(timeout=...)``. Bubblewrap has no
    #: wall-clock flag and this module adds none, so nothing in the sandbox
    #: reads this field. The previous wording -- "enforced by the caller's own
    #: timeout *as well*" -- implied a second enforcement that does not exist,
    #: which is the same shape as the `max_processes` defect this file already
    #: documents having fixed once. Both callers do pass an equal `timeout`, so
    #: there is no live gap; the sentence was the defect.
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
            dict.fromkeys(_absolute(item) for item in self.protected if item.exists())
        )

    def resolved_discarded(self) -> tuple[Path, ...]:
        """Overlay sources. Only ones that exist: there is nothing to layer over
        a directory that is not there, and an absent cache is a cold cache
        rather than an error."""

        return tuple(
            dict.fromkeys(item.resolve() for item in self.discarded if item.exists())
        )


def linked_worktree_paths(workdir: Path) -> tuple[Path, ...]:
    """The repository directory a **linked Git worktree** cannot run without.

    The second defect this release found, and it is the same shape as the
    first. ``_program_binding`` was binding a program without the runtime its
    ``#!`` line needs; this is a *worktree* bound without the repository its
    ``.git`` names. The coding pipeline runs every acceptance command inside a
    ``git worktree add`` checkout, where ``.git`` is not a directory but a file
    holding one line:

    ```text
    gitdir: /home/you/project/.git/worktrees/task-0007
    ```

    That path is outside the worktree, so the sandbox denied it and every
    project whose checks include a ``git`` command failed with

    ```text
    fatal: not a git repository: /home/you/project/.git/worktrees/task-0007
    ```

    which reads like the project's own repository being broken. It is the same
    misdiagnosis as ``execvp pytest: No such file or directory`` for a pytest
    that exists: the file the message names is present, and the dependency it
    needs is the thing that is absent.

    **Read-only, and measured to be enough.** Git records where the shared
    object store lives in ``commondir``, and the per-worktree directory sits
    inside it, so one read-only path covers both. ``git status``, ``git diff
    --check HEAD``, ``git rev-parse`` and ``git show-ref`` were all run against
    a read-only bind and all succeeded -- git refreshes its index when it can
    and does without when it cannot. Nothing here is writable, so the
    adversarial property that ``git update-ref`` cannot move a canonical branch
    is unchanged, and ``.git/hooks`` stays as unwritable as
    :attr:`SandboxSpec.protected` makes it in the experiment path.

    **What this does expose**, stated plainly rather than left implicit: a
    contained acceptance command can *read* the repository the worktree came
    from -- its history, its other branches, and ``.git/config``. A remote URL
    with a token embedded in it would be readable, which is a reason not to
    keep one there and is now written down in `SECURITY.md`. It cannot write
    any of it.

    Returns nothing for an ordinary checkout, whose ``.git`` is a directory
    inside the workdir and needs no help, and nothing for a worktree whose
    pointer does not resolve -- the caller's own Git error is a better
    diagnosis than a bind of whatever the file happened to say.
    """

    pointer = workdir / ".git"
    try:
        if not pointer.is_file():
            return ()
        declared = pointer.read_text("utf-8", errors="replace").strip()
    except OSError:
        return ()
    prefix = "gitdir:"
    if not declared.startswith(prefix):
        return ()
    target = Path(declared[len(prefix) :].strip())
    if ".." in target.parts:
        return ()
    if not target.is_absolute():
        target = workdir / target
    try:
        gitdir = target.resolve()
        if not gitdir.is_dir():
            return ()
        # **The pointer file is inside a directory a model has just written to,
        # so it is attacker input, and `name == ".git"` was the entire policy.**
        # An adversarial review pointed at any other repository on the host --
        # `gitdir: /home/you/private-client-repo/.git` -- and got its whole
        # object store, its branches and its config bound read-only into the
        # sandbox. Every private repository on the machine, from a file the
        # contained command could write itself.
        #
        # Git records the reverse link when it creates a worktree: `gitdir`
        # inside the per-worktree directory holds the absolute path of the
        # `.git` file that points at it. Forging the pointer is easy; forging
        # *both ends* needs write access to the repository being aimed at,
        # which is the access this is trying to deny in the first place.
        back = gitdir / "gitdir"
        if not back.is_file():
            return ()
        claimed = Path(back.read_text("utf-8", errors="replace").strip())
        if claimed.resolve() != pointer.resolve():
            LOG.warning(
                "%s names %s as its Git directory, but %s points back at %s. "
                "Refusing to expose it: a worktree pointer is file content, and "
                "a repository that does not agree it owns this worktree is not "
                "this worktree's repository.",
                pointer,
                gitdir,
                back,
                claimed,
            )
            return ()
        # **The per-worktree directory must not live inside the worktree.**
        # A second review showed why the reverse link alone is not enough: a
        # worker can copy a plausible gitdir *into* the worktree, write both
        # ends of the link so they agree, and then point `commondir` at any
        # repository on the host. Everything the first fix checked was
        # satisfied, because the attacker wrote everything the first fix
        # checked. Git never puts a worktree's gitdir inside the worktree.
        resolved_workdir = workdir.resolve()
        if gitdir == resolved_workdir or resolved_workdir in gitdir.parents:
            LOG.warning(
                "%s names %s as its Git directory, which is inside the worktree "
                "itself. Git does not do that, and a worktree is writable by "
                "the thing being contained. Refusing to expose it.",
                pointer,
                gitdir,
            )
            return ()
        link = gitdir / "commondir"
        common = gitdir
        if link.is_file():
            named = Path(link.read_text("utf-8", errors="replace").strip())
            if ".." in named.parts and named.is_absolute():
                return ()
            common = (named if named.is_absolute() else gitdir / named).resolve()
        if not common.is_dir():
            common = gitdir
        # **And the shared directory must contain the per-worktree one.** Git's
        # layout is `<common>/worktrees/<name>`, always. This relation was
        # already computed a line below, but only to decide how many paths to
        # return -- making it a *requirement* is what stops `commondir` naming
        # an unrelated repository, which is the whole attack.
        if not (gitdir == common or common in gitdir.parents):
            LOG.warning(
                "%s names %s as the shared Git directory, which does not "
                "contain %s. Git's layout is <common>/worktrees/<name>; "
                "refusing to expose a repository that does not own this "
                "worktree.",
                link,
                common,
                gitdir,
            )
            return ()
    except OSError:
        return ()
    found = (
        [common] if gitdir == common or common in gitdir.parents else [common, gitdir]
    )
    # Never the home directory, never the filesystem root, and never anything
    # this system keeps its own state in.
    return tuple(
        item for item in found if item.name == ".git" and not _refused_as_root(item)
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
class NamespaceState(StrEnum):
    """What the namespace probe actually found, in three answers not two.

    The distinction this exists for cost an afternoon. The probe ran

        bwrap --unshare-user --unshare-net --ro-bind /usr /usr \
              --proc /proc -- /bin/true

    and reported any non-zero exit as "this kernel refuses unprivileged user
    namespaces". After an AppArmor profile correctly granted the namespace, it
    kept reporting a refusal -- because the real failure had become

        bwrap: execvp /bin/true: No such file or directory

    The namespace was created. The *sandbox* had no ``/bin`` and no
    ``/lib64``: this host is usrmerged, ``/bin`` and ``/lib64`` are symlinks
    into ``/usr``, and binding only ``/usr`` leaves neither present. ``execvp``
    then returns ENOENT for a missing **ELF interpreter** exactly as it does for
    a missing binary, so a filesystem mistake in the probe read as a kernel
    denial and sent an operator to look at AppArmor, which was working.

    Both non-success states still mean "not usable": what changes is which of
    them a person is told to go and fix.
    """

    AVAILABLE = "available"
    BLOCKED = "blocked"
    """The kernel or a security module refused the namespace. EPERM territory."""

    PROBE_ERROR = "probe_error"
    """The namespace was obtained and the probe itself failed afterwards.

    A bug here, or a host whose layout the probe's filesystem does not suit.
    Never evidence about the kernel's policy.
    """


#: stderr fragments that mean nested user namespaces could *not* be denied.
#:
#: Checked before everything else, because this failure is the exact inverse of
#: the one it would otherwise be read as, and the remedy is the opposite too.
#:
#: This release added ``--disable-userns --assert-userns-disabled`` to both the
#: probe and production, so that a host which silently ignores the
#: ``max_user_namespaces`` write fails closed instead of running every command
#: in a sandbox whose nested-namespace denial was a comment in this file. An
#: independent review then pointed out what the new failure *said*: the message
#: mentions user namespaces, `_NAMESPACE_DENIED_MARKERS` matches on exactly
#: that, and the operator was told "this kernel refuses unprivileged user
#: namespaces -- a host administrator can permit it, install an AppArmor
#: profile granting bwrap the `userns` permission". The kernel is not too
#: strict, it is too lax, and granting more namespace permission is the one
#: action that cannot help and that this module warns against elsewhere.
#:
#: Which is the misdiagnosis :class:`NamespaceState` was written to eliminate,
#: reintroduced by the two flags added to fix something else.
_USERNS_NOT_DISABLED_MARKERS: tuple[str, ...] = (
    "was not disabled as requested",
    "not disabled as requested",
    "max_user_namespaces",
    "disable-userns",
)

#: stderr fragments that mean the kernel or an LSM refused the namespace.
#:
#: Checked *after* the exec markers below, because a message can carry both and
#: "the namespace was created, then exec failed" is the more specific reading.
_NAMESPACE_DENIED_MARKERS: tuple[str, ...] = (
    "setting up uid map",
    "uid map",
    "gid map",
    "no permissions to create new namespace",
    "clone failed",
    "unshare failed",
    "user namespace",
    "userns",
    "operation not permitted",
    "rtm_newaddr",
)

#: stderr fragments that mean the sandbox was built and the sentinel did not run.
#:
#: ``execvp`` is the decisive one: bubblewrap only reaches it after every
#: namespace and every mount has succeeded, so its presence is positive
#: evidence that the namespace was obtained.
_PROBE_ERROR_MARKERS: tuple[str, ...] = (
    "execvp",
    "can't find",
    "can't create",
    "can't mkdir",
    "can't make",
    "no such file or directory",
)


def classify_namespace_failure(message: str) -> NamespaceState:
    """Read one bubblewrap error line as a denial or as a probe defect."""

    lowered = message.lower()
    if any(marker in lowered for marker in _USERNS_NOT_DISABLED_MARKERS):
        # Not a denial, and the opposite of one. Reported as a probe error
        # because the sandbox could not be built to policy, and the *remedy*
        # text is what matters: see `probe_bubblewrap`.
        return NamespaceState.PROBE_ERROR
    if any(marker in lowered for marker in _PROBE_ERROR_MARKERS):
        return NamespaceState.PROBE_ERROR
    if any(marker in lowered for marker in _NAMESPACE_DENIED_MARKERS):
        return NamespaceState.BLOCKED
    # Unrecognised. Not "blocked", because claiming a kernel policy we did not
    # observe is what this classifier exists to stop; and not available either.
    return NamespaceState.PROBE_ERROR


@dataclass(frozen=True, slots=True)
class SecurityFloor:
    """The upstream version that fixes an advisory, and how to name it."""

    minimum: tuple[int, ...]
    #: For a human reading a report.
    advisory: str
    #: The stable identifier :data:`VENDOR_FIXED_RANGES` is keyed by.
    key: str


SECURITY_FLOORS: Mapping[str, SecurityFloor] = {
    "bubblewrap": SecurityFloor(
        minimum=(0, 12, 0),
        advisory="CVE-2026-87766 / GHSA-pxhw-h44j-8pfx",
        key="CVE-2026-87766",
    ),
}


#: Vendor package versions in which an advisory is *known* to be fixed.
#:
#: Keyed by ``(os id, codename, package, advisory key)``, and the value is a
#: tuple of half-open intervals ``(fixed_from, reopened_at)`` in Debian version
#: order: fixed at ``fixed_from`` inclusive, and no longer fixed from
#: ``reopened_at`` inclusive. ``None`` means "and every version after".
#:
#: **Why intervals and not a floor.** A floor assumes vendor fix status is
#: monotonic in version, and on this very host it is not. Ubuntu noble's
#: bubblewrap went:
#:
#: ```text
#: 0.9.0-1ubuntu0.1   unfixed
#: 0.9.0-1ubuntu0.2   FIXED    -- USN-8779-1, two CVE-2026-87766 patches
#: 0.9.0-1ubuntu0.3   UNFIXED  -- "SECURITY REGRESSION: Incompatibility with
#:                                 Flatpak (LP: #2167621) - debian: Drop
#:                                 CVE-2026-87766"
#: ```
#:
#: The patches broke Flatpak's CUPS socket path resolution
#: (``containers/bubblewrap#801``, ``flatpak/flatpak#6830``) and Canonical
#: reverted them rather than hold the regression. So ``>= 0.9.0-1ubuntu0.2``
#: -- the obvious rule, and the one this table was nearly written as -- returns
#: true for a binary whose CVE fix was deliberately removed. That is the exact
#: false positive the eligibility gate exists to prevent, and it would have
#: been produced by trusting a version ordering instead of a changelog.
#:
#: **Positive evidence only.** Unlike :data:`SECURITY_FLOORS`, which lists what
#: is known broken so that an unlisted technology passes, this table lists what
#: is known *fixed* and anything unlisted does not pass. The polarity is
#: inverted deliberately: the upstream floor has already declared this version
#: unsafe, so a vendor entry is a narrow exception, and an exception granted on
#: absence of evidence is not an exception, it is a hole. A future
#: ``0.9.0-1ubuntu0.4`` that restores the fix will pass only once somebody adds
#: it here, having read its changelog.
VENDOR_FIXED_RANGES: Mapping[
    tuple[str, str, str, str], tuple[tuple[str, str | None], ...]
] = {
    ("ubuntu", "noble", "bubblewrap", "CVE-2026-87766"): (
        ("0.9.0-1ubuntu0.2", "0.9.0-1ubuntu0.3"),
    ),
}


@dataclass(frozen=True, slots=True)
class VendorPackage:
    """The distribution package that owns a binary, as the package manager says.

    Every field comes from ``dpkg`` or ``/etc/os-release``, never from the
    program's own ``--version``. A binary can print whatever it likes; what it
    cannot do is forge its entry in the package database or the checksum the
    package recorded for it.
    """

    os_id: str
    codename: str
    release: str
    name: str
    version: str
    path: str


def dpkg_compare(left: str, operator: str, right: str) -> bool | None:
    """Compare two Debian versions with ``dpkg --compare-versions``.

    The real implementation rather than a reimplementation of it. Debian
    version ordering has epochs, tildes that sort *before* the empty string,
    and a digit/non-digit alternation rule, and a hand-written comparison gets
    one of those wrong eventually -- on a security decision.

    ``None`` when dpkg cannot be asked, which every caller must treat as
    "unknown", never as "satisfied".
    """

    executable = shutil.which("dpkg")
    if executable is None:
        return None
    try:
        completed = subprocess.run(
            [executable, "--compare-versions", left, operator, right],
            check=False,
            capture_output=True,
            stdin=subprocess.DEVNULL,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        LOG.debug("dpkg --compare-versions failed: %s", exc)
        return None
    # 0 is true, 1 is false; anything else is an error, not a false.
    if completed.returncode not in (0, 1):
        return None
    return completed.returncode == 0


def _os_release() -> dict[str, str]:
    """``/etc/os-release`` as a dict, or empty when it cannot be read."""

    values: dict[str, str] = {}
    try:
        text = Path("/etc/os-release").read_text(encoding="utf-8")
    except OSError:
        return values
    for line in text.splitlines():
        name, _, value = line.partition("=")
        if name and value:
            values[name.strip()] = value.strip().strip('"')
    return values


def detect_vendor_package(executable: str) -> VendorPackage | None:
    """Which distribution package owns this exact binary, if any.

    Three things have to hold, and the second is the one that matters for
    §5-style confusion: a hand-built ``/usr/local/bin/bwrap`` must never
    inherit the distribution package's patch status just because a package of
    that name is installed.

    1. the path resolves, and ``dpkg -S`` names a package that owns *it*;
    2. the file still matches the checksum that package recorded, so a
       replaced binary at a packaged path is not treated as packaged;
    3. the package is installed, with a version dpkg will report.

    ``None`` on any failure, including on a non-dpkg system. ``None`` means
    "no vendor evidence", and the caller falls back to the upstream rule.
    """

    query = shutil.which("dpkg-query")
    search = shutil.which("dpkg")
    if query is None or search is None:
        return None
    try:
        resolved = str(Path(executable).resolve())
    except OSError:  # pragma: no cover - resolve() does not raise on Linux
        return None

    try:
        owner = subprocess.run(
            [search, "-S", resolved],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        LOG.debug("dpkg -S failed: %s", exc)
        return None
    if owner.returncode != 0:
        # Not shipped by any package. A locally built binary, which is exactly
        # the case that must not inherit a package's backport status.
        return None
    line = (owner.stdout or "").strip().splitlines()
    if not line:
        return None
    name, _, owned_path = line[0].partition(": ")
    name = name.split(":")[0].strip()
    if owned_path.strip() != resolved or not name:
        return None

    if _package_file_modified(search, name, resolved):
        LOG.warning(
            "%s is owned by %s but no longer matches the checksum that package "
            "recorded; not treating it as vendor-packaged",
            resolved,
            name,
        )
        return None

    try:
        version = subprocess.run(
            [query, "-W", "-f=${Version}", name],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=15,
        )
    except (OSError, subprocess.SubprocessError) as exc:  # pragma: no cover
        LOG.debug("dpkg-query failed: %s", exc)
        return None
    if version.returncode != 0 or not (version.stdout or "").strip():
        return None

    release = _os_release()
    return VendorPackage(
        os_id=release.get("ID", "").lower(),
        codename=release.get("VERSION_CODENAME", "").lower(),
        release=release.get("VERSION_ID", ""),
        name=name,
        version=version.stdout.strip(),
        path=resolved,
    )


def _package_file_modified(dpkg: str, package: str, path: str) -> bool:
    """Whether dpkg reports this file as changed since the package installed it.

    Only a *positive* report counts. ``dpkg --verify`` exits non-zero both when
    a file has changed and when the package shipped no checksums, and treating
    the second as tampering would refuse perfectly good packages -- so the
    output is parsed for a line naming this path, rather than the exit code
    being trusted.
    """

    try:
        completed = subprocess.run(
            [dpkg, "--verify", package],
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        # **Unknown means modified.** Everything else in this file is
        # positive-evidence-only and this one inverted it: a `--verify` that
        # times out or cannot run returned "not modified", so a tampered binary
        # at a packaged path inherited the vendor backport's clean bill of
        # health. An independent review found the one fail-open branch in the
        # eligibility gate.
        LOG.info("could not verify %s against its package: %s", path, exc)
        return True
    for line in (completed.stdout or "").splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[-1] == path:
            return True
    return False


def vendor_backport_verdict(
    package: VendorPackage | None, advisory_key: str
) -> tuple[bool, str]:
    """Whether the vendor is known to have fixed ``advisory_key`` in this build.

    Returns ``(fixed, why)``. ``fixed`` is true only on positive evidence: a
    matching entry in :data:`VENDOR_FIXED_RANGES` and a dpkg comparison that
    actually ran.
    """

    if package is None:
        return False, "not installed from a distribution package"
    ranges = VENDOR_FIXED_RANGES.get(
        (package.os_id, package.codename, package.name, advisory_key)
    )
    if not ranges:
        return False, (
            f"no {advisory_key} backport is recorded for {package.name} on "
            f"{package.os_id} {package.codename}"
        )
    for fixed_from, reopened_at in ranges:
        at_or_after = dpkg_compare(package.version, "ge", fixed_from)
        if at_or_after is None:
            return False, "dpkg could not compare the package versions"
        if not at_or_after:
            continue
        if reopened_at is None:
            return True, f"{package.version} >= {fixed_from}"
        before_reopen = dpkg_compare(package.version, "lt", reopened_at)
        if before_reopen is None:
            return False, "dpkg could not compare the package versions"
        if before_reopen:
            return True, f"{package.version} in [{fixed_from}, {reopened_at})"
        return False, (
            f"{package.version} is at or above {reopened_at}, in which this "
            f"vendor REVERTED the {advisory_key} fix. {_revert_note(package)}"
        )
    return False, (
        f"{package.version} is below {ranges[0][0]}, the first version in "
        f"which this vendor shipped the {advisory_key} fix"
    )


def _revert_note(package: VendorPackage) -> str:
    """The sentence a person needs when a vendor has withdrawn a fix."""

    return (
        f"Read `zcat /usr/share/doc/{package.name}/changelog.Debian.gz | head`: "
        f"on this host it records 'SECURITY REGRESSION: Incompatibility with "
        f"Flatpak (LP: #2167621) - debian: Drop CVE-2026-87766'. A later "
        f"package that restores the fix must be added to VENDOR_FIXED_RANGES "
        f"after reading its changelog; it will not be trusted for being newer"
    )


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
    namespace_state: NamespaceState
    detail: str
    remedy: str = ""

    @property
    def namespaces_ok(self) -> bool:
        """Whether a real namespace with a uid map was obtained.

        A property rather than the stored field it used to be, so that the two
        non-success states cannot drift apart from it: BLOCKED and PROBE_ERROR
        are both "not usable", and only the advice differs.
        """

        return self.namespace_state is NamespaceState.AVAILABLE

    version: str | None = None
    """What ``--version`` reported, verbatim. ``None`` when it was not asked."""

    vendor_package: VendorPackage | None = None
    """The distribution package that owns the binary, if any.

    Recorded because on a distribution the *package* version, not the
    program's, is what carries a security backport -- and because "this binary
    is not owned by any package" is the fact that stops a locally built
    ``/usr/local/bin/bwrap`` inheriting a packaged one's patch status.
    """

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


def security_verdict(
    technology: str,
    version: str | None,
    package: VendorPackage | None = None,
) -> tuple[bool, str]:
    """Whether this build clears its technology's known-security floor.

    Two independent ways to clear it, checked in that order:

    1. **upstream** -- the program's own version is at or above the floor;
    2. **vendor backport** -- the binary is owned by a distribution package
       whose version appears in :data:`VENDOR_FIXED_RANGES` for this advisory.

    The second exists because a distribution can carry a fix without carrying
    the version number that fix arrived in upstream, and refusing every such
    build would refuse most correctly-patched Linux hosts. It is *only* reached
    when the upstream rule fails, it requires positive evidence, and it reads
    the package database rather than the program's ``--version`` -- a binary
    can print whatever it likes, and cannot forge its dpkg entry or the
    checksum the package recorded for it.

    A version that could not be determined is **not** eligible: "we could not
    tell" and "it is fine" are different facts and only one of them is a reason
    to run model-written code inside it.

    A technology with no floor recorded is eligible, because
    :data:`SECURITY_FLOORS` lists what is known broken rather than what is
    known good.
    """

    floor = SECURITY_FLOORS.get(technology)
    if floor is None:
        return True, "no known-security floor recorded for this technology"

    minimum = ".".join(str(part) for part in floor.minimum)
    parsed = parse_version(version or "")
    if parsed is not None and parsed >= floor.minimum:
        return (
            True,
            f"upstream {version} is at or above {minimum} ({floor.advisory} fixed)",
        )

    fixed, why = vendor_backport_verdict(package, floor.key)
    if fixed and package is not None:
        return True, (
            f"upstream {version or 'unknown'} is below {minimum}, and "
            f"{package.os_id} {package.release} ships the {floor.key} fix as a "
            f"vendor backport in {package.name} {package.version} ({why})"
        )

    if parsed is None:
        return False, (
            f"could not determine the upstream version, and no vendor backport "
            f"applies ({why}), so it cannot be shown to be at or above "
            f"{minimum} ({floor.advisory}). An undetermined version is treated "
            f"as unsafe"
        )
    return False, (
        f"upstream {version} is below {minimum} and is affected by "
        f"{floor.advisory}: during sandbox setup a parent symlink can be "
        f"followed out of the sandbox, writing attacker-chosen paths on the "
        f"host as the launching user. This runs acceptance commands over a "
        f"worktree a model has just written, so that is this system's ordinary "
        f"case. No vendor backport applies either: {why}. "
        f"PRESENT_BUT_UNACCEPTABLE"
    )


def security_basis(
    technology: str,
    version: str | None,
    package: VendorPackage | None,
    eligible: bool,
) -> tuple[tuple[str, str], ...]:
    """The eligibility decision as labelled fields, for a report to render.

    Separate from the prose in :func:`security_verdict` because a person
    auditing this wants to see the inputs laid out -- which binary, which
    package, which advisory -- rather than to parse them back out of a
    sentence.
    """

    floor = SECURITY_FLOORS.get(technology)
    rows: list[tuple[str, str]] = [("upstream_version", version or "unknown")]
    if package is not None:
        rows += [
            ("package", package.name),
            ("package_version", package.version),
            ("vendor", package.os_id or "unknown"),
            ("release", f"{package.release} / {package.codename}".strip(" /")),
        ]
    else:
        rows.append(("package", "none (not owned by a distribution package)"))
    if floor is not None:
        parsed = parse_version(version or "")
        if parsed is not None and parsed >= floor.minimum:
            state = "upstream_fixed"
        elif eligible:
            state = "vendor_backport_fixed"
        else:
            state = "affected"
        rows.append((floor.key, state))
    rows.append(("security_eligible", "yes" if eligible else "no"))
    return tuple(rows)


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


#: The modules whose source constitutes the containment policy.
#:
#: Relative to this package. `sandbox.py` builds the invocation; the other two
#: decide which paths go into the spec it is built from, and a record that
#: survived a change to either would be a claim about a sandbox nobody builds
#: any more.
_POLICY_SOURCES: tuple[str, ...] = (
    "sandbox.py",
    "automation/checks.py",
    "runtime/executors.py",
    # `paths.py` decides where this system's own state lives, and
    # `_forbidden_roots` reads it to keep those directories out of the bind
    # set. `config.py` carries `extra_readable` and `network`, which go into
    # the spec directly. A review found both missing: change either and the
    # mount surface moves with every existing record still standing.
    "paths.py",
    "automation/config.py",
    # `controller.py` decides `uv_project_environment` -- a read-write bind
    # outside every worktree -- and passes `sandbox_readable`, `sandbox_network`
    # and `sandbox_mode`. `worktree.py` decides where the writable workdir is.
    # A third review found both missing: change either and the mount surface
    # moves with every record still standing.
    "automation/controller.py",
    "automation/worktree.py",
)


def _policy_identity() -> str:
    """A digest of *this system's own* containment policy.

    The second half of what a validation record is about, and it was missing.
    A record keyed only by the bubblewrap binary says "these 27 escapes were
    attempted against this bwrap and failed" -- and then survives a change to
    the mount policy it was actually measuring. This release proved the point:
    :func:`program_binding` was rewritten to bind virtual environments and
    interpreter prefixes read-only, which is a change to the sandbox's
    filesystem surface, and the previous record went on reporting 27/27 against
    a surface that no longer existed. An adversarial result is about a boundary,
    not about a binary.

    The digest is over the source of the modules that *constitute* the policy.
    That is a proxy and it is deliberately the conservative one: a rewritten
    comment invalidates a record that was still true, which costs one test run,
    and no reachable change to the policy can leave a record standing. A digest
    of the rendered flags was considered and rejected for failing in the other
    direction -- it would not have noticed this release's change at all, because
    the flags are identical and the *paths they name* are what moved.

    **It is more than this module**, and an adversarial review is why. Half the
    mount policy is decided by the callers: `automation/checks.py` chooses what
    goes into ``readable``, ``writable``, ``discarded`` and ``protected``, and
    `runtime/executors.py` chooses the experiment path's. The worst defect this
    project has shipped -- uv's data directory in ``writable``, a host
    code-execution escape -- lived in the first of those. A digest that covered
    only this file would have let that exact regression return with every
    existing "containment validated" record still standing.
    """

    import hashlib

    digest = hashlib.sha256()
    for name in _POLICY_SOURCES:
        try:
            digest.update(name.encode())
            digest.update((Path(__file__).parent / name).read_bytes())
        except OSError:  # pragma: no cover - these ship with the package
            # Named, not collapsed. Returning one constant made every record
            # written while any policy file was unreadable share an identity --
            # and match every other such host.
            return f"unreadable-policy:{name}"
    return digest.hexdigest()[:16]


def _binary_identity(executable: str, version: str | None) -> str:
    """What a validation record is keyed by.

    The binary's *content*, not its path. A validation earned by one
    ``/usr/bin/bwrap`` must not be inherited by a different binary that has
    since been installed at the same path -- which is exactly what an upgrade
    does, and exactly when a stale "validated" would be most misleading.

    And the containment *policy* alongside it. See :func:`_policy_identity`:
    the binary is half of what the suite attacked, and a record that outlived
    the other half is a claim about a sandbox that is no longer the one being
    built.
    """

    import hashlib

    digest = hashlib.sha256()
    try:
        with Path(executable).open("rb") as handle:
            for chunk in iter(lambda: handle.read(1 << 20), b""):
                digest.update(chunk)
    except OSError:
        return f"unreadable:{executable}:{version or 'unknown'}:{_policy_identity()}"
    return f"{digest.hexdigest()}:{version or 'unknown'}:{_policy_identity()}"


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
            "no adversarial containment record for this exact binary and this "
            "exact containment policy. A record is keyed by the binary's "
            "content hash and by a digest of the policy that was attacked, so "
            "both an upgrade of the binary and a change to what this system "
            "binds correctly invalidate the previous one"
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
            namespace_state=NamespaceState.PROBE_ERROR,
            detail="bwrap is not on PATH",
            remedy="install bubblewrap (apt install bubblewrap)",
        )

    version = _bwrap_version(executable)
    setuid = _is_setuid(executable)
    package = detect_vendor_package(executable)
    eligible, security_detail = security_verdict("bubblewrap", version, package)
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
        "vendor_package": package,
        "setuid": setuid,
        "security_eligible": eligible,
        "security_detail": security_detail,
    }
    validated, validation_detail = containment_validation(executable, version)

    argv = _namespace_probe_argv(executable)
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
            namespace_state=NamespaceState.PROBE_ERROR,
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
            namespace_state=NamespaceState.AVAILABLE,
            detail=detail,
            remedy=remedy,
            containment_validated=validated,
            validation_detail=validation_detail,
        )

    reason = (completed.stderr or completed.stdout or "").strip().splitlines()
    first = reason[0] if reason else f"exit {completed.returncode}"
    state = classify_namespace_failure(first)

    if any(marker in first.lower() for marker in _USERNS_NOT_DISABLED_MARKERS):
        # The inverse of a denial, and it needs its own sentence. See
        # `_USERNS_NOT_DISABLED_MARKERS`: the generic probe-error remedy says
        # "a defect in the probe or an unusual host layout", and the denial
        # remedy advises granting `userns` -- which here would be granting more
        # of the thing that could not be taken away.
        remedy = (
            "this host cannot deny nested user namespaces to a contained "
            "command, so a contained process could create one and be root "
            "inside it. That is where published namespace escapes begin, and "
            "it is why the sandbox asserts the denial rather than requesting "
            "it. This is NOT a kernel refusing user namespaces and granting "
            "bwrap more namespace permission cannot help. Reproduce with: "
            + " ".join(argv)
        )
        return SandboxProbe(
            **common,
            namespace_state=NamespaceState.PROBE_ERROR,
            detail=(
                "bubblewrap could not confirm that nested user namespaces are "
                f"disabled inside the sandbox: {first}"
            ),
            remedy=remedy,
        )

    if state is NamespaceState.PROBE_ERROR:
        remedy = (
            "this is a defect in the probe or an unusual host layout, NOT a "
            "kernel policy: bubblewrap only reaches this point after every "
            "namespace and every mount has succeeded. Do not change AppArmor "
            "or sysctl in response to it. Reproduce with: " + " ".join(argv)
        )
    else:
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
                "First, this build is not security-eligible (see the sandbox "
                "line above for why). Second, this kernel refuses unprivileged "
                "user namespaces. Granting the namespace before replacing the "
                "binary would produce a working sandbox with a known escape, "
                "which is worse than the present state -- so replace the "
                "binary first. docs/CONTAINMENT_OPTIONS.md has the options"
            )
    return SandboxProbe(
        **common,
        namespace_state=state,
        detail=first,
        remedy=remedy,
        containment_validated=validated,
        validation_detail=validation_detail,
    )


def _namespace_probe_argv(executable: str) -> list[str]:
    """The smallest sandbox that can run a sentinel and still test what matters.

    **It reuses :data:`_OS_PATHS`**, which is what :func:`_bubblewrap` binds for
    a real contained command. That is the point: a probe that builds a
    *different* filesystem from production can pass while production fails, or
    -- as it did here -- fail while production would have worked. Binding the
    same set means the probe exercises the same mounts, and a host where one of
    them is missing is a host where the probe says so.

    ``--ro-bind / /`` would also have run the sentinel, and was rejected: it
    hides exactly the class of defect that caused this, because every path
    exists under it whether or not the production bind list is right.

    ``--unshare-user`` is the strict form, never ``--unshare-user-try``, so a
    host that cannot give a user namespace fails here rather than quietly
    running the sentinel outside one. ``--unshare-net`` is included because a
    contained command is denied the network by default and that denial is a
    namespace this must prove it can create.
    """

    flags = [
        executable,
        "--unshare-user",
        "--unshare-net",
        "--unshare-pid",
        # Production sets both, so the probe must too. A probe that omitted
        # them would report a host as able to contain while every real command
        # failed on `--assert-userns-disabled` -- the exact shape of defect the
        # `_OS_PATHS` paragraph above exists to prevent, in a different field.
        "--disable-userns",
        "--assert-userns-disabled",
        "--proc",
        "/proc",
    ]
    for path in _OS_PATHS:
        if Path(path).exists():
            flags.extend(["--ro-bind", path, path])
    flags.extend(["--", _probe_sentinel()])
    return flags


def _probe_sentinel() -> str:
    """A tiny executable the probe can run inside the sandbox.

    Resolved against the *host*, because the bind list above reproduces the
    host's layout inside the sandbox. ``/bin/true`` is listed first and is a
    symlink into ``/usr`` on a usrmerged host -- which is fine, because
    ``/bin`` is in :data:`_OS_PATHS` and bubblewrap resolves the source.

    The fallback matters on a host where ``true`` lives in only one of them.
    """

    for candidate in ("/bin/true", "/usr/bin/true"):
        if Path(candidate).exists():
            return candidate
    return "/bin/true"


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
            namespace_state=NamespaceState.PROBE_ERROR,
            detail="systemd-run is not on PATH",
        )
    return SandboxProbe(
        technology="systemd-run",
        executable=executable,
        namespace_state=NamespaceState.BLOCKED,
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
                namespace_state=NamespaceState.PROBE_ERROR,
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


_OVERLAY_SUPPORT: bool | _Unset = _Unset()


def overlay_available(executable: str, *, refresh: bool = False) -> bool:
    """Whether this host can give a contained command a throwaway overlay.

    ``--tmp-overlay`` needs unprivileged overlayfs inside a user namespace,
    which Linux has had since 5.11 and which a hardened kernel may still
    refuse. Measured once by running it, for the same reason the namespace
    probe runs bubblewrap rather than looking for it: the failure is invisible
    to anything short of invocation.

    A host without it is not an error. The caller's overlay paths are dropped,
    the command runs against an empty one, and the warning says so -- which for
    ``uv`` means a project that must resolve dependencies needs
    ``sandbox.network: true``. Failing every check instead would be a refusal
    made on behalf of a researcher whose only remaining option is one this file
    cannot take for them.
    """

    global _OVERLAY_SUPPORT
    if refresh or isinstance(_OVERLAY_SUPPORT, _Unset):
        # Built on the namespace probe's own invocation, so the sentinel has
        # the operating system it needs to run. Overlaying `/usr` *in place*
        # with nothing else bound removes the sentinel from the sandbox, and
        # the first version of this function did exactly that and concluded the
        # host had no overlay support -- measuring its own missing mount for
        # the second time in this file's history. The overlay therefore lands
        # somewhere that changes nothing about whether the sentinel runs.
        flags = _namespace_probe_argv(executable)
        source = "/usr" if Path("/usr").is_dir() else "/"
        at = flags.index("--")
        flags[at:at] = ["--overlay-src", source, "--tmp-overlay", "/overlay-probe"]
        try:
            completed = subprocess.run(
                flags,
                check=False,
                capture_output=True,
                text=True,
                timeout=20,
            )
            _OVERLAY_SUPPORT = completed.returncode == 0
            if not _OVERLAY_SUPPORT:
                LOG.info(
                    "this host cannot provide a throwaway overlay: %s",
                    (completed.stderr or "").strip() or "no reason given",
                )
        except (OSError, subprocess.SubprocessError) as exc:
            LOG.info("could not measure overlay support: %s", exc)
            _OVERLAY_SUPPORT = False
    return bool(_OVERLAY_SUPPORT)


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

    **And the limit must first clear the host's own task count.** This is the
    third time this repository has made a version of the mistake above, and the
    first two notes were still not enough, because they got the *timing* wrong.
    A ``preexec_fn`` runs in the child between ``fork`` and ``exec`` -- which is
    before ``bwrap`` runs at all, and therefore before the user namespace that
    makes the count "start near zero" exists. At the moment bubblewrap calls
    ``clone(CLONE_NEWUSER)`` the limit is still being checked against the
    researcher's session.

    ``RLIMIT_NPROC`` also counts *tasks*, not processes. On the machine this was
    found on that was 1163 threads against 254 processes, so a 512 ceiling made
    namespace creation itself fail:

    ```text
    bwrap: Creating new namespace failed: Resource temporarily unavailable
    ```

    Every contained command failed that way, and it was invisible for as long
    as the host could not create namespaces at all -- the containment tests
    skipped, so nothing executed this path. It surfaced the hour containment
    started working.

    So the ceiling is raised, in the parent, to clear the current task count
    with headroom. Inside the sandbox the uid maps to a fresh ``user_struct``
    whose count starts near zero, and the inherited limit becomes the real
    per-sandbox ceiling it was always meant to be -- just a larger number than
    the caller asked for, and :attr:`SandboxSpec.max_processes` is the floor of
    it rather than the value.
    """

    if spec.max_processes is None or not contained:
        return None
    try:
        import resource
    except ImportError:  # pragma: no cover - POSIX only
        return None

    # Computed in the *parent*. A `preexec_fn` runs after `fork` in a process
    # that may hold locks other threads were using, so it must do as close to
    # nothing as possible -- certainly not walk /proc.
    ceiling = max(int(spec.max_processes), _uid_task_count() + _NPROC_HEADROOM)

    def apply() -> None:  # pragma: no cover - runs in the forked child
        _soft, hard = resource.getrlimit(resource.RLIMIT_NPROC)
        wanted = min(ceiling, hard) if hard > 0 else ceiling
        resource.setrlimit(resource.RLIMIT_NPROC, (wanted, hard))

    return apply


#: Tasks of headroom above the host's current count, for the contained ceiling.
#:
#: The count is sampled once, before the command starts; the researcher's
#: desktop keeps creating threads while it runs, and a ceiling sampled exactly
#: at the current count would fail the moment a browser tab opened.
_NPROC_HEADROOM = 1024


def _uid_task_count() -> int:
    """How many tasks this uid currently owns, as ``RLIMIT_NPROC`` counts them.

    Tasks, not processes: the kernel checks the limit per ``clone``, so a
    process with forty threads costs forty. Reading ``/proc`` directly rather
    than shelling out to ``ps``, because this is on the path of every contained
    command.

    Returns 0 when it cannot be determined, which makes the ceiling exactly
    what the caller asked for -- the previous behaviour, and the right fallback
    for a platform whose ``/proc`` this does not understand.
    """

    try:
        uid = os.getuid()
        total = 0
        for entry in Path("/proc").iterdir():
            if not entry.name.isdigit():
                continue
            try:
                if entry.stat().st_uid != uid:
                    continue
                total += len(list((entry / "task").iterdir()))
            except OSError:
                # The process exited while we were looking at it.
                continue
        return total
    except OSError:  # pragma: no cover - /proc is present on Linux
        return 0


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


def _sandbox_path_value(extra_path: str | None) -> str:
    """The sandbox's ``PATH``, in one place.

    Both :func:`_sandbox_environment` and :func:`_search_path` derive from
    this. They used to build it separately, which is how a binding layer and
    the sandbox it prepares come to disagree about which file a command
    resolves to.
    """

    return f"{extra_path}:{_SANDBOX_PATH}" if extra_path else _SANDBOX_PATH


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
        "PATH": _sandbox_path_value(extra_path),
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
    # `PATH` is the sandbox's to set, and a caller does not get to overrule it.
    # It used to: `spec.environment` was applied last and won. That made the
    # PATH a command resolves `env python3` against inside the sandbox differ
    # from the one `_search_path` resolved it against out here -- so the file
    # that got bound and the file that would run could be two different files.
    # An adversarial review found it latent; no caller sets `PATH` today.
    built["PATH"] = _sandbox_path_value(extra_path)
    return built


# ------------------------------------------------- the execution closure --
#: How many bytes of a ``#!`` line the kernel reads. Linux 5.1 and later use
#: ``BINPRM_BUF_SIZE == 256`` and silently *truncate* anything longer, so a
#: line at the limit is refused rather than guessed at: the interpreter this
#: file would bind and the one the kernel would run could differ.
_SHEBANG_LIMIT = 256

#: How many interpreter hops to follow. A script whose interpreter is a script
#: is legal; eight of them is a loop somebody built on purpose.
_INTERPRETER_DEPTH = 8

#: How many symbolic links to follow, matching the kernel's own ``ELOOP``.
_LINK_DEPTH = 40

#: ``PT_INTERP`` -- the program header that names an ELF binary's loader.
_PT_INTERP = 3

#: Filenames that make a directory a self-describing *runtime root*.
#:
#: The distinction this list exists for is the whole security argument of
#: :func:`program_binding`. A program's parent directory is **not** its runtime:
#: ``uv`` lives in ``~/.local/bin`` beside forty other console scripts, and
#: ``~/.local`` holds ``share/`` and ``state/`` -- this system's own database
#: among them. Binding a parent because a program lives inside it is how a
#: sandbox grows to contain the thing it was protecting.
#:
#: A runtime root is instead a directory that *says* it is one, in a file the
#: runtime itself put there. ``pyvenv.cfg`` is written by every virtual
#: environment builder; ``lib/python3.*/os.py`` is the marker of an
#: installation prefix. Both are positive evidence, and a directory with
#: neither gets nothing bound but the executable file itself.
#:
#: Data rather than code so a second runtime can be added by adding a marker,
#: and so the list is readable as policy.
_RUNTIME_MARKERS: tuple[str, ...] = (
    # A Python virtual environment, written by `venv`, `virtualenv` and `uv`.
    "pyvenv.cfg",
    # A Python installation prefix. The glob is the version directory.
    "lib/python3.*/os.py",
)


@dataclass(frozen=True, slots=True)
class ProgramBinding:
    """What ``argv[0]`` needs in order to exist and to run inside the sandbox.

    :attr:`paths` are bound read-only at their own names, so a path resolves
    inside exactly as it does outside. :attr:`path_prefix` is prepended to the
    sandbox ``PATH`` when the program lives somewhere the sandbox does not
    already expose.
    """

    paths: tuple[Path, ...] = ()
    path_prefix: str | None = None
    detail: str = ""


def _exposed_paths(spec: SandboxSpec) -> tuple[Path, ...]:
    """Every host path this sandbox already shows at its own name."""

    found = [Path(item) for item in _OS_PATHS]
    found.extend(spec.resolved_readable())
    found.extend(spec.resolved_writable())
    found.append(spec.workdir.resolve())
    return tuple(found)


def _within(path: Path, roots: Sequence[Path]) -> bool:
    return any(path == root or root in path.parents for root in roots)


def _home() -> Path | None:
    try:
        return Path.home()
    except (RuntimeError, OSError):  # pragma: no cover - HOME is always set here
        return None


def _forbidden_roots() -> tuple[Path, ...]:
    """Directories that are never a runtime root, whatever markers they carry.

    The marker test is positive evidence and it is not enough on its own. A
    review pointed out that the *only* thing keeping `~/.local` out of the bind
    set was the accident that no `~/.local/pyvenv.cfg` happens to exist on this
    machine -- and `~/.local` is where this system keeps its own database and
    its own containment record. An invariant held by a coincidence of host
    layout is not an invariant.

    So the places this system itself writes are named, along with the obvious
    credential directories, and refused explicitly. Ancestors of them too: a
    root that *contains* the state directory exposes it just as completely as
    the state directory itself.
    """

    found: list[Path] = [Path("/")]
    home = _home()
    if home is not None:
        found.append(home)
        found.extend(
            home / name
            for name in (".local", ".config", ".cache", ".ssh", ".aws", ".gnupg")
        )
    try:
        from research_os import paths

        for accessor in (
            paths.state_home,
            paths.data_home,
            paths.config_home,
            paths.cache_home,
        ):
            try:
                found.append(Path(accessor()))
            except (OSError, AttributeError):  # pragma: no cover - defensive
                continue
    except ImportError:  # pragma: no cover - paths is part of this package
        pass
    return tuple(dict.fromkeys(found))


def _refused_as_root(candidate: Path) -> bool:
    """True when ``candidate`` is, or contains, somewhere that must never bind."""

    for forbidden in _forbidden_roots():
        if candidate == forbidden or candidate in forbidden.parents:
            return True
    return False


def _absolute(path: Path) -> Path:
    """Absolute, with the *directory* components resolved and the name kept.

    ``Path.resolve()`` would follow the final symlink too, which loses the name
    the program is actually invoked by -- and a console script that re-execs
    ``sys.executable`` needs that name to exist inside.
    """

    candidate = Path(path)
    if not candidate.is_absolute():
        candidate = Path.cwd() / candidate
    resolved = candidate.parent.resolve() / candidate.name
    # `pathlib` does not normalise `..`, and an adversarial review turned that
    # into the whole host filesystem: `Path("/etc/..")` has `.parent == /etc`
    # and `.name == ".."`, so it survived every textual guard below while the
    # kernel resolved it to `/`. Measured, `--ro-bind /etc/.. /etc/..` mounts
    # the host root read-only inside the sandbox and `~/.ssh/id_ed25519` is
    # readable. The final component is normalised here, once, so no caller can
    # forget: `/etc/..` becomes `/`, which the root guards already refuse.
    return Path(os.path.normpath(resolved))


def _link_chain(path: Path) -> tuple[Path, ...]:
    """Every name traversed from ``path`` to the file it finally denotes.

    Bubblewrap resolves the *source* of a ``--ro-bind`` on the host, so binding
    a symlink puts its target at the link's own name. Binding every name in the
    chain therefore makes the file reachable under each of them -- which is what
    ``<venv>/bin/python3 -> python -> <uv>/bin/python3.12`` needs, because a
    virtual environment's interpreter is found by its *link* name and Python
    then looks beside it for ``pyvenv.cfg``.

    Refuses on a cycle and refuses past the kernel's own ``ELOOP`` ceiling
    rather than binding whatever it happened to reach.
    """

    seen: list[Path] = []
    current = _absolute(path)
    for _ in range(_LINK_DEPTH):
        if current in seen:
            raise SandboxPreparationError(
                f"{path} resolves through a symbolic-link cycle at {current}; "
                "refusing to build an execution closure for it"
            )
        seen.append(current)
        try:
            target = os.readlink(current)
        except OSError:
            # Not a symlink, or gone. Either way this is the end of the chain.
            return tuple(seen)
        current = _absolute(
            Path(target) if Path(target).is_absolute() else current.parent / target
        )
    raise SandboxPreparationError(
        f"{path} resolves through more than {_LINK_DEPTH} symbolic links; "
        "refusing to build an execution closure for it"
    )


def _runtime_root(path: Path) -> Path | None:
    """The self-describing runtime an executable at ``path`` belongs to.

    ``<root>/bin/<name>`` where ``<root>`` carries one of
    :data:`_RUNTIME_MARKERS`. Everything else returns ``None``, and ``None``
    means "bind the file, nothing else" -- never "bind the parent".

    ``$HOME`` and the filesystem root are refused outright even if a marker
    were somehow present, because no correct answer here is ever one of those
    and a wrong one is the whole sandbox.
    """

    parent = path.parent
    if parent.name != "bin":
        return None
    root = parent.parent
    return root if _is_runtime_root(root) else None


def _is_runtime_root(root: Path) -> bool:
    """Does ``root`` declare itself a runtime, and is it allowed to be one?

    Split out from :func:`_runtime_root` because the same question is asked of
    a directory that was reached by *name* rather than by layout -- the ``home``
    line of a ``pyvenv.cfg`` -- and asking it two different ways is how the two
    answers come to differ.
    """

    if root == root.parent or _refused_as_root(root):
        return False
    for marker in _RUNTIME_MARKERS:
        try:
            if "*" in marker:
                head, _, tail = marker.partition("/")
                if any((root / head).glob(tail)):
                    return True
            elif (root / marker).is_file():
                return True
        except OSError:  # pragma: no cover - unreadable directory
            continue
    return False


def _declared_base(root: Path) -> Path | None:
    """The base installation a virtual environment names in ``pyvenv.cfg``.

    A virtual environment built with ``--copies`` has no symbolic link to
    follow, so the interpreter's own stdlib is reachable only through what the
    environment *declares*. That declaration is attacker-writable whenever the
    environment is, so the answer is accepted only if the directory it names is
    itself a runtime root: a ``home =`` pointing at ``~/.ssh`` names a
    directory with no ``lib/python3.*/os.py`` in it and is refused.
    """

    config = root / "pyvenv.cfg"
    try:
        text = config.read_text("utf-8", errors="replace")
    except OSError:
        return None
    for line in text.splitlines():
        key, sep, value = line.partition("=")
        if not sep or key.strip() != "home":
            continue
        base = Path(value.strip())
        if not base.is_absolute():
            return None
        # `pyvenv.cfg` is a file in a directory a model may have written to, so
        # its text is attacker input. A `..` segment in it is how a review got
        # `--ro-bind /etc/.. /etc/..` -- the host root -- out of this function.
        # Refused here as well as normalised in `_absolute`, because the two
        # guards fail differently and this one names the reason.
        if ".." in base.parts:
            return None
        if base.name == "bin":
            base = base.parent
        base = _absolute(base)
        return base if _is_runtime_root(base) else None
    return None


def _script_interpreter(path: Path) -> tuple[str, ...] | None:
    """The ``#!`` line of ``path`` split as the kernel splits it.

    ``None`` when the file has no ``#!``, which is not an error: ``execvp``
    falls back to ``/bin/sh`` for a file it cannot recognise, and ``/bin`` is
    already bound.

    Everything that *is* a ``#!`` and cannot be read as one raises. A malformed
    interpreter line is the case where guessing is worst: the guess decides
    which host file gets bound into a sandbox.
    """

    try:
        with path.open("rb") as handle:
            head = handle.read(_SHEBANG_LIMIT)
    except OSError as exc:
        raise SandboxPreparationError(
            f"{path} is executable but could not be read to determine how it "
            f"runs: {exc}"
        ) from None
    if not head.startswith(b"#!"):
        return None
    if b"\n" not in head and len(head) == _SHEBANG_LIMIT:
        raise SandboxPreparationError(
            f"the `#!` line of {path} is longer than the {_SHEBANG_LIMIT} bytes "
            "the kernel reads, so the interpreter it names and the one that "
            "would run are not the same thing; refusing to bind either"
        )
    line = head[2:].split(b"\n", 1)[0]
    if b"\0" in line:
        raise SandboxPreparationError(
            f"the `#!` line of {path} contains a NUL byte and does not name an "
            "interpreter"
        )
    try:
        text = line.decode("utf-8")
    except UnicodeDecodeError:
        raise SandboxPreparationError(
            f"the `#!` line of {path} is not valid UTF-8 and does not name an "
            "interpreter this can resolve"
        ) from None
    # The kernel skips *spaces and tabs* after `#!`, takes one token as the
    # interpreter, and passes the rest of the line as a single argument. It does
    # not treat a carriage return as whitespace. `str.split(None, 1)` does, so a
    # CRLF file made this function resolve and bind `/bin/sh` while the kernel
    # exec'd `/bin/sh\r` and returned ENOENT naming the script -- reintroducing
    # the exact misdiagnosis `SandboxPreparationError` exists to remove. An
    # adversarial review found it.
    text = text.strip(" \t")
    parts = [item for item in re.split(r"[ \t]+", text, maxsplit=1) if item]
    if parts and any(character.isspace() for character in parts[0]):
        raise SandboxPreparationError(
            f"the `#!` line of {path} names an interpreter containing "
            f"whitespace the kernel does not strip ({parts[0]!r}); the file "
            "is most likely saved with CRLF line endings"
        )
    if not parts:
        raise SandboxPreparationError(
            f"{path} begins with `#!` and names no interpreter"
        )
    return tuple(parts)


def _elf_loader(path: Path) -> Path | None:
    """The dynamic loader an ELF binary names in ``PT_INTERP``.

    ``None`` for a static binary, and ``None`` for anything that is not an ELF
    this understands -- a diagnosis, not a guess.

    This exists because of a specific afternoon. ``/usr/bin/true`` needs
    ``/lib64/ld-linux-x86-64.so.2``; ``/lib64`` is a usrmerge symlink; the
    namespace probe bound neither and ``exec`` returned ``ENOENT``, which was
    read as "this kernel refuses user namespaces" for hours. Reading the loader
    out of the header turns that class of failure into a sentence that names
    the missing file.
    """

    try:
        with path.open("rb") as handle:
            header = handle.read(64)
            if len(header) < 20 or header[:4] != b"\x7fELF":
                return None
            wide = header[4] == 2
            byteorder: str = "little" if header[5] == 1 else "big"
            if wide:
                if len(header) < 64:
                    return None
                offsets, entry_size, count = 0x20, 0x36, 0x38
                size = 8
            else:
                offsets, entry_size, count = 0x1C, 0x2A, 0x2C
                size = 4

            def read(at: int, width: int) -> int:
                return int.from_bytes(header[at : at + width], byteorder)  # type: ignore[arg-type]

            table = read(offsets, size)
            stride = read(entry_size, 2)
            headers = read(count, 2)
            if not table or not stride or not headers or headers > 512:
                return None
            handle.seek(table)
            blob = handle.read(stride * headers)
            for index in range(headers):
                entry = blob[index * stride : (index + 1) * stride]
                if len(entry) < stride:
                    break
                kind = int.from_bytes(entry[0:4], byteorder)  # type: ignore[arg-type]
                if kind != _PT_INTERP:
                    continue
                if wide:
                    start = int.from_bytes(entry[0x08:0x10], byteorder)  # type: ignore[arg-type]
                    length = int.from_bytes(entry[0x20:0x28], byteorder)  # type: ignore[arg-type]
                else:
                    start = int.from_bytes(entry[0x04:0x08], byteorder)  # type: ignore[arg-type]
                    length = int.from_bytes(entry[0x10:0x14], byteorder)  # type: ignore[arg-type]
                if not length or length > 4096:
                    return None
                handle.seek(start)
                raw = handle.read(length).split(b"\0", 1)[0]
                if not raw:
                    return None
                return _absolute(Path(raw.decode("utf-8", "replace")))
    except OSError:
        return None
    return None


def _search_path(prefix: str | None) -> tuple[Path, ...]:
    """The directories ``/usr/bin/env`` will search **inside** the sandbox.

    Derived from the same string :func:`_sandbox_environment` sets, so what
    this function resolves and what the sandbox resolves cannot drift apart.

    It is deliberately *not* the host's ``PATH``, and deliberately not
    :attr:`SandboxSpec.environment`'s either. A caller may set ``PATH`` in the
    spec and that value wins inside the sandbox -- but it must not decide which
    host file gets bound, because then a variable would choose what the sandbox
    exposes. A spec ``PATH`` can therefore make a command fail to find its
    interpreter, which is visible, and cannot make it find a different one,
    which would not be.
    """

    return tuple(Path(item) for item in _sandbox_path_value(prefix).split(":") if item)


def _resolve_env_shebang(
    tokens: tuple[str, ...], *, script: Path, search: Sequence[Path]
) -> Path:
    """Resolve ``#!/usr/bin/env python3`` without a shell and without the host.

    ``env`` is the one shebang form whose interpreter is chosen at run time, so
    it is the one that must be pinned down here. The rules are narrow on
    purpose: exactly one operand, a bare program name, no options, no variable
    assignments. ``env -S`` re-splits the line, ``env -i`` clears the
    environment, and ``env FOO=bar python3`` sets one -- each of those changes
    what runs, and none of them can be honoured by a function whose job is to
    say in advance which file that is.
    """

    if len(tokens) != 2:
        raise SandboxPreparationError(
            f"the `#!` line of {script} runs `env` with no interpreter to find"
        )
    wanted = tokens[1].strip()
    if wanted.startswith("-"):
        raise SandboxPreparationError(
            f"the `#!` line of {script} passes the option {wanted!r} to `env`. "
            "Options change which interpreter runs, and this resolves the "
            "interpreter before anything runs; use an explicit path instead."
        )
    if "=" in wanted:
        raise SandboxPreparationError(
            f"the `#!` line of {script} sets {wanted!r} through `env`. A "
            "contained command's environment is an allowlist built by the "
            "sandbox, not by the file being run."
        )
    if not wanted or os.sep in wanted:
        raise SandboxPreparationError(
            f"the `#!` line of {script} gives `env` {wanted!r}, which is not a "
            "bare program name"
        )
    for directory in search:
        candidate = directory / wanted
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return _absolute(candidate)
    raise SandboxPreparationError(
        f"the `#!` line of {script} runs `env {wanted}`, and {wanted} is on "
        "none of the directories the sandbox's own PATH searches "
        f"({':'.join(str(item) for item in search)}). The host's PATH is "
        "deliberately not consulted: an inherited PATH entry would let the "
        "environment choose which host file gets bound into the sandbox."
    )


def _close_over(
    path: Path,
    *,
    needed: list[Path],
    trusted: list[Path],
    search: Sequence[Path],
    depth: int,
) -> None:
    """Accumulate everything ``path`` needs in order to ``exec`` successfully."""

    if depth > _INTERPRETER_DEPTH:
        raise SandboxPreparationError(
            f"resolving how to run {path} passed through more than "
            f"{_INTERPRETER_DEPTH} interpreters; refusing to continue"
        )
    chain = _link_chain(path)
    for index, member in enumerate(chain):
        # **Every name in the chain is checked, not only the one that was
        # asked for.** The first version of this checked the requested path and
        # then bound wherever its links landed, and the test matrix executed
        # what that granted: a virtual environment is a trusted runtime root,
        # so replacing its `bin/python3` with a link to `~/.ssh/id_rsa` bound
        # that file, read-only, at its own name, inside the sandbox. A worktree
        # a model has just written to can contain a `pyvenv.cfg`, so the
        # environment being trusted is not a reason to trust where its links go.
        #
        # A hop may leave the trusted set only by landing in a *runtime root* --
        # a directory carrying one of `_RUNTIME_MARKERS`, written there by the
        # runtime itself. That is what `<venv>/bin/python3 -> <install>/bin/
        # python3.12` does and what a link to a private key cannot.
        if index and not _within(member, trusted) and _runtime_root(member) is None:
            raise SandboxPreparationError(
                f"{chain[0]} resolves through {member}, which is outside the "
                "operating system, outside what the caller declared, and is "
                "not itself part of a runtime. A symbolic link inside an "
                "exposed environment does not get to choose which other part "
                "of this host is bound into a sandbox."
            )
        needed.append(member)
        root = _runtime_root(member)
        if root is None or root in trusted:
            continue
        trusted.append(root)
        needed.append(root)
        base = _declared_base(root)
        # Subject it to the same test the `#!` interpreter and the ELF loader
        # get. A review found this was the one edge that went straight into the
        # bind set on the strength of a file's contents alone, and turned that
        # into `--ro-bind /etc/.. /etc/..`.
        if base is not None and base not in trusted and _is_runtime_root(base):
            trusted.append(base)
            needed.append(base)
    real = chain[-1]
    shebang = _script_interpreter(real)
    if shebang is None:
        loader = _elf_loader(real)
        if loader is None:
            return
        if not _within(loader, trusted):
            raise SandboxPreparationError(
                f"{real} is an ELF binary whose dynamic loader is {loader}, "
                "which is outside every path this sandbox exposes. Running it "
                "would fail with a bare `No such file or directory` naming the "
                "binary rather than the loader."
            )
        if not loader.is_file():
            # Binding a source that is not there produces `bwrap: Can't find
            # source path`, which is the class of message this whole function
            # exists to replace with the name of the missing file.
            raise SandboxPreparationError(
                f"{real} names the dynamic loader {loader}, and no such file "
                "exists on this host. The binary is present; the loader it "
                "needs is not."
            )
        # Recurse rather than `needed.extend(_link_chain(loader))`. A review
        # found that only the loader's *first* name was checked against the
        # trusted set while `_link_chain` then followed up to forty symbolic
        # links and appended every hop -- so a loader inside a trusted
        # directory could point anywhere. Recursing gives the loader's own
        # chain the identical per-hop treatment the interpreter's gets.
        _close_over(
            loader, needed=needed, trusted=trusted, search=search, depth=depth + 1
        )
        return
    named = shebang[0]
    if not named.startswith("/"):
        raise SandboxPreparationError(
            f"the `#!` line of {real} names the interpreter {named!r}, which is "
            "not an absolute path. The kernel does not search PATH for it, so "
            "there is nothing here to resolve."
        )
    interpreter = _absolute(Path(named))
    if interpreter.name == "env" and _within(
        interpreter, [Path(item) for item in _OS_PATHS]
    ):
        interpreter = _resolve_env_shebang(shebang, script=real, search=search)
    if not interpreter.is_file():
        raise SandboxPreparationError(
            f"{real} needs the interpreter {interpreter} named on its `#!` "
            "line, and no such file exists on this host. The command would "
            f"otherwise fail with `No such file or directory` naming {real}, "
            "which exists, and the missing file would never be named."
        )
    if not os.access(interpreter, os.X_OK):
        raise SandboxPreparationError(
            f"{real} names {interpreter} on its `#!` line, and that file is "
            "not executable. Binding it would put a file this system cannot "
            "even run inside the sandbox."
        )
    if not _within(interpreter, trusted):
        raise SandboxPreparationError(
            f"{real} names the interpreter {interpreter}, which lies outside "
            "the operating system, outside what the caller declared, and "
            f"outside {real.name}'s own runtime. A `#!` line is file content, "
            "and file content does not get to choose which part of this host "
            "is bound into a sandbox."
        )
    _close_over(
        interpreter, needed=needed, trusted=trusted, search=search, depth=depth + 1
    )


def _minimal(paths: Sequence[Path]) -> tuple[Path, ...]:
    """Drop any path already covered by another in the set, order preserved."""

    ordered = list(dict.fromkeys(paths))
    return tuple(
        item
        for item in ordered
        if not any(other != item and other in item.parents for other in ordered)
    )


def program_binding(argv: Sequence[str], *, spec: SandboxSpec) -> ProgramBinding:
    """The smallest read-only closure that lets ``argv[0]`` actually run.

    **The defect this replaced.** An earlier version bound the resolved
    executable *file* and nothing else, which is right for an ELF binary whose
    libraries live under ``/usr`` and wrong for everything else. Containment
    began really executing commands and 112 tests failed at once with

    ```text
    bwrap: execvp pytest: No such file or directory
    ```

    for a ``pytest`` that existed, was bound, and was reachable -- because
    ``<venv>/bin/pytest`` starts ``#!<venv>/bin/python3``, and the kernel's
    ``ENOENT`` for a missing *interpreter* names the script. The file was
    present and the thing that runs it was not.

    So the unit is not the file, it is the **execution dependency closure**:
    every host path the kernel and the program's own runtime must find between
    ``execvp`` and the program's first instruction. Four kinds of edge, and
    they compose:

    ```text
    symbolic links   every name in the chain, because a venv interpreter is
                     found by its link name and looks beside *that* for
                     `pyvenv.cfg`
    `#!` line        the interpreter, resolved and then closed over in turn
    `env`            the interpreter the *sandbox's own* PATH would find
    PT_INTERP        the ELF loader, so a missing one is named rather than
                     reported as a missing binary
    ```

    **What it will not do.** It never binds a directory because a program lives
    in it. ``uv`` lives in ``~/.local/bin`` beside forty console scripts, under
    a ``~/.local`` that holds this system's own state; ``<venv>/bin/pytest``
    lives inside a project checkout. The only directory that ever gets bound is
    a *runtime root* -- one carrying a :data:`_RUNTIME_MARKERS` file that the
    runtime itself wrote -- and then only the root, never its parent. A venv is
    exposed; the project around it is not.

    **And a `#!` line is untrusted input.** It is content in a file a model
    wrote one step earlier. An interpreter it names is honoured only inside the
    operating system allowlist, inside what the caller declared readable or
    writable, or inside the runtime root of the program being run. A script in
    the worktree naming ``/home/you/.ssh/bin/python`` is refused, and so is one
    naming an unrelated virtual environment.

    Everything is read-only. A caller that genuinely needs one of these paths
    writable -- ``uv`` materialising a project environment -- declares it in
    :attr:`SandboxSpec.writable`, and wins, because these binds are emitted
    before the caller's.
    """

    program = str(argv[0]) if argv else ""
    if not program:
        return ProgramBinding(detail="no program to bind")
    # No `..` segment, ever. `Path("/work") / "../../etc/shadow"` resolves out
    # of the workdir, and `.resolve()` follows symlinks with no root check, so a
    # final adversarial review got `--ro-bind /etc/shadow` and `--ro-bind /` out
    # of this function by controlling argv[0]. On the acceptance path the
    # command policy's literal allowlist closed it; the local experiment
    # executor has no such policy.
    if ".." in Path(program).parts:
        return ProgramBinding(detail=f"{program} contains a `..` segment")
    if os.sep in program:
        # A path, which a relative one resolves against the *sandbox's* working
        # directory and not this process's. `./run.sh` means the same thing
        # inside as it does to the caller writing the spec, and resolving it
        # here against `os.getcwd()` would bind whatever happens to sit beside
        # the daemon.
        candidate = _absolute(spec.workdir / program)
    else:
        located = shutil.which(program)
        if not located:
            # Not resolvable out here either. The caller reports "not on PATH"
            # against the host, which is the accurate diagnosis; inventing a
            # bind for a path that does not exist would turn it into a bwrap
            # error.
            return ProgramBinding(detail=f"{program} is not on this host's PATH")
        candidate = _absolute(Path(located))
    # A regular, executable file. Not a directory (`argv[0] = "/"` bound the
    # whole filesystem read-only), not a device, not a dangling symlink.
    # Binding something this process cannot even execute buys nothing and is
    # exactly how an arbitrary path gets inside.
    if not candidate.is_file() or not os.access(candidate, os.X_OK):
        return ProgramBinding(detail=f"{candidate} is not an executable file")

    exposed = _exposed_paths(spec)
    inside_already = _within(candidate, exposed)
    prefix = None if inside_already else str(candidate.parent)
    search = _search_path(prefix)
    trusted = list(exposed)
    if prefix is not None:
        # The sandbox prepends this directory to PATH, so by construction it is
        # where this program's own neighbours resolve from. Saying so here is
        # what keeps the binding and the run in agreement.
        trusted.append(Path(prefix))
    needed: list[Path] = []
    _close_over(candidate, needed=needed, trusted=trusted, search=search, depth=0)

    binds = _minimal([item for item in needed if not _within(item, exposed)])
    for item in binds:
        if _refused_as_root(item):
            raise SandboxPreparationError(
                f"building the execution closure for {program} produced "
                f"{item}, which is the filesystem root, the researcher's home, "
                "or somewhere this system keeps its own state. Refusing: no "
                "correct answer here is ever one of those, and a wrong one is "
                "the entire sandbox."
            )
    return ProgramBinding(
        paths=binds,
        path_prefix=prefix,
        detail=(
            f"{len(binds)} read-only path(s) for {program}"
            if binds
            else f"{program} already resolves inside the sandbox"
        ),
    )


def _program_binding(
    argv: Sequence[str], *, spec: SandboxSpec
) -> tuple[tuple[str, ...], str | None]:
    """:func:`program_binding` rendered as bubblewrap flags."""

    binding = program_binding(argv, spec=spec)
    flags: list[str] = []
    for path in binding.paths:
        flags.extend(["--ro-bind", str(path), str(path)])
    return tuple(flags), binding.path_prefix


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

    if backend.executable is None:  # pragma: no cover - probes always set it
        # Not `assert`: `python -O` strips it, and what follows would then put
        # `None` at argv[0] and raise `TypeError` where a refusal belongs.
        raise SandboxError(
            "the selected containment backend reported no executable to run"
        )
    # Bubblewrap binds a *source* that exists; there is no create-on-demand and
    # there must not be, because a sandbox that manufactures host directories
    # in order to contain something has a write side effect of its own. So the
    # missing path is named here, before anything runs.
    #
    # The third defect this release found. `uv` materialises its project
    # environment at a path the controller chooses and uv creates, which is
    # nothing at all until the first `uv run` -- and under containment that is
    # a `--bind` of a source that is not there. Every `uv run` check failed
    # with `bwrap: Can't find source path ...`, attributed to the project.
    missing = [path for path in spec.resolved_writable() if not path.exists()]
    if missing:
        raise SandboxPreparationError(
            "these paths were declared writable and do not exist: "
            + ", ".join(str(path) for path in missing)
            + ". A bind mount needs a source, and this does not create one: "
            "whoever owns the directory creates it before the command runs."
        )
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
        # `--unshare-all` includes `--unshare-cgroup-try`, and the module
        # docstring above argues that `-try` is "exactly the shape this
        # paragraph rejects". One was left. The cgroup namespace is not a
        # boundary this system relies on, so the impact was nil and the claim
        # was still broader than the flags.
        "--unshare-cgroup",
        # The sandbox dies when this process does. Without it, a command that
        # forks and returns leaves children running with the bind mounts alive.
        "--die-with-parent",
        # A new session, so the contained process has no controlling terminal
        # and cannot push characters into the researcher's shell with TIOCSTI.
        "--new-session",
        # No *further* user namespaces inside. An adversarial review recorded
        # that a contained process could still run `unshare --user
        # --map-root-user`, which is a nested namespace in which it is root --
        # the position from which every namespace escape published to date
        # starts. Nothing this system contains needs one: acceptance commands
        # are `uv`, `pytest` and `ruff`, and a declared experiment is a script.
        # bubblewrap implements it by writing 0 to `max_user_namespaces` in the
        # new namespace, so a nested `unshare` fails with ENOSPC.
        "--disable-userns",
        # And fail if that did not take. `--disable-userns` alone is a request;
        # this is the check. Without it a kernel that silently ignored the
        # write would run every command in a sandbox whose nested-namespace
        # denial was a comment in this file rather than a property of the host.
        "--assert-userns-disabled",
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

    # Only the read-only inputs that exist, exactly as `resolved_protected`
    # already does. An input that is absent exposes nothing, and the command's
    # own "no such file" is a better diagnosis of it than bubblewrap's.
    readable = [path for path in spec.resolved_readable() if path.exists()]

    # After the read-only operating system so it is not shadowed by it, and
    # before the caller's writable binds so a caller who declared one of these
    # genuinely writable still wins.
    requested = spec.resolved_discarded()
    overlays = requested if requested and overlay_available(backend.executable) else ()
    if requested and not overlays:
        LOG.warning(
            "this host cannot provide a throwaway overlay, so %s is not exposed "
            "and the command runs against an empty one. A `uv` command that "
            "must resolve dependencies will need `sandbox.network: true`.",
            ", ".join(str(path) for path in requested),
        )
    for path in overlays:
        flags.extend(["--overlay-src", str(path), "--tmp-overlay", str(path)])

    for path in readable:
        flags.extend(["--ro-bind", str(path), str(path)])
    for path in spec.resolved_writable():
        flags.extend(["--bind", str(path), str(path)])
    # Last, so they win. `.git` and `.research` inside a writable checkout are
    # the two things a declared experiment must not be able to touch, and
    # nothing earlier can express that.
    for path in spec.resolved_protected():
        flags.extend(["--ro-bind", str(path), str(path)])
    # **A protected path that does not exist is not covered, and that is a
    # deliberate trade rather than an oversight.** A review pointed out that
    # `.research` is left writable in exactly the projects that have no capsule
    # yet. The obvious repair -- mounting an empty read-only tmpfs at the path
    # -- was implemented and then reverted, because bubblewrap *creates* the
    # destination, and the destination is inside the read-write worktree, so
    # every contained acceptance command in every capsule-less project would
    # leave an empty `.research` directory behind on the host. A sandbox with a
    # write of its own is the thing this file refuses to be, and the risk being
    # closed is smaller than the one being introduced.
    #
    # The residual, stated so it is not rediscovered: a contained command can
    # create a capsule in a worktree that had none, and the diff archived for
    # the human is taken before the checks run, so it would not show it. The
    # worktree is disposable and its merge is a human decision; the *canonical*
    # capsule is covered by `canonical_fingerprint` either way.
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
            f"{len(readable)} read-only, "
            f"{len(spec.resolved_protected())} protected, "
            f"{len(program_bind) // 3} program, "
            f"{len(overlays)} discarded, network "
            f"{'granted' if spec.network else 'denied'}, nested user "
            "namespaces disabled"
        ),
    )
