"""A project named by its path and by its id is one project, in every command.

The first live qualification found the defect this file closes. ``portfolio
enable <repository>`` read the capsule and resolved the repository to
``cg-sparse-regression``. ``portfolio status <repository>``, run a moment
later with the same argument, answered that the project had no portfolio, and
``portfolio pause <repository>`` failed on

    insert or update on table "portfolio_state" violates foreign key
    constraint "portfolio_state_project_id_fkey"

because it had written the literal filesystem path into
``portfolio_state.project_id``. Two resolvers existed. ``enable`` asked the
capsule; everything else asked the global project registry, which
`ARCHITECTURE.md` calls disposable, and fell back to the string it was given
when the registry did not list that path. The live registry was not wrong in
any way the registry can be wrong: it listed the project at an older checkout,
which is exactly the state a disposable discovery index is allowed to be in.

So the tests below run the whole command set through the real CLI against a
real database, with the registry in each of the three states it can be in --
current, stale, and absent -- and assert on the rows, not on the output: every
row that names a project names the capsule's id, and no project column holds
anything shaped like a path.
"""

from __future__ import annotations

import ast
import inspect
import json
import shutil
from pathlib import Path

import pytest

from research_os.cli import main
from research_os.errors import InvalidIdError
from research_os.portfolio import commands as portfolio_commands
from research_os.portfolio.store import PortfolioStore
from research_os.registry import registry_path
from research_os.runtime.config import DSN_ENV
from research_os.runtime.db import Database
from research_os.runtime.store import RuntimeStore
from tests.runtime_graph_helpers import make_capsule
from tests.runtime_helpers import pg_dsn, runtime_db, runtime_xdg

__all__ = ["pg_dsn", "runtime_db", "runtime_xdg"]

PROJECT = "identity-study"


def run_cli(monkeypatch: pytest.MonkeyPatch, *argv: str) -> int:
    monkeypatch.setattr("sys.argv", ["researchctl", *argv])
    try:
        main()
    except SystemExit as exc:
        code = exc.code
        if code is None:
            return 0
        return code if isinstance(code, int) else 1
    return 0


def project_columns(db: Database) -> list[tuple[str, str]]:
    """Every ``(table, column)`` whose meaning is "a canonical project id".

    Read from the catalogue rather than listed by hand, so a table added
    later is covered without anyone remembering to add it here.
    """

    with db.tx() as conn:
        rows = conn.execute(
            "select table_name, column_name from information_schema.columns "
            "where table_schema = current_schema() and column_name = 'project_id' "
            "and table_name in (select table_name from information_schema.tables "
            "where table_schema = current_schema() and table_type = 'BASE TABLE') "
            "order by 1"
        ).fetchall()
    return [(row["table_name"], row["column_name"]) for row in rows]


def project_values(db: Database) -> dict[str, set[str]]:
    values: dict[str, set[str]] = {}
    with db.tx() as conn:
        for table, column in project_columns(db):
            found = {
                row["value"]
                for row in conn.execute(
                    f"select distinct {column} as value from {table} "
                    f"where {column} is not null"
                ).fetchall()
            }
            if found:
                values[table] = found
    return values


def assert_one_canonical_project(db: Database, project_id: str) -> None:
    values = project_values(db)
    assert values, "nothing was written at all, so nothing was tested"
    for table, found in values.items():
        assert found == {project_id}, (table, found)
        assert not any("/" in value for value in found), (table, found)


@pytest.fixture
def cli_env(
    pg_dsn: str, runtime_db: Database, monkeypatch: pytest.MonkeyPatch
) -> Database:
    monkeypatch.setenv(DSN_ENV, pg_dsn)
    return runtime_db


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    return make_capsule(tmp_path / "canonical-checkout", project_id=PROJECT)


