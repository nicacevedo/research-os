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
