"""Attack the sandbox, and report what actually held.

**Why this is a module and not a transcript.** The containment record this
system reads -- the one `researchctl runtime doctor` reports as "containment
validated" -- said that 27 adversarial checks had passed. Nothing in this
repository ran them. They had been executed by hand, once, and their result
written into ``~/.local/state/research-os/containment/validated.json``. A claim
about whether a boundary held existed only as a sentence somebody typed.

**What the record does and does not do.** It is reported; it is not a gate.
:meth:`~research_os.sandbox.SandboxProbe.available` is deliberately
``namespaces_ok and security_eligible`` and excludes it, because gating ordinary
operation on validation would mean a fresh host could never run the suite that
would validate it. An earlier draft of this docstring said
``available_backend()`` gated on the record. It does not, an independent audit
caught the sentence, and it is corrected here rather than quietly deleted:
a security document that overstates what enforces a property is the same defect
as a sandbox that overstates what it contains.

So the checks live here, they run, and the record is a product of running them.
:func:`audit` returns one :class:`Check` per attack with the evidence it
collected; ``researchctl runtime containment-audit`` runs it against the real
host and records the result; ``tests/test_sandbox_adversarial.py`` runs the
same function and fails if any check does not hold or does not execute.

**A skip is not a pass.** Every check reports ``held`` as a tri-state -- held,
failed, or could not run -- because "the suite was green" on a host where half
of it never executed is the failure this whole design exists to avoid.

**What this is not.** It is not a proof that the sandbox cannot be escaped. It
attacks the specific boundaries this system relies on, which is a lower bar and
a checkable one. CVE-2026-87766 is deliberately out of scope: this build is
patched against it rather than tested for it.
"""

from __future__ import annotations

import logging
import os
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path

from research_os.sandbox import (
    SandboxError,
    SandboxMode,
    SandboxPreparationError,
    SandboxSpec,
    available_backend,
    contain,
    linked_worktree_paths,
    overlay_available,
    process_limit_preexec,
    program_binding,
    record_containment_validation,
    unavailable_reason,
)

LOG = logging.getLogger("research_os.sandbox_audit")

#: How long any one attack may take before it is counted as not having run.
TIMEOUT_SECONDS = 120


@dataclass(frozen=True, slots=True)
class Check:
    """One attack, and whether the boundary it aimed at held.

    ``held is None`` means the check could not execute here. It is reported
    separately from a failure and it is *not* a pass: the caller decides what
    to do about a host where an attack could not be attempted, and the one
    thing it must not do is call that evidence.
    """

    name: str
    description: str
    held: bool | None
    evidence: str

    @property
    def status(self) -> str:
        if self.held is None:
            return "SKIP"
        return "PASS" if self.held else "FAIL"


