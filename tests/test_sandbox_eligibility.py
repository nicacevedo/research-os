"""A sandbox that runs is not thereby a sandbox that may be used.

Four facts, kept apart, because collapsing them is the specific mistake that
would let this deployment run model-written code inside a known-escapable
boundary:

.. code-block:: text

    executable present       the binary is on PATH
    namespace capability     it ran, and got a real namespace with a uid map
    security-eligible        its version is at or above the known floor
    containment validated    the adversarial suite ran against it, and held

The live case. This host has bubblewrap 0.9.0, non-setuid, from
``0.9.0-1ubuntu0.1``. CVE-2026-87766 / GHSA-pxhw-h44j-8pfx affects every
version below 0.12.0: during sandbox *setup*, creating a file under the new
root can follow a parent symlink out through ``/oldroot`` and write an
attacker-chosen path on the host as the launching user. This system runs
acceptance commands over a worktree a model has just written to, so
"attacker-controlled filesystem content" is its ordinary case.

The binary works. That is the danger. A probe that measured only whether it
ran would report it available, which is exactly what the advisory says a
vulnerable bwrap does.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from research_os import sandbox
from research_os.sandbox import (
    SECURITY_FLOORS,
    VENDOR_FIXED_RANGES,
    NamespaceState,
    SandboxProbe,
    VendorPackage,
    available_backend,
    dpkg_compare,
    parse_version,
    security_basis,
    security_verdict,
    unavailable_reason,
)


@pytest.fixture(autouse=True)
def _restore_backend_cache() -> object:
    """Put ``sandbox._BACKEND`` back after each test.

    ``available_backend(refresh=True)`` caches its answer per process, which is
    correct in production -- the kernel cannot change underneath a daemon -- and
    means a test that refreshes against a fake probe leaves every later test
    holding that fake. Two of the tests below do exactly that.
    """

    saved = sandbox._BACKEND
    yield
    sandbox._BACKEND = saved


def make_probe(**overrides: object) -> SandboxProbe:
    # `namespaces_ok` is a derived property now, but it is still the clearest
    # thing to write in a test about availability, so it is accepted here and
    # translated.
    if "namespaces_ok" in overrides:
        overrides["namespace_state"] = (
            NamespaceState.AVAILABLE
            if overrides.pop("namespaces_ok")
            else NamespaceState.BLOCKED
        )
    base: dict[str, object] = {
        "technology": "bubblewrap",
        "executable": "/usr/bin/bwrap",
        "namespace_state": NamespaceState.AVAILABLE,
        "detail": "namespaces work",
        "security_eligible": True,
        "version": "bubblewrap 0.12.0",
        "setuid": False,
    }
    base.update(overrides)
    return SandboxProbe(**base)  # type: ignore[arg-type]


# --- 1. the four dimensions are four ----------------------------------------
def test_the_probe_reports_four_separate_facts() -> None:
    found = make_probe(namespaces_ok=True, security_eligible=False)
    assert found.executable is not None
    assert found.namespaces_ok is True
    assert found.security_eligible is False
    assert found.containment_validated is False
    # And the derived answer is not any one of them.
    assert found.available is False


def test_availability_requires_namespaces_and_eligibility_together() -> None:
    assert make_probe(namespaces_ok=True, security_eligible=True).available is True
    assert make_probe(namespaces_ok=False, security_eligible=True).available is False
    assert make_probe(namespaces_ok=True, security_eligible=False).available is False
    assert make_probe(namespaces_ok=False, security_eligible=False).available is False


def test_validation_does_not_gate_ordinary_availability() -> None:
    """Otherwise a fresh host could never run the suite that validates it."""

    found = make_probe(containment_validated=False)
    assert found.available is True


# --- 2. version parsing ------------------------------------------------------
@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("bubblewrap 0.9.0", (0, 9, 0)),
        ("bubblewrap 0.12.0", (0, 12, 0)),
        ("0.12.0-1ubuntu1", (0, 12, 0)),
        ("bubblewrap 1.0", (1, 0)),
        ("", None),
        ("no version here", None),
    ],
)
def test_a_version_string_parses_or_reports_that_it_did_not(
    text: str, expected: tuple[int, ...] | None
) -> None:
    assert parse_version(text) == expected


def test_a_dotted_comparison_is_numeric_not_lexicographic() -> None:
    """The bug this avoids: as strings, "0.9.0" sorts above "0.12.0"."""

    as_strings = sorted(["0.9.0", "0.12.0"])
    assert as_strings[-1] == "0.9.0", "string order puts 0.9.0 last"
    old = parse_version("0.9.0")
    new = parse_version("0.12.0")
    assert old is not None and new is not None
    assert old < new
    # And the floor agrees with the tuple order, not the string order.
    assert security_verdict("bubblewrap", "0.9.0")[0] is False
    assert security_verdict("bubblewrap", "0.12.0")[0] is True


# --- 3. the floor, and what it refuses ---------------------------------------
def test_the_floor_is_recorded_with_its_advisory() -> None:
    floor = SECURITY_FLOORS["bubblewrap"]
    assert floor.minimum == (0, 12, 0)
    assert "CVE-2026-87766" in floor.advisory
    assert "GHSA-pxhw-h44j-8pfx" in floor.advisory
    assert floor.key == "CVE-2026-87766"


def test_an_old_bubblewrap_is_not_security_eligible() -> None:
    eligible, detail = security_verdict("bubblewrap", "bubblewrap 0.9.0")
    assert eligible is False
    assert "CVE-2026-87766" in detail
    assert "PRESENT_BUT_UNACCEPTABLE" in detail


def test_a_patched_bubblewrap_is_security_eligible() -> None:
    eligible, detail = security_verdict("bubblewrap", "bubblewrap 0.12.0")
    assert eligible is True
    assert "0.12.0" in detail


def test_a_vendor_backport_carrying_the_fixed_version_is_accepted() -> None:
    """A distribution package with a suffix is still that version."""

    eligible, _detail = security_verdict("bubblewrap", "0.12.0-1ubuntu0.1")
    assert eligible is True


def test_an_undeterminable_version_is_treated_as_unsafe() -> None:
    """ "We could not tell" and "it is fine" are different facts."""

    eligible, detail = security_verdict("bubblewrap", None)
    assert eligible is False
    assert "could not determine" in detail


def test_a_technology_with_no_recorded_floor_is_not_refused() -> None:
    """The table lists what is known broken, not what is known good."""

    eligible, _detail = security_verdict("podman", "5.0.0")
    assert eligible is True


# --- 4. the five host configurations §7 names --------------------------------
def test_missing_bwrap_is_absent_rather_than_unacceptable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    found = sandbox.probe_bubblewrap()
    assert found.executable is None
    assert found.available is False
    assert "not on PATH" in found.detail
    assert "install bubblewrap" in found.remedy


def test_an_unsafe_bwrap_with_working_namespaces_is_not_a_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The dangerous configuration: everything works, and it must not be used.

    This is what this host becomes the moment someone installs the AppArmor
    userns profile without replacing the binary.
    """

    _fake_bwrap(monkeypatch, version="bubblewrap 0.9.0", namespaces_work=True)
    found = sandbox.probe_bubblewrap()
    assert found.namespaces_ok is True
    assert found.security_eligible is False
    assert found.available is False
    assert "NOT security-eligible" in found.detail
    # And the remedy must not be "grant it the namespace".
    assert "Do not install an AppArmor userns profile" in found.remedy


