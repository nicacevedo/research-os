"""Dependency direction, asserted rather than documented.

```text
graphs  ->  capability interfaces  ->  runtime/adapters  ->  external systems
```

Four properties, each of which has a specific failure it prevents:

- the scientific kernel must not depend on the runtime, or the kernel can no
  longer be installed, reasoned about or tested on its own;
- a graph node must not name a provider, or model identity has been hard-coded
  into graph semantics and routing has nothing left to route;
- a graph node must not build a scheduler command, or the graph has become
  Slurm-specific and the local executor is a fiction;
- importing ``research_os.runtime`` must not import LangGraph or psycopg, or the
  ``runtime`` extra is not optional and the kernel install has grown a
  dependency tree it was promised it would not.
"""

from __future__ import annotations

import ast
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "research_os"
RUNTIME_DIR = SRC / "runtime"

#: The v1 packages that are the scientific kernel and the layers above it.
#: None of them may import the runtime.
NON_RUNTIME_PACKAGES = (
    "capsule.py",
    "validate.py",
    "models.py",
    "review.py",
    "digests.py",
    "ids.py",
    "registry.py",
    "runlock.py",
    "errors.py",
    "paths.py",
    "diagnostics.py",
    "textsafe.py",
)


def _imports(path: Path) -> list[tuple[int, str]]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            found.append((node.lineno, node.module))
        elif isinstance(node, ast.Import):
            found.extend((node.lineno, alias.name) for alias in node.names)
    return found


def test_the_scientific_kernel_does_not_import_the_runtime() -> None:
    offenders = [
        f"{name}:{lineno}: imports {module}"
        for name in NON_RUNTIME_PACKAGES
        for lineno, module in _imports(SRC / name)
        if module.startswith("research_os.runtime")
    ]
    assert offenders == [], (
        "the kernel must be installable and testable without the runtime: "
        + "; ".join(offenders)
    )


def test_no_v1_layer_imports_the_runtime() -> None:
    """The runtime wraps the v1 layers. Nothing may wrap it back."""

    offenders: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        if RUNTIME_DIR in path.parents or path.parent == RUNTIME_DIR:
            continue
        offenders.extend(
            f"{path.relative_to(SRC)}:{lineno}: imports {module}"
            for lineno, module in _imports(path)
            if module.startswith("research_os.runtime")
        )
    # cli.py is allowed to, because it is the composition root.
    offenders = [line for line in offenders if not line.startswith("cli.py")]
    assert offenders == [], "; ".join(offenders)


def _graph_sources() -> list[Path]:
    graphs = RUNTIME_DIR / "graphs"
    return sorted(graphs.rglob("*.py")) if graphs.exists() else []


def test_no_graph_node_names_a_model_provider() -> None:
    """Model identity must not be hard-coded into graph semantics (§20)."""

    vendors = (
        "claude",
        "codex",
        "gemini",
        "anthropic",
        "openai",
        "gpt-",
        "opus",
        "sonnet",
    )
    offenders: list[str] = []
    for path in _graph_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.strip()
            if stripped.startswith("#") or '"""' in stripped:
                continue
            lowered = stripped.lower()
            offenders.extend(
                f"{path.name}:{number}: names {vendor!r}"
                for vendor in vendors
                if vendor in lowered
            )
    assert offenders == [], (
        "a graph node asks for a capability, never a vendor: " + "; ".join(offenders)
    )


def test_no_graph_node_builds_a_scheduler_command() -> None:
    """A graph submits an ExecutionSpec; the executor owns sbatch."""

    scheduler_tokens = ("sbatch", "squeue", "sacct", "scancel", "#SBATCH")
    offenders: list[str] = []
    for path in _graph_sources():
        for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if line.strip().startswith("#"):
                continue
            offenders.extend(
                f"{path.name}:{number}: mentions {token}"
                for token in scheduler_tokens
                if token in line
            )
    assert offenders == [], "; ".join(offenders)


@pytest.mark.parametrize("heavy", ["langgraph", "psycopg", "psycopg_pool"])
def test_importing_the_runtime_package_does_not_import_the_heavy_dependency(
    heavy: str,
) -> None:
    """The ``runtime`` extra stays optional.

    Run in a subprocess because this process has already imported everything.
    """

    program = f"import sys, research_os.runtime;print({heavy!r} in sys.modules)"
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert completed.stdout.strip() == "False", (
        f"importing research_os.runtime pulled in {heavy}"
    )


def test_the_kernel_imports_without_the_runtime_extra_installed() -> None:
    """A two-dependency install must still import and build its parser.

    The blocker uses ``find_spec``, not ``find_module``. The first version of
    this test used ``find_module``, which Python 3.12 ignores entirely -- so it
    blocked nothing, passed happily, and hid the fact that
    ``research_os.cli`` had grown a hard dependency on psycopg through the
    runtime command module. A test that cannot fail is worse than no test.
    """

    program = (
        "import sys\n"
        "from importlib.abc import MetaPathFinder\n"
        "BLOCKED = {'psycopg', 'psycopg_pool', 'langgraph', 'langchain_core'}\n"
        "class Blocker(MetaPathFinder):\n"
        "    def find_spec(self, name, path=None, target=None):\n"
        "        if name.split('.')[0] in BLOCKED:\n"
        "            raise ModuleNotFoundError(f'blocked for this test: {name}')\n"
        "        return None\n"
        "sys.meta_path.insert(0, Blocker())\n"
        # Prove the blocker actually blocks, so this test cannot go vacuous again.
        "try:\n"
        "    import psycopg\n"
        "except ModuleNotFoundError:\n"
        "    pass\n"
        "else:\n"
        "    raise SystemExit('the blocker did not block')\n"
        "import research_os, research_os.cli, research_os.capsule, research_os.validate\n"
        "research_os.cli._build_parser()\n"
        "print('ok')\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )
    assert completed.returncode == 0, completed.stderr
    assert completed.stdout.strip().endswith("ok")


def test_importing_the_cli_does_not_connect_to_anything() -> None:
    """Registering the runtime commands must not open a database.

    ``researchctl --help`` on a machine with no PostgreSQL has to work, and the
    person most likely to run it is someone finding out what this thing is.
    """

    program = (
        "import sys, research_os.cli\n"
        "research_os.cli._build_parser()\n"
        "print('psycopg' in sys.modules, 'langgraph' in sys.modules)\n"
    )
    completed = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    assert completed.stdout.strip() == "False False", completed.stdout