class _Attacker:
    """Runs shell snippets inside the production sandbox adapter.

    The *production* one. A suite that built its own bubblewrap invocation
    would be testing a sandbox nothing uses, and could hold while the one the
    coding pipeline builds does not.
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.workdir = root / "worktree"
        self.workdir.mkdir(parents=True, exist_ok=True)

    def run(
        self,
        script: str,
        *,
        spec: SandboxSpec | None = None,
        network: bool = False,
    ) -> subprocess.CompletedProcess[str]:
        target = spec or SandboxSpec(workdir=self.workdir, network=network)
        prepared = contain(
            ["/bin/sh", "-c", script], spec=target, mode=SandboxMode.REQUIRED
        )
        if not prepared.contained:  # pragma: no cover - REQUIRED raises instead
            raise SandboxError("the adapter returned an uncontained command")
        return subprocess.run(
            list(prepared.argv),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            env=prepared.environment,
            start_new_session=True,
            preexec_fn=process_limit_preexec(target, contained=True),
        )

    def run_argv(
        self, argv: list[str], *, spec: SandboxSpec | None = None
    ) -> subprocess.CompletedProcess[str]:
        """Run a real program, so the executable closure is actually exercised."""

        target = spec or SandboxSpec(workdir=self.workdir)
        prepared = contain(argv, spec=target, mode=SandboxMode.REQUIRED)
        return subprocess.run(
            list(prepared.argv),
            check=False,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=TIMEOUT_SECONDS,
            env=prepared.environment,
            start_new_session=True,
            # Production passes this at every call site; a check that omitted
            # it would run the closure under a different process-limit regime
            # than the thing it is evidence about.
            preexec_fn=process_limit_preexec(target, contained=True),
        )

    def output(self, script: str, **kwargs: object) -> str:
        completed = self.run(script, **kwargs)  # type: ignore[arg-type]
        return (completed.stdout or "") + (completed.stderr or "")


def _host_unchanged(path: Path, before: str | None) -> bool:
    """Did the *host* file survive exactly as it was?

    The only question worth asking, and the first version of this file asked a
    different one. It failed a check because the contained command printed
    ``WROTE`` -- and the command was right: inside the sandbox ``/tmp`` is a
    fresh tmpfs, so the write succeeded against a filesystem that is destroyed
    when the command exits and that the host never sees.

    A command reporting success is a *proxy*. What the boundary promises is
    about the host, so the host is what gets read.
    """

    if before is None:
        return not path.exists()
    try:
        return path.read_text("utf-8") == before
    except OSError:
        return False


def _fingerprint(repo: Path) -> str:
    """Everything about a repository this system treats as canonical state.

    The capsule's bytes and every Git ref -- which is what
    ``canonical_fingerprint`` hashes in the runtime, so an audit that watched
    less than this would pass while the thing the runtime watches had moved.
    """

    import hashlib

    digest = hashlib.sha256()
    capsule = repo / ".research"
    if capsule.is_dir():
        for path in sorted(capsule.rglob("*")):
            if path.is_file():
                digest.update(str(path.relative_to(repo)).encode())
                digest.update(path.read_bytes())
    refs = subprocess.run(
        ["git", "show-ref"], cwd=repo, check=False, capture_output=True, text=True
    )
    digest.update(refs.stdout.encode())
    config = repo / ".git" / "config"
    if config.is_file():
        digest.update(config.read_bytes())
    return digest.hexdigest()


def _checks(attacker: _Attacker, canonical: Sequence[Path]) -> Iterator[Check]:
    yield from _filesystem_checks(attacker, canonical)
    yield from _closure_checks(attacker)
    yield from _secret_checks(attacker)
    yield from _isolation_checks(attacker)
    yield from _residue_checks(attacker)


def _closure_checks(attacker: _Attacker) -> Iterator[Check]:
    """Attack the executable-binding policy itself.

    **This section exists because it was missing, and two exploitable defects
    shipped through the gap.** Every other attack in this file runs
    ``/bin/sh -c``, and ``/bin/sh`` is inside :data:`~research_os.sandbox.
    _OS_PATHS`, so `program_binding` returns no binds and the entire executable
    closure -- the substance of this release -- was never once exercised by the
    suite that declared containment validated. An independent review then found,
    by reading, that a ``pyvenv.cfg`` line a model could write produced
    ``--ro-bind /etc/.. /etc/..``, which mounts the host root: measured, the
    researcher's SSH private key was readable inside the sandbox.

    So the policy is attacked here, with the attacker's write primitive where it
    really is -- inside the worktree, which is writable by design.
    """

    worktree = attacker.workdir
    home = Path.home()

    # A `#!` line naming a host path outside policy.
    outsider = attacker.root / "elsewhere" / "python3"
    outsider.parent.mkdir(parents=True, exist_ok=True)
    outsider.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    outsider.chmod(0o755)
    script = worktree / "hostile-shebang"
    script.write_text(f"#!{outsider}\n", encoding="utf-8")
    script.chmod(0o755)
    yield _refuses(
        "closure_shebang_outside_policy",
        "a `#!` line cannot name a host path the caller never declared",
        attacker,
        [str(script)],
        because="content does not get to choose",
    )

    # A virtual environment inside the worktree whose interpreter is a link to
    # a host secret. The environment is a legitimate runtime root; where its
    # links go is not the environment's to decide.
    hostile = worktree / ".venv-linked"
    (hostile / "bin").mkdir(parents=True, exist_ok=True)
    (hostile / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    # The target has to be *executable*, or the refusal comes from the
    # executability guard and the trust guard is never reached -- a review
    # showed the check passing with the policy it tests deleted. A copy of a
    # real interpreter, outside every declared path, is the honest target.
    prize = attacker.root / "elsewhere" / "stolen-interpreter"
    prize.parent.mkdir(parents=True, exist_ok=True)
    source = Path("/bin/sh").resolve()
    prize.write_bytes(source.read_bytes())
    prize.chmod(0o755)
    link = hostile / "bin" / "python3"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(prize)
    linked_script = hostile / "bin" / "tool"
    linked_script.write_text(f"#!{link}\n", encoding="utf-8")
    linked_script.chmod(0o755)
    yield _refuses(
        "closure_symlink_escape",
        "a symlink inside an exposed environment cannot redirect the binding",
        attacker,
        [str(linked_script)],
        must_not_bind=prize,
        because="exposed environment does not get to choose",
    )

    # A `pyvenv.cfg` whose declared base escapes through `..`. This is the one
    # that reached `--ro-bind /etc/.. /etc/..`.
    forged = worktree / ".venv-forged"
    (forged / "bin").mkdir(parents=True, exist_ok=True)
    (forged / "pyvenv.cfg").write_text("home = /etc/../bin\n", encoding="utf-8")
    interpreter = forged / "bin" / "python"
    interpreter.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    interpreter.chmod(0o755)
    binding = program_binding([str(interpreter)], spec=SandboxSpec(workdir=worktree))
    escaped = [
        path
        for path in binding.paths
        if ".." in path.parts or path == Path("/") or path == home
    ]
    yield Check(
        "closure_hostile_declared_base",
        "a `pyvenv.cfg` cannot name the filesystem root as its base installation",
        not escaped,
        f"binds produced: {[str(item) for item in binding.paths] or 'none'}",
    )

    # A forged worktree pointer aimed at another repository on this host.
    victim = attacker.root / "other-repo"
    (victim / ".git").mkdir(parents=True, exist_ok=True)
    (victim / ".git" / "config").write_text("[remote]\n", encoding="utf-8")
    liar = attacker.root / "lying-worktree"
    liar.mkdir(parents=True, exist_ok=True)
    (liar / ".git").write_text(f"gitdir: {victim / '.git'}\n", encoding="utf-8")
    exposed = linked_worktree_paths(liar)
    yield Check(
        "closure_forged_worktree_pointer",
        "a worktree pointer cannot name a repository that does not own it",
        not exposed,
        f"would have exposed: {[str(item) for item in exposed] or 'nothing'}",
    )

    # And the control: a real console script in a real environment must run.
    # Without it every refusal above is satisfied by a policy that refuses
    # everything, which would contain perfectly and be useless.
    real = _repository_console_script()
    if real is None:
        yield Check(
            "closure_console_script_runs",
            "a legitimate virtual-environment console script still executes",
            None,
            "no virtual-environment console script found to exercise",
        )
    else:
        completed = attacker.run_argv([str(real), "--version"])
        text = (completed.stdout or "") + (completed.stderr or "")
        yield Check(
            "closure_console_script_runs",
            "a legitimate virtual-environment console script still executes",
            completed.returncode == 0,
            f"{real.name} exited {completed.returncode}: {text.strip()[:80]}",
        )


def _repository_console_script() -> Path | None:
    """A real venv console script on this host, for the control above."""

    for candidate in (
        Path(sys.prefix) / "bin" / "pytest",
        Path(sys.prefix) / "bin" / "ruff",
    ):
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def _refuses(
    name: str,
    description: str,
    attacker: _Attacker,
    argv: list[str],
    *,
    must_not_bind: Path | None = None,
    because: str | None = None,
) -> Check:
    """The binding layer must refuse ``argv`` outright, before anything runs.

    ``because`` is a fragment the refusal's own message must contain. Without
    it a check passes on *any* ``SandboxPreparationError``, which is how one of
    these was found asserting a policy it never reached: the target was not
    executable, so the refusal came from a different guard entirely and the
    check would have passed with the tested policy removed.
    """

    try:
        binding = program_binding(argv, spec=SandboxSpec(workdir=attacker.workdir))
    except SandboxPreparationError as exc:
        if because is not None and because not in str(exc):
            return Check(
                name,
                description,
                False,
                f"refused, but for the wrong reason -- expected {because!r}, "
                f"got: {str(exc)[:120]}",
            )
        return Check(name, description, True, f"refused: {str(exc)[:110]}")
    bound = [str(item) for item in binding.paths]
    if must_not_bind is not None:
        held = not any(
            must_not_bind == item or item in must_not_bind.parents
            for item in binding.paths
        )
        return Check(
            name,
            description,
            held,
            f"did not refuse; bound {bound or 'nothing'} "
            f"(the target was {must_not_bind})",
        )
    return Check(
        name, description, False, f"did not refuse; bound {bound or 'nothing'}"
    )


def _filesystem_checks(
    attacker: _Attacker, canonical: Sequence[Path]
) -> Iterator[Check]:
    root = attacker.root

    for index, repo in enumerate(canonical):
        if not repo.is_dir():
            yield Check(
                f"canonical_repository_{index}",
                f"{repo} cannot be mutated from inside the sandbox",
                None,
                "no such directory on this host",
            )
            continue
        if not (repo / ".research").is_dir() and not (repo / ".git").exists():
            # An empty directory would pass every assertion below trivially --
            # identical fingerprint, no files created -- and `record()` would
            # then write a validation claiming the canonical boundary had been
            # attacked. A review found `--repo /var/tmp/anything` sufficient to
            # earn the record.
            yield Check(
                f"canonical_repository_{index}",
                f"{repo} cannot be mutated from inside the sandbox",
                None,
                f"{repo} has neither .research nor .git, so it is not a "
                "Research OS project and attacking it would establish nothing",
            )
            continue
        before = _fingerprint(repo)
        targets = [
            repo / ".research" / "AUDIT-PROBE.yaml",
            repo / ".git" / "hooks" / "post-checkout",
            repo / "AUDIT-PROBE.txt",
        ]
        script = "; ".join(
            f"(echo tampered > {target} && echo WROTE-{n} || echo denied-{n})"
            for n, target in enumerate(targets)
        )
        script += (
            f"; (cd {repo} && git update-ref refs/heads/audit-probe {'0' * 40} "
            "&& echo WROTE-REF || echo denied-ref)"
        )
        seen = attacker.output(script)
        after = _fingerprint(repo)
        stragglers = [target for target in targets if target.exists()]
        yield Check(
            f"canonical_repository_{index}",
            f"{repo.name} is byte-identical after being attacked",
            before == after and not stragglers,
            f"fingerprint unchanged={before == after}; files created={stragglers}; "
            f"{seen.strip()[:100]}",
        )

    # The same attack against fixtures under the audit root, which is where a
    # host with no canonical repository still gets the check run.
    fixture = root / "canonical-fixture"
    (fixture / ".research").mkdir(parents=True, exist_ok=True)
    claim = fixture / ".research" / "CLAIM-0001.yaml"
    claim.write_text("status: draft\n", encoding="utf-8")
    seen = attacker.output(f"echo tampered > {claim} && echo WROTE || echo denied")
    yield Check(
        "capsule_fixture_unwritable",
        "a capsule outside the worktree is unchanged on the host afterwards",
        _host_unchanged(claim, "status: draft\n"),
        f"host content preserved={_host_unchanged(claim, 'status: draft\n')}; "
        f"{seen.strip()[:80]}",
    )

    escape = root / "OUTSIDE-WORKTREE"
    escape.unlink(missing_ok=True)
    seen = attacker.output(f"echo tampered > {escape} && echo WROTE || echo denied")
    yield Check(
        "outside_worktree",
        "nothing appears on the host outside the worktree",
        _host_unchanged(escape, None),
        f"host gained the file={escape.exists()}; {seen.strip()[:80]}",
    )

    target_dir = root / "link-target"
    target_dir.mkdir(exist_ok=True)
    planted = target_dir / "planted.txt"
    planted.unlink(missing_ok=True)
    link = attacker.workdir / "escape-link"
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target_dir)
    seen = attacker.output(
        f"echo tampered > {link}/planted.txt && echo WROTE || echo denied"
    )
    yield Check(
        "symlink_out_of_worktree",
        "a symlink pointing out of the worktree writes nothing on the host",
        _host_unchanged(planted, None),
        f"host gained the file={planted.exists()}; {seen.strip()[:80]}",
    )

    home_probe = Path.home() / "research-os-audit-probe"
    home_probe.unlink(missing_ok=True)
    home_link = attacker.workdir / "home-link"
    if home_link.is_symlink() or home_link.exists():
        home_link.unlink()
    home_link.symlink_to(Path.home())
    # `ls -a`, not `ls`. Everything this is looking for is a dotfile, so the
    # earlier `ls` could not have found one and the read half of the check was
    # guaranteed to pass. The names are taken from what the host actually has,
    # so on a machine with none of them the check says so rather than passing.
    present = sorted(
        item.name for item in Path.home().iterdir() if item.name.startswith(".")
    )[:40]
    seen = attacker.output(
        f"ls -a {home_link}/ 2>/dev/null; "
        f"echo x > {home_link}/research-os-audit-probe && echo WROTE || echo denied"
    )
    leaked = [name for name in present if name in seen.split()]
    wrote = not _host_unchanged(home_probe, None)
    if not present:
        # Nothing to look for means the read half proved nothing, and this file
        # does not report that as a pass. The write half is still real, so it
        # is reported on its own rather than folded in.
        yield Check(
            "symlink_to_home",
            "a symlink to the researcher's home neither lists it nor writes it",
            None,
            "this home directory has no dotfiles, so the read half of the "
            f"check could not be attempted (the write half: host gained the "
            f"file={wrote})",
        )
    else:
        yield Check(
            "symlink_to_home",
            "a symlink to the researcher's home neither lists it nor writes it",
            not wrote and not leaked,
            f"host gained the file={wrote}; of {len(present)} real dotfiles "
            f"the sandbox saw {leaked or 'none'}",
        )

    # `..` from the worktree, aimed at a path that *exists on this host*. An
    # earlier version wrote `../../../etc/...` from a worktree several levels
    # deep, which resolves to `<somewhere>/etc/...` and not to `/etc` at all --
    # so it asked whether a file nothing had tried to create had appeared. It
    # could not fail. Both targets here are real: the audit root one level up,
    # and `/etc` reached by counting the actual depth.
    near = root / "TRAVERSAL-PROBE"
    near.unlink(missing_ok=True)
    far = Path("/etc/research-os-audit-probe")
    far_hops = "../" * (len(attacker.workdir.parts) - 1)
    seen = attacker.output(
        f"(echo tampered > ../TRAVERSAL-PROBE && echo WROTE-NEAR || echo denied-near); "
        f"(echo tampered > {far_hops}etc/research-os-audit-probe && echo WROTE-FAR "
        "|| echo denied-far)"
    )
    yield Check(
        "path_traversal",
        "`..` traversal reaches nothing on the host",
        _host_unchanged(near, None) and _host_unchanged(far, None),
        f"host gained ../TRAVERSAL-PROBE={near.exists()}, /etc probe={far.exists()}; "
        f"{seen.strip()[:100]}",
    )

    remount_probe = Path("/usr/bin/research-os-audit-probe")
    seen = attacker.output(
        "mount -o remount,rw /usr 2>&1 | head -1; "
        "echo x > /usr/bin/research-os-audit-probe && echo WROTE || echo denied"
    )
    yield Check(
        "remount_read_only",
        "the read-only operating system cannot be remounted and written on the host",
        _host_unchanged(remount_probe, None) and "WROTE" not in seen,
        f"host gained the file={remount_probe.exists()}; {seen.strip()[:120]}",
    )

    # The writable set, measured the only way that means anything: write into
    # every interesting host location and then ask the *host* what appeared.
    #
    # The first version of this asked `[ -w /etc ]` inside the sandbox and
    # failed. It was measuring bubblewrap's own tmpfs root, which is writable
    # by construction and which nothing outside the sandbox ever sees.
    probes = {
        "root": Path("/research-os-audit-probe"),
        "etc": Path("/etc/research-os-audit-probe"),
        "usr_bin": Path("/usr/bin/research-os-audit-probe"),
        "home": Path.home() / "research-os-audit-probe",
        "var_tmp": Path("/var/tmp/research-os-audit-probe"),
        "opt": Path("/opt/research-os-audit-probe"),
    }
    for path in probes.values():
        path.unlink(missing_ok=True)
    attacker.output(
        "; ".join(f"(echo x > {path} 2>/dev/null)" for path in probes.values())
        + "; echo SURVEY-DONE"
    )
    appeared = sorted(name for name, path in probes.items() if path.exists())
    for path in probes.values():
        path.unlink(missing_ok=True)
    yield Check(
        "writable_set_bounded",
        "no host location outside the declared writable set gained a file",
        not appeared,
        f"host locations written: {appeared or 'none'} (of {sorted(probes)})",
    )

    # **The production spec shape, not the empty one.** A third review pointed
    # out that every attack in this file was built on
    # `SandboxSpec(workdir=...)` alone -- `writable=()`, `readable=()`,
    # `protected=()`, `discarded=()` -- while neither production caller looks
    # like that. The experiment path makes the *project checkout* writable and
    # relies entirely on `protected` to keep `.git/hooks` and `.research` out of
    # reach; the acceptance path adds a writable environment outside the
    # worktree and a read-only canonical `.git`. So the record said
    # "containment validated" for a boundary configuration nobody runs.
    #
    # This is the same class of defect the previous round found in `record()`
    # -- a sweep earning the record without attacking the thing the record is
    # about -- relocated from *which repository* to *which spec*.
    checkout = root / "checkout"
    (checkout / ".git" / "hooks").mkdir(parents=True, exist_ok=True)
    (checkout / ".research").mkdir(parents=True, exist_ok=True)
    hook = checkout / ".git" / "hooks" / "post-checkout"
    claim = checkout / ".research" / "CLAIM-0001.yaml"
    hook.unlink(missing_ok=True)
    claim.write_text("status: draft\n", encoding="utf-8")
    payload = checkout / "data.txt"
    payload.unlink(missing_ok=True)
    elsewhere = root / "writable-outside"
    elsewhere.mkdir(exist_ok=True)
    production = SandboxSpec(
        workdir=checkout,
        writable=(elsewhere,),
        protected=(checkout / ".git", checkout / ".research"),
    )
    seen = attacker.output(
        f"(printf '#!/bin/sh\\ntouch /tmp/pwned\\n' > {hook} && echo WROTE-HOOK "
        "|| echo denied-hook); "
        f"(echo accepted > {claim} && echo WROTE-CLAIM || echo denied-claim); "
        f"(echo ok > {payload} && echo WROTE-DATA || echo denied-data); "
        f"(echo ok > {elsewhere / 'out.txt'} && echo WROTE-OUT || echo denied-out)",
        spec=production,
    )
    yield Check(
        "protected_paths_inside_a_writable_checkout",
        "`.git` and `.research` are unwritable inside a checkout that is not",
        not hook.exists() and claim.read_text("utf-8") == "status: draft\n",
        f"host gained a hook={hook.exists()}; claim preserved="
        f"{claim.read_text('utf-8') == 'status: draft\n'}; {seen.strip()[:90]}",
    )
    yield Check(
        "the_writable_checkout_is_writable",
        "the rest of the checkout, and a writable path outside it, still work",
        payload.is_file() and (elsewhere / "out.txt").is_file(),
        "without this the check above is satisfied by a sandbox that refuses "
        f"everything; checkout write={payload.is_file()}, outside write="
        f"{(elsewhere / 'out.txt').is_file()}",
    )

    inside = attacker.workdir / "inside.txt"
    inside.unlink(missing_ok=True)
    attacker.output("echo ok > ./inside.txt && echo WROTE || echo denied")
    yield Check(
        "worktree_is_writable",
        "the declared worktree *is* writable on the host, so the denials above mean "
        "something",
        inside.is_file(),
        "a suite in which nothing can be written anywhere proves nothing",
    )


def _secret_checks(attacker: _Attacker) -> Iterator[Check]:
    home = Path.home()
    for name, relative in (
        ("ssh_keys", ".ssh"),
        ("git_credentials", ".git-credentials"),
        ("gh_credentials", ".config/gh"),
        ("aws_credentials", ".aws"),
        ("netrc", ".netrc"),
        ("provider_credentials", ".claude"),
    ):
        target = home / relative
        seen = attacker.output(
            f"if [ -e {target} ]; then echo VISIBLE; "
            f"cat {target} 2>/dev/null | head -1; else echo ABSENT; fi"
        )
        yield Check(
            name,
            f"~/{relative} is not readable from inside the sandbox",
            "VISIBLE" not in seen,
            f"host has it: {target.exists()}; sandbox says: {seen.strip()[:80]}",
        )

    # **The control for the six checks above.** Four of them name files this
    # host does not have, so `VISIBLE` was unreachable and `held=True` was
    # recorded for a boundary nothing crossed. The same probe is run against a
    # file that *is* inside the sandbox: if this reports ABSENT, the probe
    # cannot see anything at all and the six results above mean nothing.
    control = Path("/etc/passwd")
    seen = attacker.output(
        f"if [ -e {control} ]; then echo VISIBLE; else echo ABSENT; fi"
    )
    yield Check(
        "secret_probe_is_capable",
        "the credential probe can see a file that really is inside the sandbox",
        "VISIBLE" in seen,
        f"{control} is deliberately bound read-only; the probe says "
        f"{seen.strip()[:40]}",
    )

    seen = attacker.output("echo HOME=$HOME; ls $HOME | head -3; echo LIST-DONE")
    yield Check(
        "home_is_disposable",
        "HOME points into the sandbox's own temporary space, not the researcher's",
        str(home) not in seen and "LIST-DONE" in seen,
        seen.strip()[:200] or "no output",
    )

    marker = "RESEARCH_OS_AUDIT_SECRET"
    os.environ[marker] = "a-provider-key-shaped-value"
    try:
        seen = attacker.output(f"echo VALUE=${{{marker}:-absent}}; env | wc -l")
    finally:
        os.environ.pop(marker, None)
    yield Check(
        "environment_secrets",
        "a secret in the launching environment is not inherited",
        "a-provider-key-shaped-value" not in seen,
        seen.strip()[:120] or "no output",
    )

    # An inherited file descriptor is the one way a path nothing bound still
    # arrives inside. Measured twice, because measuring it once could not fail:
    # the earlier version passed `close_fds=True` and then checked that the
    # descriptor was absent, which Python guarantees before bubblewrap is
    # reached. The first run here deliberately leaks it, to establish that the
    # probe can see a descriptor when one is there; the second runs it the way
    # `run_acceptance_command` does. Both halves have to come out right.
    with tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False) as handle:
        handle.write("INHERITED-FD-SECRET\n")
        secret_path = Path(handle.name)
    reader = os.open(secret_path, os.O_RDONLY)
    leaked_fd = os.dup2(reader, 19)
    script = "cat /proc/self/fd/19 2>/dev/null || echo NO-SUCH-FD"
    try:
        prepared = contain(
            ["/bin/sh", "-c", script],
            spec=SandboxSpec(workdir=attacker.workdir),
            mode=SandboxMode.REQUIRED,
        )

        def _once(*, close_fds: bool) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                list(prepared.argv),
                check=False,
                capture_output=True,
                text=True,
                stdin=subprocess.DEVNULL,
                timeout=TIMEOUT_SECONDS,
                env=prepared.environment,
                close_fds=close_fds,
            )

        leaky = _once(close_fds=False)
        production = _once(close_fds=True)
    finally:
        os.close(leaked_fd)
        os.close(reader)
        secret_path.unlink(missing_ok=True)
    leaky_text = (leaky.stdout or "") + (leaky.stderr or "")
    production_text = (production.stdout or "") + (production.stderr or "")
    yield Check(
        "inherited_file_descriptors",
        "an open host file descriptor does not reach the contained command",
        "INHERITED-FD-SECRET" not in production_text,
        f"deliberately leaked: {'visible' if 'INHERITED-FD-SECRET' in leaky_text else 'not visible'}"
        f" (the control -- if this says 'not visible' the check below proves "
        f"nothing); as production runs it: "
        f"{'VISIBLE' if 'INHERITED-FD-SECRET' in production_text else 'absent'}",
    )
    yield Check(
        "inherited_fd_probe_is_capable",
        "the descriptor probe can see a descriptor when one is really there",
        "INHERITED-FD-SECRET" in leaky_text,
        "without this control the check above passes on a probe that cannot "
        "see anything at all",
    )


def _isolation_checks(attacker: _Attacker) -> Iterator[Check]:
    seen = attacker.output("ls /proc | grep -c '^[0-9]*$'; ps -e 2>/dev/null | wc -l")
    counted = [line for line in seen.split() if line.isdigit()]
    yield Check(
        "host_pid_isolation",
        "host processes are not visible from inside",
        bool(counted) and all(int(value) < 20 for value in counted),
        f"pid-like entries seen: {counted}",
    )

    seen = attacker.output(
        "getent hosts pypi.org >/dev/null 2>&1 && echo RESOLVED || echo NO-DNS"
    )
    yield Check(
        "network_denied",
        "outbound network is denied when it was not granted",
        "RESOLVED" not in seen,
        seen.strip() or "no output",
    )

    # One host listener, two runs. The denial and the control are measured
    # against the *same* service, so each can fail independently.
    #
    # The earlier control asked DNS whether the network worked and accepted
    # either answer -- `"RESOLVED" in granted or "NO-DNS" in granted` -- which
    # is every possible output of the script it ran. It could not fail, and on
    # an offline host the denial next to it could not fail either, so the pair
    # established nothing. That is the exact defect this pair exists to catch
    # in the sandbox, reproduced in the instrument.
    port = _spare_port()
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        listener.bind(("127.0.0.1", port))
        listener.listen(8)
        # Python, not `/dev/tcp`. `/dev/tcp` is a bash feature and `/bin/sh`
        # here is dash, so the redirection failed identically with the network
        # granted and denied -- the denial "passed" because the instrument
        # could not connect under any circumstances. The control added beside
        # it is what caught that, which is the entire argument for having one.
        connect = (
            "import socket,sys\n"
            "s=socket.socket()\n"
            "s.settimeout(5)\n"
            "try:\n"
            f"    s.connect(('127.0.0.1',{port}))\n"
            "    print('CONNECTED')\n"
            "except OSError as e:\n"
            "    print('REFUSED', type(e).__name__)\n"
        )
        interpreter = _system_python()
        if interpreter is None:
            yield Check(
                "loopback_denied",
                "a service on the host's loopback is unreachable",
                None,
                "no system python to open a socket with",
            )
            yield Check(
                "network_is_a_real_capability",
                "granting the network reaches that same service",
                None,
                "no system python to open a socket with",
            )
            return
        probe = f"{interpreter} -c {shlex.quote(connect)}"
        denied = attacker.output(probe)
        granted = attacker.output(probe, network=True)
        yield Check(
            "loopback_denied",
            "a service on the host's loopback is unreachable without the network",
            "CONNECTED" not in denied,
            f"host listener on 127.0.0.1:{port}; sandbox says {denied.strip()}",
        )
        yield Check(
            "network_is_a_real_capability",
            "granting the network reaches that same service, so the denial is a denial",
            "CONNECTED" in granted,
            f"with --share-net the sandbox says {granted.strip()}; without it "
            f"{denied.strip()}",
        )
    finally:
        listener.close()

    unshare = shutil.which("unshare")
    if unshare is None:
        yield Check(
            "nested_user_namespaces",
            "a contained process cannot create another user namespace",
            None,
            "util-linux `unshare` is not installed on this host",
        )
    else:
        seen = attacker.output(
            f"{unshare} --user --map-root-user /bin/true 2>&1 && echo NESTED "
            "|| echo DENIED"
        )
        yield Check(
            "nested_user_namespaces",
            "a contained process cannot create another user namespace",
            "NESTED" not in seen and "DENIED" in seen,
            seen.strip()[:160] or "no output",
        )

    # A fork storm has to *hit* something, and the evidence has to be the
    # shell saying it could not fork. "The script ran to the end" would pass on
    # a host with no limit at all.
    #
    # The label matters as much as the result: this is host-relative process
    # limiting, not a per-sandbox quota. `RLIMIT_NPROC` is counted per
    # (user namespace, uid), and the limit is set in the parent *before*
    # bubblewrap creates the namespace, so it must first clear the researcher's
    # own task count -- `process_limit_preexec` raises it to that count plus
    # headroom. The ceiling is therefore larger than `max_processes` asked for.
    # It bounds runaway process creation; it does not partition anything, and
    # calling it a quota would be claiming a property nothing here provides.
    from research_os.sandbox import _NPROC_HEADROOM, _uid_task_count

    before_tasks = _uid_task_count()
    ceiling = max(512, before_tasks + _NPROC_HEADROOM)
    seen = attacker.output(
        "n=0; while [ $n -lt 6000 ]; do sleep 20 & n=$((n+1)); done; echo LOOPED $n"
    )
    hit_the_limit = "annot fork" in seen or "esource temporarily" in seen
    after_tasks = _uid_task_count()
    yield Check(
        "fork_storm_bounded",
        "a fork storm hits a ceiling, and the host's own task count is unharmed",
        hit_the_limit and after_tasks < before_tasks + _NPROC_HEADROOM,
        f"ceiling≈{ceiling} tasks (host-relative, not a per-sandbox quota); "
        f"shell reported a fork failure={hit_the_limit}; host tasks "
        f"{before_tasks}->{after_tasks}; {seen.strip().splitlines()[-1][:60] if seen.strip() else 'no output'}",
    )


def _residue_checks(attacker: _Attacker) -> Iterator[Check]:
    # A unique sentinel, and the parent is not killed until the children have
    # actually started. The earlier version called `terminate()` immediately
    # after `Popen`, so bubblewrap was usually dead before `/bin/sh` forked
    # anything and the check passed without the property ever being exercised.
    # It also matched `pgrep -f "sleep 300"` across the whole host, so an
    # unrelated process could fail it.
    import time as _time
    import uuid as _uuid

    sentinel = f"research-os-audit-{_uuid.uuid4().hex[:12]}"
    spec = SandboxSpec(workdir=attacker.workdir)
    prepared = contain(
        [
            "/bin/sh",
            "-c",
            # `sh -c 'sleep 300' <name>` sets the wrapper shell's `$0`, so the
            # sentinel appears in its command line and `pgrep -f` can find it.
            # The first version put the sentinel in `sleep`'s own argv --
            # `sleep 300 <sentinel>` -- which is an invalid time interval, so
            # every child exited immediately and the check measured nothing.
            (
                f"/bin/sh -c 'sleep 300' {sentinel} & "
                f"/bin/sh -c 'sleep 300' {sentinel} & "
                f"echo STARTED; /bin/sh -c 'sleep 300' {sentinel}"
            ),
        ],
        spec=spec,
        mode=SandboxMode.REQUIRED,
    )
    process = subprocess.Popen(
        list(prepared.argv),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        text=True,
        env=prepared.environment,
        start_new_session=True,
    )
    started = False
    survivors = "unknown"
    running_before = ""
    try:
        deadline = _time.monotonic() + 30
        while _time.monotonic() < deadline:
            if process.stdout is not None:
                line = process.stdout.readline()
                if "STARTED" in line:
                    started = True
                    break
            if process.poll() is not None:
                break
        # The children are real before the parent is killed; that is the whole
        # point of the check.
        running_before = subprocess.run(
            ["pgrep", "-f", sentinel], check=False, capture_output=True, text=True
        ).stdout.strip()
        process.terminate()
        process.wait(timeout=30)
        _time.sleep(1)
        survivors = subprocess.run(
            ["pgrep", "-f", sentinel], check=False, capture_output=True, text=True
        ).stdout.strip()
    except (subprocess.TimeoutExpired, OSError):  # pragma: no cover
        process.kill()
    yield Check(
        "no_survivors_after_cancellation",
        "cancelling a contained command leaves no descendant processes",
        started and bool(running_before) and survivors == "",
        f"children started={started}; running before terminate="
        f"{len(running_before.splitlines())}; still running after="
        f"{survivors or 'none'}",
    )

    before = _tree(attacker.root)
    attacker.output("echo ok > ./residue-probe.txt; echo DONE")
    after = _tree(attacker.root)
    added = sorted(after - before)
    yield Check(
        "host_residue_bounded",
        "a contained command changes nothing on the host outside its worktree",
        all(str(attacker.workdir) in item for item in added),
        f"new paths: {added[:5] or 'none'}",
    )

    backend = available_backend()
    if backend is None or backend.executable is None:  # pragma: no cover
        yield Check("overlay_discards_writes", "overlay writes are discarded", None, "")
    elif not overlay_available(backend.executable):
        yield Check(
            "overlay_discards_writes",
            "a throwaway overlay is readable and keeps nothing",
            None,
            "this host's kernel provides no unprivileged overlay",
        )
    else:
        lower = attacker.root / "overlay-lower"
        lower.mkdir(exist_ok=True)
        (lower / "from-host.txt").write_text("host content\n", encoding="utf-8")
        seen = attacker.output(
            f"cat {lower}/from-host.txt; echo poison > {lower}/planted.txt "
            "&& echo WROTE || echo denied",
            spec=SandboxSpec(workdir=attacker.workdir, discarded=(lower,)),
        )
        yield Check(
            "overlay_discards_writes",
            "a throwaway overlay is readable and keeps nothing",
            "host content" in seen
            and "WROTE" in seen
            and not (lower / "planted.txt").exists(),
            f"{seen.strip()[:80]}; host gained planted.txt: "
            f"{(lower / 'planted.txt').exists()}",
        )


def _tree(root: Path) -> set[str]:
    found: set[str] = set()
    for path in root.rglob("*"):
        found.add(str(path))
    return found


def _system_python() -> str | None:
    """A Python inside the read-only operating system, for socket probes."""

    for candidate in ("/usr/bin/python3", "/bin/python3"):
        if Path(candidate).is_file():
            return candidate
    return None


def _spare_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# ---------------------------------------------------------------- the entry --
def audit(root: Path, canonical: Sequence[Path] = ()) -> tuple[Check, ...]:
    """Run every attack against the production adapter under ``root``.

    ``root`` is a disposable directory the caller owns. Nothing here writes
    outside it except where the attack's whole point is to try to, and those
    are asserted not to have succeeded.
    """

    # Resolved, not textual: a root reached through a symlink into `/tmp` is
    # still under the tmpfs the sandbox mounts there.
    root = Path(root)
    probe = root if root.exists() else root.parent
    resolved = probe.resolve() / root.name if probe is not root else root.resolve()
    if any(
        candidate == Path("/tmp") or Path("/tmp") in candidate.parents
        for candidate in (root, resolved)
    ):
        # The sandbox mounts its own tmpfs over `/tmp`, so a fixture placed
        # there is invisible inside for a reason that has nothing to do with
        # policy. Half these checks would pass while measuring the sandbox's
        # scratch filesystem. An audit that cannot measure is worse than no
        # audit, so this refuses rather than reporting a number.
        raise ValueError(
            f"the audit root {root} is under /tmp, which the sandbox replaces "
            "with a tmpfs of its own. Fixtures there are invisible inside for "
            "a reason unrelated to containment, and the checks would pass "
            "without measuring anything. Use a directory outside /tmp."
        )
    backend = available_backend()
    if backend is None:
        reason = unavailable_reason()
        return (
            Check(
                "containment_available",
                "this host can provide OS-level containment at all",
                None,
                reason,
            ),
        )
    attacker = _Attacker(root)
    found: list[Check] = [
        Check(
            "containment_available",
            "this host can provide OS-level containment at all",
            True,
            f"{backend.technology} {backend.version or ''} at {backend.executable}",
        )
    ]
    found.extend(_checks(attacker, canonical))
    return tuple(found)


def summarise(checks: tuple[Check, ...]) -> str:
    held = sum(1 for check in checks if check.held is True)
    failed = [check.name for check in checks if check.held is False]
    skipped = [check.name for check in checks if check.held is None]
    parts = [f"{held}/{len(checks)} adversarial checks held"]
    if failed:
        parts.append("FAILED: " + ", ".join(failed))
    if skipped:
        parts.append("could not run: " + ", ".join(skipped))
    return "; ".join(parts)


def record(checks: tuple[Check, ...], *, executable: str, version: str | None) -> bool:
    """Write the validation record, but only if every check actually held.

    A skip does not qualify, and neither does a sweep that attacked no canonical
    repository.

    The record is what `runtime doctor` reports as "containment validated". It
    is **not** a gate: `SandboxProbe.available` is deliberately
    ``namespaces_ok and security_eligible`` and excludes it, so that a fresh
    host can run the suite that would validate it. An earlier version of this
    docstring said `available_backend()` gated high-autonomy execution on it.
    That was false, an audit caught it in the module docstring, the correction
    was applied there -- and this copy, in the function that actually writes the
    record, was missed. A second audit caught that. Recording both because the
    pattern is the point: a claim repeated in three places gets corrected in
    two.

    None of which makes the record cheap. It is the sentence a person reads when
    deciding whether to leave this system running unattended, so a record
    written from a suite that half ran would still be the most expensive kind of
    wrong answer this file could produce.
    """

    if any(check.held is not True for check in checks):
        return False
    if not any(
        check.name == "protected_paths_inside_a_writable_checkout" for check in checks
    ):
        # The experiment path's only barrier between model-parameterised project
        # code and `.git/hooks` host code execution is `protected`, and a suite
        # that never built a spec carrying one has not measured it.
        LOG.warning(
            "refusing to record a containment validation from a run that never "
            "attacked a spec with `protected` paths: the production experiment "
            "path relies on nothing else."
        )
        return False
    if not any(check.name.startswith("canonical_repository_") for check in checks):
        # The canonical repository is the boundary this system exists to
        # protect, and a suite that never attacked one has not established the
        # thing the record claims. An audit run without `--repo` is still
        # useful to read; it is not a validation.
        LOG.warning(
            "refusing to record a containment validation from a run that "
            "attacked no canonical repository: pass --repo."
        )
        return False
    record_containment_validation(
        executable,
        version,
        detail=(
            f"{summarise(checks)}, run through the production `contain()` "
            "adapter by `researchctl runtime containment-audit`. Scope: "
            + ", ".join(check.name for check in checks)
            + ". NOT a proof of absence of escape: these are the boundaries "
            "this system relies on, and CVE-2026-87766 is out of scope because "
            "this build is patched against it rather than tested for it."
        ),
    )
    return True


def run_and_record(
    root: Path, canonical: Sequence[Path] = ()
) -> tuple[tuple[Check, ...], bool]:
    """The whole job: attack, report, and record only on a clean sweep."""

    checks = audit(root, canonical)
    backend = available_backend()
    if backend is None or backend.executable is None:
        return checks, False
    return checks, record(
        checks, executable=backend.executable, version=backend.version
    )


__all__ = [
    "Check",
    "audit",
    "record",
    "run_and_record",
    "summarise",
]