def test_a_safe_bwrap_blocked_by_apparmor_is_eligible_but_unavailable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_bwrap(
        monkeypatch,
        version="bubblewrap 0.12.0",
        namespaces_work=False,
        stderr="bwrap: setting up uid map: Permission denied",
    )
    found = sandbox.probe_bubblewrap()
    assert found.security_eligible is True
    assert found.namespaces_ok is False
    assert found.available is False
    # Now -- and only now -- an AppArmor profile is the right remedy.
    assert "AppArmor profile" in found.remedy
    assert "replace the binary first" not in found.remedy


def test_a_safe_bwrap_with_namespaces_is_an_acceptable_backend(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_bwrap(monkeypatch, version="bubblewrap 0.12.0", namespaces_work=True)
    found = sandbox.probe_bubblewrap()
    assert found.namespaces_ok is True
    assert found.security_eligible is True
    assert found.available is True


def test_an_unsafe_bwrap_blocked_by_apparmor_is_told_which_to_fix_first(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """This host, exactly. Two things are wrong and the order matters.

    Granting the namespace before replacing the binary turns a sandbox that
    cannot escape into one that can, which is worse than the present state.
    """

    _fake_bwrap(
        monkeypatch,
        version="bubblewrap 0.9.0",
        namespaces_work=False,
        stderr="bwrap: setting up uid map: Permission denied",
    )
    found = sandbox.probe_bubblewrap()
    assert found.available is False
    assert "replace the binary first" in found.remedy


def test_a_setuid_binary_claiming_the_fixed_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """bubblewrap removed setuid support in 0.12.0.

    A binary that is both setuid and at the floor is a contradiction, and the
    conservative reading of a contradiction is to decline.
    """

    _fake_bwrap(
        monkeypatch, version="bubblewrap 0.12.0", namespaces_work=True, setuid=True
    )
    found = sandbox.probe_bubblewrap()
    assert found.setuid is True
    assert found.security_eligible is False
    assert found.available is False
    assert "setuid" in found.security_detail


# --- 5. selection honours the gate -------------------------------------------
def test_available_backend_never_returns_an_ineligible_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The single most important assertion in this file."""

    monkeypatch.setattr(
        sandbox,
        "probe",
        lambda: (make_probe(namespaces_ok=True, security_eligible=False),),
    )
    assert available_backend(refresh=True) is None


def test_available_backend_returns_an_eligible_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        sandbox,
        "probe",
        lambda: (make_probe(namespaces_ok=True, security_eligible=True),),
    )
    found = available_backend(refresh=True)
    assert found is not None and found.technology == "bubblewrap"


def test_the_reason_distinguishes_unacceptable_from_absent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They call for opposite actions: install one, replace the other."""

    monkeypatch.setattr(
        sandbox,
        "probe",
        lambda: (
            make_probe(
                namespace_state=NamespaceState.AVAILABLE,
                security_eligible=False,
                security_detail="0.9.0 is below 0.12.0",
            ),
        ),
    )
    reason = unavailable_reason()
    assert "PRESENT_BUT_UNACCEPTABLE" in reason
    assert "0.9.0 is below 0.12.0" in reason


# --- 6. validation is recorded, never inferred -------------------------------
def test_containment_is_not_validated_until_the_suite_has_run(
    tmp_path: Path,
) -> None:
    validated, detail = sandbox.containment_validation("/usr/bin/bwrap", "0.12.0")
    assert validated is False
    assert "has not been run" in detail or "no adversarial containment record" in detail


def test_a_recorded_validation_is_keyed_to_the_binarys_content(
    tmp_path: Path,
) -> None:
    """An upgrade must invalidate the previous record.

    A validation earned by one binary being attacked must not be inherited by a
    different one installed at the same path -- which is precisely what
    replacing a vulnerable bwrap does, and precisely when a stale "validated"
    would be most misleading.
    """

    binary = tmp_path / "bwrap"
    binary.write_bytes(b"version one")
    sandbox.record_containment_validation(
        str(binary), "0.12.0", detail="suite held, 14 escapes attempted"
    )
    validated, detail = sandbox.containment_validation(str(binary), "0.12.0")
    assert validated is True
    assert "14 escapes attempted" in detail

    binary.write_bytes(b"version two, a different build entirely")
    validated_after, _detail = sandbox.containment_validation(str(binary), "0.12.0")
    assert validated_after is False


def test_a_probe_cannot_mark_itself_validated(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No amount of measuring a version establishes that a boundary held."""

    _fake_bwrap(monkeypatch, version="bubblewrap 0.12.0", namespaces_work=True)
    found = sandbox.probe_bubblewrap()
    assert found.security_eligible is True
    assert found.containment_validated is False


# --- 7. the floor is general, not Ubuntu-specific ----------------------------
def test_nothing_in_the_gate_dispatches_on_the_distribution() -> None:
    """A floor keyed to a distribution would be wrong on every other one."""

    source = Path(sandbox.__file__).read_text(encoding="utf-8")
    verdict_source = source[
        source.index("def security_verdict") : source.index("def _bwrap_version")
    ]
    for token in ("ubuntu", "noble", "debian", "lsb_release", "/etc/os-release"):
        assert token not in verdict_source.lower(), token


def _fake_bwrap(
    monkeypatch: pytest.MonkeyPatch,
    *,
    version: str,
    namespaces_work: bool,
    stderr: str = "",
    setuid: bool = False,
) -> None:
    """Stand in for a bwrap of a chosen version and a chosen kernel outcome."""

    import subprocess

    # `which` is stubbed for bwrap only. Returning the fake path for *every*
    # lookup made `dpkg_compare` shell out to the stubbed bwrap and read its
    # exit code as a version comparison, which silently answered "no" to every
    # backport question.
    real_which = sandbox.shutil.which
    monkeypatch.setattr(
        sandbox.shutil,
        "which",
        lambda name: "/usr/bin/bwrap" if name == "bwrap" else real_which(name),
    )
    monkeypatch.setattr(sandbox, "_is_setuid", lambda _path: setuid)
    # No vendor evidence unless a test supplies some. Without this the stubbed
    # probe would consult the real dpkg database of whatever host it runs on.
    monkeypatch.setattr(sandbox, "detect_vendor_package", lambda _exe: None)

    real_run = sandbox.subprocess.run

    def fake_run(argv: list[str], **kwargs: object) -> object:
        # Only bwrap is faked. dpkg must keep answering for real, because the
        # version semantics under test are dpkg's.
        if argv and not str(argv[0]).endswith("bwrap"):
            return real_run(argv, **kwargs)  # type: ignore[arg-type]
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version, stderr="")
        return subprocess.CompletedProcess(
            argv, 0 if namespaces_work else 1, stdout="", stderr=stderr
        )

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)


# --- 8. vendor security backports ------------------------------------------
#
# A distribution can carry a fix without carrying the version number that fix
# arrived in upstream, and refusing every such build would refuse most
# correctly-patched Linux hosts. What makes it safe to trust is that the
# evidence comes from the package database rather than from the program's own
# `--version`, and that it is positive evidence rather than an ordering.
NOBLE = {"os_id": "ubuntu", "codename": "noble", "release": "24.04"}


def ubuntu_bwrap(version: str, **overrides: object) -> VendorPackage:
    fields: dict[str, object] = {
        **NOBLE,
        "name": "bubblewrap",
        "version": version,
        "path": "/usr/bin/bwrap",
    }
    fields.update(overrides)
    return VendorPackage(**fields)  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("package_version", "expected", "why"),
    [
        ("0.9.0-1ubuntu0.1", False, "before the backport"),
        ("0.9.0-1ubuntu0.2", True, "USN-8779-1 shipped the two CVE patches"),
        # The case that makes this a range table rather than a floor.
        ("0.9.0-1ubuntu0.3", False, "the vendor REVERTED the fix"),
        ("0.9.0-1ubuntu0.4", False, "unknown; not trusted for being newer"),
    ],
)
def test_ubuntu_noble_backport_status_is_not_monotonic_in_version(
    package_version: str, expected: bool, why: str
) -> None:
    """The defect a `>=` floor would have introduced on this exact host.

    Ubuntu noble's bubblewrap went unfixed, fixed, then **unfixed again**:

        0.9.0-1ubuntu0.1   unfixed
        0.9.0-1ubuntu0.2   FIXED    -- USN-8779-1
        0.9.0-1ubuntu0.3   UNFIXED  -- "SECURITY REGRESSION: Incompatibility
                                        with Flatpak (LP: #2167621)
                                        - debian: Drop CVE-2026-87766"

    The patches broke Flatpak's CUPS socket path resolution and Canonical
    reverted them. So `>= 0.9.0-1ubuntu0.2` -- the obvious rule -- returns true
    for a binary whose CVE fix was deliberately removed.
    """

    eligible, detail = security_verdict(
        "bubblewrap", "bubblewrap 0.9.0", ubuntu_bwrap(package_version)
    )
    assert eligible is expected, f"{package_version}: {why} -- {detail}"


def test_the_reverting_version_says_that_it_reverted() -> None:
    """A refusal a person cannot act on sends them to upgrade, which is wrong
    here: the newer package is the one that removed the fix."""

    _eligible, detail = security_verdict(
        "bubblewrap", "bubblewrap 0.9.0", ubuntu_bwrap("0.9.0-1ubuntu0.3")
    )
    assert "REVERTED" in detail
    assert "Drop CVE-2026-87766" in detail
    assert "will not be trusted for being newer" in detail


def test_a_generic_upstream_build_is_unaffected_by_the_vendor_table() -> None:
    """Requirement: the upstream rule is preserved."""

    assert security_verdict("bubblewrap", "bubblewrap 0.9.0", None)[0] is False
    assert security_verdict("bubblewrap", "bubblewrap 0.12.0", None)[0] is True


def test_a_local_build_does_not_inherit_the_distribution_patch_status() -> None:
    """A hand-built /usr/local/bin/bwrap at 0.9.0 is not the packaged one.

    `detect_vendor_package` returns None for a binary no package owns, so the
    vendor path is never reached -- and the caller must not be able to get the
    same answer by constructing a VendorPackage for a path the package does not
    own either.
    """

    # No package evidence at all: the ordinary local-build case.
    assert security_verdict("bubblewrap", "bubblewrap 0.9.0", None)[0] is False
    # And a package record for a *different* binary is still not this binary's.
    other = ubuntu_bwrap("0.9.0-1ubuntu0.2", name="not-bubblewrap")
    assert security_verdict("bubblewrap", "bubblewrap 0.9.0", other)[0] is False


def test_the_backport_table_is_keyed_by_distribution_and_release() -> None:
    """Ubuntu's noble backport says nothing about Debian, or about jammy."""

    fixed = "0.9.0-1ubuntu0.2"
    assert security_verdict("bubblewrap", "bubblewrap 0.9.0", ubuntu_bwrap(fixed))[0]
    for wrong in (
        ubuntu_bwrap(fixed, os_id="debian"),
        ubuntu_bwrap(fixed, codename="jammy"),
    ):
        assert security_verdict("bubblewrap", "bubblewrap 0.9.0", wrong)[0] is False


def test_an_unlisted_distribution_gets_no_backport_credit() -> None:
    """Positive evidence only. Unlike SECURITY_FLOORS, absence is not consent."""

    assert ("fedora", "", "bubblewrap", "CVE-2026-87766") not in VENDOR_FIXED_RANGES
    unknown = ubuntu_bwrap("0.9.0-1", os_id="fedora", codename="")
    eligible, detail = security_verdict("bubblewrap", "bubblewrap 0.9.0", unknown)
    assert eligible is False
    assert "no CVE-2026-87766 backport is recorded" in detail


def test_version_comparison_uses_real_dpkg_semantics() -> None:
    """Not a hand-written lexical comparison.

    `~` sorting before the empty string is the case a string comparison always
    gets wrong, and it is exactly what a distribution backport looks like.
    """

    if dpkg_compare("1", "ge", "1") is None:
        pytest.skip("dpkg is not available on this host")
    assert dpkg_compare("0.9.0-1ubuntu0.10", "gt", "0.9.0-1ubuntu0.9") is True
    assert dpkg_compare("0.12.0-1~deb13u1", "lt", "0.12.0-1") is True
    assert dpkg_compare("1:0.9.0", "gt", "0.12.0") is True
    # And the lexical answer disagrees.
    lexical = sorted(["0.9.0-1ubuntu0.10", "0.9.0-1ubuntu0.9"])
    assert lexical[-1] == "0.9.0-1ubuntu0.9", "string order puts 0.9 above 0.10"


def test_an_unaskable_dpkg_is_unknown_rather_than_satisfied(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: None)
    assert dpkg_compare("1", "ge", "0") is None
    eligible, detail = security_verdict(
        "bubblewrap", "bubblewrap 0.9.0", ubuntu_bwrap("0.9.0-1ubuntu0.2")
    )
    assert eligible is False
    assert "could not compare" in detail


def test_the_basis_explains_what_the_decision_was_made_from() -> None:
    package = ubuntu_bwrap("0.9.0-1ubuntu0.2")
    eligible, _detail = security_verdict("bubblewrap", "bubblewrap 0.9.0", package)
    rows = dict(security_basis("bubblewrap", "bubblewrap 0.9.0", package, eligible))
    assert rows["upstream_version"] == "bubblewrap 0.9.0"
    assert rows["package"] == "bubblewrap"
    assert rows["package_version"] == "0.9.0-1ubuntu0.2"
    assert rows["vendor"] == "ubuntu"
    assert rows["release"] == "24.04 / noble"
    assert rows["CVE-2026-87766"] == "vendor_backport_fixed"
    assert rows["security_eligible"] == "yes"


def test_the_basis_distinguishes_upstream_from_vendor_and_from_affected() -> None:
    def state(version: str, package: VendorPackage | None) -> str:
        eligible, _ = security_verdict("bubblewrap", version, package)
        return dict(security_basis("bubblewrap", version, package, eligible))[
            "CVE-2026-87766"
        ]

    assert state("bubblewrap 0.12.0", None) == "upstream_fixed"
    assert state("bubblewrap 0.9.0", ubuntu_bwrap("0.9.0-1ubuntu0.2")) == (
        "vendor_backport_fixed"
    )
    assert state("bubblewrap 0.9.0", ubuntu_bwrap("0.9.0-1ubuntu0.3")) == "affected"
    assert state("bubblewrap 0.9.0", None) == "affected"


def test_the_live_hosts_verdict_matches_its_own_evidence() -> None:
    """Whatever this machine has installed, the verdict must follow from it.

    Written as a consistency check rather than as a pin to one version,
    because the host has already moved twice under this suite:
    0.9.0-1ubuntu0.1 (unfixed), 0.9.0-1ubuntu0.3 (the revert), and now a
    rebuilt Debian 0.12.0-1~deb13u1. A test pinned to any one of those skips
    itself into uselessness on the next upgrade.
    """

    found = next(item for item in sandbox.probe() if item.technology == "bubblewrap")
    if found.executable is None:
        pytest.skip("no bwrap on this host")

    upstream = parse_version(found.version or "")
    floor = SECURITY_FLOORS["bubblewrap"].minimum
    upstream_ok = upstream is not None and upstream >= floor
    vendor_ok = (
        sandbox.vendor_backport_verdict(found.vendor_package, "CVE-2026-87766")[0]
        if found.vendor_package is not None
        else False
    )
    assert found.security_eligible is (upstream_ok or vendor_ok), found.security_detail
    # And availability never outruns eligibility, whatever the kernel allows.
    assert found.available is (found.security_eligible and found.namespaces_ok)


# --- 9. what the namespace probe found, in three states ---------------------
#
# The distinction cost an afternoon. After an AppArmor profile correctly
# granted bwrap the `userns` permission, the probe went on reporting a kernel
# denial -- because its error had changed from "setting up uid map" to
# "execvp /bin/true: No such file or directory", and it classified any non-zero
# exit as a denial. The namespace was fine; the probe's own sandbox had no
# /bin and no /lib64, this host being usrmerged, and execvp returns ENOENT for
# a missing ELF interpreter exactly as it does for a missing binary.
@pytest.mark.parametrize(
    ("message", "expected"),
    [
        # (a) AppArmor / userns denied.
        ("bwrap: setting up uid map: Permission denied", NamespaceState.BLOCKED),
        (
            "bwrap: Creating new namespace failed: Operation not permitted",
            NamespaceState.BLOCKED,
        ),
        (
            "bwrap: loopback: Failed RTM_NEWADDR: Operation not permitted",
            NamespaceState.BLOCKED,
        ),
        ("bwrap: No permissions to create new namespace", NamespaceState.BLOCKED),
        # (b) malformed sandbox root -> ENOENT, after the namespace succeeded.
        (
            "bwrap: execvp /bin/true: No such file or directory",
            NamespaceState.PROBE_ERROR,
        ),
        ("bwrap: Can't find source path /nope", NamespaceState.PROBE_ERROR),
        ("bwrap: Can't mkdir /oldroot/usr", NamespaceState.PROBE_ERROR),
        # Unrecognised: not claimed as a kernel policy we did not observe.
        ("bwrap: something nobody has seen before", NamespaceState.PROBE_ERROR),
    ],
)
def test_a_namespace_failure_is_classified_by_what_it_says(
    message: str, expected: NamespaceState
) -> None:
    assert sandbox.classify_namespace_failure(message) is expected


def test_an_exec_failure_is_never_reported_as_a_kernel_denial() -> None:
    """The specific misdirection: it sent an operator to look at AppArmor.

    "Operation not permitted" appears in the denial vocabulary, so a message
    carrying both markers must still resolve to the more specific reading --
    bubblewrap only reaches execvp after every namespace and mount succeeded.
    """

    both = "bwrap: execvp /bin/true: Operation not permitted"
    assert sandbox.classify_namespace_failure(both) is NamespaceState.PROBE_ERROR


@pytest.mark.parametrize(
    ("namespaces_work", "stderr", "expected"),
    [
        (
            False,
            "bwrap: setting up uid map: Permission denied",
            NamespaceState.BLOCKED,
        ),
        (
            False,
            "bwrap: execvp /bin/true: No such file or directory",
            NamespaceState.PROBE_ERROR,
        ),
        (True, "", NamespaceState.AVAILABLE),
    ],
    ids=["apparmor-denied", "malformed-root-enoent", "namespace-created"],
)
def test_the_probe_reports_the_three_states_end_to_end(
    monkeypatch: pytest.MonkeyPatch,
    namespaces_work: bool,
    stderr: str,
    expected: NamespaceState,
) -> None:
    _fake_bwrap(
        monkeypatch,
        version="bubblewrap 0.12.0",
        namespaces_work=namespaces_work,
        stderr=stderr,
    )
    found = sandbox.probe_bubblewrap()
    assert found.namespace_state is expected
    assert found.namespaces_ok is (expected is NamespaceState.AVAILABLE)
    # Only a real denial may send someone to change AppArmor or sysctl. The
    # probe-error remedy mentions both, to say explicitly *not* to touch them.
    if expected is NamespaceState.PROBE_ERROR:
        assert "not a kernel policy" in found.remedy.lower()
        assert "do not change apparmor or sysctl" in found.remedy.lower()
    elif expected is NamespaceState.BLOCKED:
        assert "apparmor" in found.remedy.lower()
        assert "do not change apparmor" not in found.remedy.lower()


def test_the_probe_binds_what_production_binds() -> None:
    """A probe that builds a different filesystem from production can pass
    while production fails -- or, as here, fail while production would work.

    Binding only `/usr` left no `/lib64` on a usrmerged host, so no dynamically
    linked sentinel could start. Reusing `_OS_PATHS` is what makes the probe's
    filesystem the same shape as a real contained command's.
    """

    argv = sandbox._namespace_probe_argv("/usr/bin/bwrap")
    bound = {argv[i + 1] for i, token in enumerate(argv) if token == "--ro-bind"}
    expected = {path for path in sandbox._OS_PATHS if Path(path).exists()}
    assert bound == expected
    # The strict form, never `--unshare-user-try`.
    assert "--unshare-user" in argv
    assert "--unshare-user-try" not in argv
    # And the network namespace, because a contained command is denied network
    # by default and that denial is a namespace this has to prove it can make.
    assert "--unshare-net" in argv


def test_the_probe_sentinel_exists_on_this_host() -> None:
    sentinel = sandbox._probe_sentinel()
    assert Path(sentinel).exists(), sentinel


def test_the_three_facts_stay_distinct_after_a_backport(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Requirement 8: eligibility is not namespace capability is not validation.

    A host whose *package* is patched but whose kernel still refuses the
    namespace must report eligible, blocked, and not validated -- three
    different answers, and no containment.
    """

    _fake_bwrap(
        monkeypatch,
        version="bubblewrap 0.9.0",
        namespaces_work=False,
        stderr="bwrap: setting up uid map: Permission denied",
    )
    monkeypatch.setattr(
        sandbox,
        "detect_vendor_package",
        lambda _exe: ubuntu_bwrap("0.9.0-1ubuntu0.2"),
    )
    found = sandbox.probe_bubblewrap()
    assert found.security_eligible is True
    assert found.namespaces_ok is False
    assert found.containment_validated is False
    assert found.available is False