def _drive_every_command(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    db: Database,
    repository: Path,
) -> None:
    """The mission's sequence, by path, with the id used once as the control."""

    path = str(repository)
    store = PortfolioStore(db)

    assert run_cli(monkeypatch, "portfolio", "enable", path) == 0
    assert f"portfolio enabled for {PROJECT}" in capsys.readouterr().out
    assert_one_canonical_project(db, PROJECT)

    assert run_cli(monkeypatch, "portfolio", "status", path, "--json") == 0
    by_path = json.loads(capsys.readouterr().out)
    assert by_path["project"] == PROJECT
    assert by_path["status"] == "RUNNING"
    assert by_path["scheduled"] is True

    assert run_cli(monkeypatch, "portfolio", "status", PROJECT, "--json") == 0
    by_id = json.loads(capsys.readouterr().out)
    assert by_id == by_path

    assert run_cli(monkeypatch, "portfolio", "top", path) == 0
    assert "PIDEA" in capsys.readouterr().out

    assert run_cli(monkeypatch, "portfolio", "pause", path, "--reason", "a pause") == 0
    assert f"{PROJECT} paused" in capsys.readouterr().out
    state = store.get_state(PROJECT)
    assert state is not None and state.status.name == "PAUSED_BY_RESEARCHER"
    assert run_cli(monkeypatch, "portfolio", "status", PROJECT, "--json") == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PAUSED_BY_RESEARCHER"

    assert run_cli(monkeypatch, "portfolio", "resume", path) == 0
    assert f"{PROJECT} resumed" in capsys.readouterr().out
    state = store.get_state(PROJECT)
    assert state is not None and state.status.name == "RUNNING"

    # `top` produced a digest record, so `digest` has one to show -- and it
    # can only find it under the canonical id.
    latest = store.latest_digest(PROJECT)
    assert latest is not None
    assert run_cli(monkeypatch, "portfolio", "digest", path) == 0
    assert latest.digest_id in capsys.readouterr().out
    assert run_cli(monkeypatch, "portfolio", "digest", path, "list") == 0
    assert latest.digest_id in capsys.readouterr().out

    for view in ("list", "rejected", "validated", "human-ready"):
        assert run_cli(monkeypatch, "ideas", view, path) == 0
        capsys.readouterr()
    assert run_cli(monkeypatch, "seed", "add", path, "--text", "a direction") == 0
    capsys.readouterr()
    assert len(store.pending_seeds(project_id=PROJECT)) == 1
    assert run_cli(monkeypatch, "seed", "list", path) == 0
    assert "a direction" in capsys.readouterr().out

    # One project, by every name, in every table that names one.
    assert_one_canonical_project(db, PROJECT)
    stored = RuntimeStore(db).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()


