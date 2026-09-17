"""The package's own metadata, checked so it cannot drift apart.

Two files state this project's version -- ``pyproject.toml`` and
``research_os.__init__`` -- and nothing but agreement between them makes either
true. They were already inconsistent with the shipped release once: the system
was tagged and announced as v1 while both still said ``0.1.0``, which is the
kind of thing nobody notices until someone reports a bug against a version that
does not exist.

The version also travels: it is in the ``User-Agent`` every literature request
carries, which is how a provider identifies a client that is behaving badly.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import research_os
from research_os.literature.http import user_agent

ROOT = Path(__file__).resolve().parent.parent


def pyproject() -> dict:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


def test_the_declared_version_and_the_importable_version_agree() -> None:
    assert pyproject()["project"]["version"] == research_os.__version__


def test_the_version_is_a_release_version_not_a_placeholder() -> None:
    major, minor, patch = research_os.__version__.split(".")
    assert major.isdigit() and minor.isdigit() and patch.isdigit()
    assert int(major) >= 1, (
        "a system documented as an operational release must not ship a 0.x "
        "version; a reader takes 0.x to mean the interface may move without "
        "notice"
    )


def test_the_version_reaches_the_user_agent_providers_see() -> None:
    assert f"research-os/{research_os.__version__}" in user_agent(None)
    assert f"research-os/{research_os.__version__}" in user_agent("a@b.invalid")


def test_the_console_script_is_declared() -> None:
    scripts = pyproject()["project"].get("scripts", {})
    assert scripts.get("researchctl") == "research_os.cli:main"


def test_the_package_declares_a_license_or_this_test_says_so() -> None:
    """A public repository with no licence grants no rights to anyone.

    Not an assertion that a licence exists, because choosing one is the
    author's decision and not a thing an automated change may make for them.
    It asserts the *consistency* of whatever the answer is: metadata claiming a
    licence that is not in the tree would be worse than no licence at all.
    """

    declared = pyproject()["project"].get("license")
    files = [
        path.name
        for path in ROOT.iterdir()
        if path.name.upper().startswith(("LICENSE", "LICENCE", "COPYING"))
    ]
    if declared is None:
        assert not files, (
            f"pyproject declares no license but {files} exists; the package "
            "metadata and the repository must not disagree about the terms"
        )
    else:
        assert files, (
            f"pyproject declares license {declared!r} but no LICENSE file is in "
            "the tree, so nothing states the terms it refers to"
        )


def test_every_migration_ships_in_the_package() -> None:
    """A migration that is not packaged is a database an installed copy cannot build.

    The runtime's schema is numbered SQL files discovered from
    ``research_os/runtime/sql/`` at runtime, and they are *data* rather than
    modules. A build backend that shipped only ``.py`` files would produce a
    wheel that imports fine, starts fine, and fails at ``runtime migrate`` on
    somebody else's machine with "no migration files found" — which is the kind
    of defect that cannot happen when you run from source and always happens on
    first install.

    Asserted against the declared package data rather than by building a wheel:
    a build takes seconds and this needs to run in every suite. The build itself
    was verified once by hand, and the property that keeps it true is that the
    backend includes non-Python files under the package root.
    """

    from research_os.runtime.migrations import discover, sql_dir

    found = discover()
    assert found, "no migrations discovered; the path in `sql_dir` is wrong"
    on_disk = sorted(path.name for path in sql_dir().glob("*.sql"))
    assert [migration.path.name for migration in found] == on_disk

    # The package directory is inside the importable package, which is what
    # makes it shippable at all. A migration directory beside `src/` would be
    # discovered in a source checkout and absent from every install.
    import research_os

    package_root = Path(research_os.__file__).resolve().parent
    assert package_root in sql_dir().resolve().parents


def test_the_declared_schema_version_is_the_highest_shipped_migration() -> None:
    """Duplicated in `test_runtime_schema.py`, and deliberately also here.

    That copy needs a database and skips without one. This one does not, so a
    packaging change that dropped a migration file fails a test that always
    runs.
    """

    from research_os.runtime import RUNTIME_SCHEMA_VERSION
    from research_os.runtime.migrations import discover

    assert RUNTIME_SCHEMA_VERSION == max(m.version for m in discover())


# --- the shipped service units ----------------------------------------------
DEPLOY = Path(__file__).resolve().parents[1] / "deploy"


def test_the_shipped_units_are_not_installed_by_anything() -> None:
    """Enabling a persistent service is the researcher's decision.

    AGENTS.md rule 13. Checked here because "we ship it, we never install it"
    is the kind of promise that decays silently: the units are useless unless
    somebody copies them, and the temptation is to make that automatic.
    """

    import ast

    source = Path(__file__).resolve().parents[1] / "src"
    installers = []
    for module in sorted(source.rglob("*.py")):
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
                continue
            value = node.value
            # An argv element rather than a mention: the daemon's own error
            # message tells a researcher to run `systemctl --user status`, and
            # telling someone a command is not running it.
            if value == "systemctl" or "systemd/user" in value:
                installers.append(f"{module.relative_to(source)}: {value!r}")
    assert installers == []


def test_the_control_plane_unit_can_write_the_researchers_repositories() -> None:
    """A regression, and it cost every write task on a service-run daemon.

    The unit shipped with ``ProtectHome=read-only``, which reads like good
    hardening and is not: ``git worktree add`` writes into the *source*
    repository's ``.git/worktrees/``, under the researcher's project directory.
    Every automation write task on a systemd-run daemon would have failed on a
    read-only filesystem, and the failure would have looked like a Git problem.
    """

    unit = (DEPLOY / "researchd.service").read_text(encoding="utf-8")
    directives = _directives(unit)
    assert directives.get("ProtectHome") == "false"
    # The rest of the hardening is still expected to be there.
    for name in ("NoNewPrivileges", "ProtectKernelTunables", "RestrictSUIDSGID"):
        assert directives.get(name) == "true", name
    assert directives.get("ProtectSystem") == "strict"


def test_a_crash_looping_control_plane_reaches_an_explicit_failure() -> None:
    """`Restart=on-failure` with no start limit retries a broken config forever.

    Which leaves `systemctl --user status` reporting "activating" rather than
    the truth, and nothing for a person to notice.
    """

    directives = _directives((DEPLOY / "researchd.service").read_text(encoding="utf-8"))
    assert directives.get("Restart") == "on-failure"
    assert int(directives["StartLimitBurst"]) > 0
    assert int(directives["StartLimitIntervalSec"].removesuffix("s")) > 0


def test_the_units_run_frozen_so_a_service_start_matches_what_was_tested() -> None:
    for name in ("researchd.service", "researchd-db.service"):
        text = (DEPLOY / name).read_text(encoding="utf-8")
        for line in text.splitlines():
            if line.startswith(("ExecStart=", "ExecStop=")) and "uv run" in line:
                assert "--frozen" in line, f"{name}: {line}"


def test_the_database_unit_is_ordered_before_the_control_plane() -> None:
    """Otherwise a reboot brings the daemon up against no database."""

    control = _directives((DEPLOY / "researchd.service").read_text(encoding="utf-8"))
    assert "researchd-db.service" in control.get("Wants", "")
    # `Wants`, not `Requires`: a runtime pointed at a real PostgreSQL has no
    # such unit installed, and a missing wanted unit must not block startup.
    assert "researchd-db.service" not in control.get("Requires", "")


def _directives(unit: str) -> dict[str, str]:
    """Parse a unit file's ``Key=Value`` lines, last one winning."""

    found: dict[str, str] = {}
    for line in unit.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith(("#", ";", "[")):
            continue
        key, _, value = stripped.partition("=")
        found[key.strip()] = value.strip()
    return found
