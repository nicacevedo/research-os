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
    SandboxProbe,
    available_backend,
    parse_version,
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
    base: dict[str, object] = {
        "technology": "bubblewrap",
        "executable": "/usr/bin/bwrap",
        "namespaces_ok": True,
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
    minimum, advisory = SECURITY_FLOORS["bubblewrap"]
    assert minimum == (0, 12, 0)
    assert "CVE-2026-87766" in advisory
    assert "GHSA-pxhw-h44j-8pfx" in advisory


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
                namespaces_ok=True,
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

    monkeypatch.setattr(sandbox.shutil, "which", lambda _name: "/usr/bin/bwrap")
    monkeypatch.setattr(sandbox, "_is_setuid", lambda _path: setuid)

    def fake_run(argv: list[str], **_kwargs: object) -> object:
        if "--version" in argv:
            return subprocess.CompletedProcess(argv, 0, stdout=version, stderr="")
        return subprocess.CompletedProcess(
            argv, 0 if namespaces_work else 1, stdout="", stderr=stderr
        )

    monkeypatch.setattr(sandbox.subprocess, "run", fake_run)