# ------------------------------------------------ the three registry states --
def test_a_registered_project_is_one_project_by_path_and_by_id(
    cli_env: Database,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The documented order: register, then enable, then everything else."""

    assert run_cli(monkeypatch, "register-project", str(repository)) == 0
    capsys.readouterr()
    _drive_every_command(monkeypatch, capsys, cli_env, repository)


def test_a_stale_registry_cannot_split_a_project_in_two(
    cli_env: Database,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The live failure, reproduced: the registry lists an older checkout.

    On 2026-09-24 the qualification's data home carried a registry written
    by an earlier dogfood, which listed ``cg-sparse-regression`` at a clone
    under the dogfood's state directory. The researcher then enabled the
    portfolio on the canonical repository. Every command after ``enable``
    looked the canonical path up in that registry, found nothing, and
    carried on with the path as the project id.
    """

    older = make_capsule(tmp_path / "older-clone", project_id=PROJECT)
    assert run_cli(monkeypatch, "register-project", str(older)) == 0
    capsys.readouterr()
    listed = json.loads(registry_path().read_text(encoding="utf-8"))["projects"]
    assert [Path(item["path"]) for item in listed] == [older.resolve()]

    _drive_every_command(monkeypatch, capsys, cli_env, repository)


def test_enabling_by_id_never_re_points_a_project_at_a_stale_registry_entry(
    cli_env: Database,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The operational table outranks the registry for an id, and must.

    ``enable`` writes the repository path it resolved. With the registry
    consulted first, ``portfolio enable cg-sparse-regression`` typed after
    the live session would have moved the project back to the dogfood's
    clone -- and the Curator, the experiments and every capsule read would
    have followed it there, attributed to the canonical project's id.
    """

    older = make_capsule(tmp_path / "older-clone", project_id=PROJECT)
    assert run_cli(monkeypatch, "register-project", str(older)) == 0
    assert run_cli(monkeypatch, "portfolio", "enable", str(repository)) == 0
    assert run_cli(monkeypatch, "portfolio", "enable", PROJECT) == 0
    capsys.readouterr()
    stored = RuntimeStore(cli_env).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()
    assert_one_canonical_project(cli_env, PROJECT)


def test_a_deleted_registry_changes_nothing(
    cli_env: Database,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`ARCHITECTURE.md`: deleting the registry must never stop a project working."""

    assert not registry_path().exists()
    _drive_every_command(monkeypatch, capsys, cli_env, repository)


def test_a_subdirectory_of_the_repository_names_the_same_project(
    cli_env: Database,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A researcher standing in ``.research/`` is standing in their project."""

    assert run_cli(monkeypatch, "portfolio", "enable", str(repository)) == 0
    capsys.readouterr()
    inside = repository / ".research"
    assert run_cli(monkeypatch, "portfolio", "pause", str(inside)) == 0
    capsys.readouterr()
    state = PortfolioStore(cli_env).get_state(PROJECT)
    assert state is not None and state.status.name == "PAUSED_BY_RESEARCHER"
    assert_one_canonical_project(cli_env, PROJECT)


# ------------------------------------- an id outranks the working directory --
# An independent review of the first version of this repair found that the
# single resolver asked the filesystem before the database, so an id that
# named something in the working directory was read as a path and resolved to
# whichever repository enclosed it. Each test below failed on that version.
def test_an_id_that_names_a_file_in_another_project_still_names_its_project(
    cli_env: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Standing in project A, with a file called ``b-study``: pause B, not A."""

    a = make_capsule(tmp_path / "a", project_id="a-study")
    b = make_capsule(tmp_path / "b", project_id="b-study")
    assert run_cli(monkeypatch, "portfolio", "enable", str(a)) == 0
    assert run_cli(monkeypatch, "portfolio", "enable", str(b)) == 0
    (a / "b-study").write_text("notes on the sibling project\n", encoding="utf-8")
    (a / "c-study").mkdir()
    monkeypatch.chdir(a)
    capsys.readouterr()

    assert run_cli(monkeypatch, "portfolio", "pause", "b-study", "--reason", "x") == 0
    assert "b-study paused" in capsys.readouterr().out
    store = PortfolioStore(cli_env)
    assert store.get_state("b-study").status.name == "PAUSED_BY_RESEARCHER"
    assert store.get_state("a-study").status.name == "RUNNING"

    assert run_cli(monkeypatch, "seed", "add", "b-study", "--text", "for B") == 0
    capsys.readouterr()
    assert len(store.pending_seeds(project_id="b-study")) == 1
    assert len(store.pending_seeds(project_id="a-study")) == 0

    # And the escape hatch still works: `./b-study` is unmistakably a path,
    # and a path inside A is A.
    assert run_cli(monkeypatch, "portfolio", "status", "./b-study", "--json") == 0
    assert json.loads(capsys.readouterr().out)["project"] == "a-study"


def test_an_enabled_id_survives_a_same_named_directory_in_the_working_directory(
    cli_env: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """A plain directory called ``b-study`` is not a reason to refuse ``b-study``."""

    b = make_capsule(tmp_path / "b", project_id="b-study")
    assert run_cli(monkeypatch, "portfolio", "enable", str(b)) == 0
    elsewhere = tmp_path / "scratch"
    (elsewhere / "b-study").mkdir(parents=True)
    monkeypatch.chdir(elsewhere)
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "status", "b-study", "--json") == 0
    assert json.loads(capsys.readouterr().out)["project"] == "b-study"


def test_enable_by_id_next_to_an_old_clone_keeps_the_project_where_it_is(
    cli_env: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``~/research/delta-study`` is an older clone; the project runs elsewhere."""

    canonical = make_capsule(tmp_path / "work" / "canonical", project_id="delta-study")
    home = tmp_path / "research"
    make_capsule(home / "delta-study", project_id="delta-study")
    assert run_cli(monkeypatch, "portfolio", "enable", str(canonical)) == 0
    monkeypatch.chdir(home)
    assert run_cli(monkeypatch, "portfolio", "enable", "delta-study") == 0
    assert "re-pointed" not in capsys.readouterr().out
    stored = RuntimeStore(cli_env).get_project("delta-study")
    assert stored is not None
    assert Path(stored.repo_path).resolve() == canonical.resolve()


def test_a_bare_command_means_the_capsule_underfoot_not_the_registry(
    cli_env: Database,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No argument, standing in the canonical checkout, with the live registry.

    The registry's one entry is the dogfood's older clone. A bare ``portfolio
    enable`` used to take that entry's id and then its path, and bound the
    older clone -- from inside the canonical repository.
    """

    older = make_capsule(tmp_path / "older-clone", project_id=PROJECT)
    assert run_cli(monkeypatch, "register-project", str(older)) == 0
    monkeypatch.chdir(repository / ".research")
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "enable") == 0
    capsys.readouterr()
    stored = RuntimeStore(cli_env).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()
    assert_one_canonical_project(cli_env, PROJECT)

    # Standing nowhere in particular, the one registered project is still
    # the default -- by id, so the operational row decides where it lives.
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    assert run_cli(monkeypatch, "portfolio", "status", "--json") == 0
    assert json.loads(capsys.readouterr().out)["project"] == PROJECT
    assert run_cli(monkeypatch, "portfolio", "enable") == 0
    capsys.readouterr()
    stored = RuntimeStore(cli_env).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()


def test_enable_never_writes_a_repository_whose_capsule_is_another_project(
    cli_env: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The registry lists alpha at X; X has since become beta's checkout."""

    x = make_capsule(tmp_path / "x", project_id="alpha-study")
    assert run_cli(monkeypatch, "register-project", str(x)) == 0
    shutil.rmtree(x)
    make_capsule(tmp_path / "x", project_id="beta-study")
    capsys.readouterr()

    assert run_cli(monkeypatch, "portfolio", "enable", "alpha-study") != 0
    captured = capsys.readouterr()
    assert "beta-study" in captured.out + captured.err
    assert project_values(cli_env) == {}


def test_enable_by_a_registered_id_says_which_repository_it_bound(
    cli_env: Database,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The registry route is kept -- `register-project` printed that id -- and
    it is never silent about which checkout it chose."""

    assert run_cli(monkeypatch, "register-project", str(repository)) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "enable", PROJECT) == 0
    assert f"repository {repository.resolve()}" in capsys.readouterr().out
    stored = RuntimeStore(cli_env).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()


def test_re_pointing_a_project_at_another_checkout_is_said_out_loud(
    cli_env: Database,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """Legitimate -- the canonical checkout after a trial clone -- and not silent."""

    trial = make_capsule(tmp_path / "trial-clone", project_id=PROJECT)
    assert run_cli(monkeypatch, "portfolio", "enable", str(trial)) == 0
    capsys.readouterr()
    assert run_cli(monkeypatch, "portfolio", "enable", str(repository)) == 0
    out = capsys.readouterr().out
    assert f"re-pointed from {trial.resolve()}" in out
    stored = RuntimeStore(cli_env).get_project(PROJECT)
    assert stored is not None
    assert Path(stored.repo_path).resolve() == repository.resolve()


def test_a_budget_ceiling_lands_on_the_project_named_not_the_one_underfoot(
    cli_env: Database,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """`researchctl runtime` shares the rule, and budget authority is why.

    Standing in A with a file called ``b-study``, ``runtime budget b-study``
    set *A's* standing ceiling on the version that asked the filesystem
    first.
    """

    from research_os.runtime.budgets import BudgetLedger, Dimension
    from research_os.runtime.models import BudgetScope

    a = make_capsule(tmp_path / "a", project_id="a-study")
    b = make_capsule(tmp_path / "b", project_id="b-study")
    assert run_cli(monkeypatch, "portfolio", "enable", str(a)) == 0
    assert run_cli(monkeypatch, "portfolio", "enable", str(b)) == 0
    (a / "b-study").write_text("notes\n", encoding="utf-8")
    monkeypatch.chdir(a)
    capsys.readouterr()
    code = run_cli(monkeypatch, "runtime", "budget", "b-study", "--max-cost-usd", "7.5")
    assert code == 0, capsys.readouterr()
    ledger = BudgetLedger(cli_env)

    def ceiling(project_id: str):
        record = ledger.get(
            scope=BudgetScope.PROJECT,
            scope_id=project_id,
            dimension=Dimension.MODEL_COST_USD,
        )
        return None if record is None else record.limit_value

    assert ceiling("b-study") is not None
    assert float(ceiling("b-study")) == 7.5
    assert ceiling("a-study") is None or float(ceiling("a-study")) != 7.5


# ------------------------------------------------------------- refusals --
@pytest.mark.parametrize(
    "argv",
    [
        ("portfolio", "pause", "{missing}"),
        ("portfolio", "resume", "{missing}"),
        ("portfolio", "status", "{missing}"),
        ("portfolio", "top", "{missing}"),
        ("portfolio", "digest", "{missing}"),
        ("ideas", "list", "{missing}"),
        ("seed", "add", "{missing}", "--text", "x"),
        ("seed", "list", "{missing}"),
    ],
)
def test_a_path_that_names_no_project_writes_nothing(
    cli_env: Database,
    repository: Path,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    argv: tuple[str, ...],
) -> None:
    """A mistyped path is refused, and is never somebody's project id.

    Including the read-only views: ``top`` produces a digest record, which
    is a write, and the old ``status`` answered "no portfolio yet" about a
    project that has one -- a false statement about the present.
    """

    assert run_cli(monkeypatch, "portfolio", "enable", str(repository)) == 0
    capsys.readouterr()
    before = project_values(cli_env)
    missing = str(tmp_path / "no-such-checkout")
    code = run_cli(monkeypatch, *(item.format(missing=missing) for item in argv))
    captured = capsys.readouterr()
    assert code != 0
    assert missing in captured.err + captured.out
    assert project_values(cli_env) == before


@pytest.mark.parametrize(
    ("verb", "succeeds"),
    [("pause", False), ("resume", False), ("top", True)],
)
def test_a_project_without_an_operational_row_is_told_so_and_nothing_is_written(
    cli_env: Database,
    repository: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    verb: str,
    succeeds: bool,
) -> None:
    """Registered but never enabled: a sentence, not a foreign-key violation.

    ``top`` included because it records a digest, which is a write, and a
    view should not fail where ``status`` answers. ``pause`` and ``resume``
    exit non-zero: what was asked for did not happen.
    """

    assert run_cli(monkeypatch, "register-project", str(repository)) == 0
    capsys.readouterr()
    code = run_cli(monkeypatch, "portfolio", verb, str(repository))
    captured = capsys.readouterr()
    assert (code == 0) is succeeds
    assert "portfolio enable" in captured.out
    assert "foreign key" not in captured.out + captured.err
    assert project_values(cli_env) == {}


# ------------------------------------------------ the database boundary --
@pytest.mark.parametrize(
    "value",
    [
        "/home/researcher/column-generation-for-large-scale-feature-selection",
        "relative/checkout",
        "./checkout",
        "Upper-Case",
        "",
    ],
)
def test_the_projects_table_refuses_anything_that_is_not_a_project_id(
    runtime_db: Database, value: str
) -> None:
    """Every ``project_id`` column references ``projects``, so this is the gate.

    Checked against the catalogue below, and enforced where the one row
    everything else hangs from is written -- with the pattern the capsule
    itself enforces on ``project.yaml``, so the two can never disagree about
    what a project id is.
    """

    with pytest.raises(InvalidIdError):
        RuntimeStore(runtime_db).upsert_project(project_id=value, repo_path="/tmp/x")
    assert RuntimeStore(runtime_db).list_projects() == ()


def test_every_project_column_hangs_from_the_projects_table(
    runtime_db: Database,
) -> None:
    """Why validating ``projects`` is sufficient: nothing else is a root.

    If a later migration adds a ``project_id`` column without the foreign key,
    a path could be written there without passing the gate above, and this
    fails naming the table.
    """

    with runtime_db.tx() as conn:
        referenced = {
            row["table_name"]
            for row in conn.execute(
                """
                select kcu.table_name
                  from information_schema.table_constraints tc
                  join information_schema.key_column_usage kcu
                    on tc.constraint_name = kcu.constraint_name
                   and tc.table_schema = kcu.table_schema
                  join information_schema.constraint_column_usage ccu
                    on tc.constraint_name = ccu.constraint_name
                   and tc.table_schema = ccu.table_schema
                 where tc.constraint_type = 'FOREIGN KEY'
                   and kcu.column_name = 'project_id'
                   and ccu.table_name = 'projects'
                   and ccu.column_name = 'project_id'
                   and tc.table_schema = current_schema()
                """
            ).fetchall()
        }
    columns = {table for table, _ in project_columns(runtime_db)}
    assert columns, "the catalogue query found nothing, so nothing was tested"
    assert columns - referenced == {"projects"}


# --------------------------------------------------- one resolver, used --
def test_every_command_that_takes_a_project_uses_the_one_resolver() -> None:
    """Structural, because the defect was a second resolver nobody noticed.

    Every handler in ``portfolio/commands.py`` that reads ``args.project``
    must pass it to :func:`_resolve_project` and to nothing else. A new
    handler that reached for ``args.project`` directly would reintroduce the
    split this file exists to close, and would pass every behavioural test
    that happened not to exercise it with a path.
    """

    source = inspect.getsource(portfolio_commands)
    tree = ast.parse(source)
    handlers = {
        node.name: node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name.startswith("_")
    }
    readers: dict[str, list[ast.AST]] = {}
    for name, node in handlers.items():
        for child in ast.walk(node):
            if (
                isinstance(child, ast.Attribute)
                and child.attr == "project"
                and isinstance(child.value, ast.Name)
                and child.value.id == "args"
            ):
                readers.setdefault(name, []).append(child)
    assert readers, "no handler reads args.project, so nothing was tested"
    expected = {
        "_enable",
        "_status",
        "_top",
        "_pause",
        "_resume",
        "_digest",
        "_list",
        "_seed_add",
        "_seed_list",
    }
    assert set(readers) == expected
    for name in expected:
        uses = readers[name]
        calls = [
            child
            for child in ast.walk(handlers[name])
            if isinstance(child, ast.Call)
            and isinstance(child.func, ast.Name)
            and child.func.id == "_resolve_project"
        ]
        assert len(calls) == 1, name
        (call,) = calls
        # The only read of args.project is the argument to the resolver.
        assert len(uses) == 1, name
        assert call.args and call.args[0] is uses[0], name
    # And there is exactly one resolver left to call.
    assert "_project_and_repo" not in handlers
    assert "_project" not in handlers
