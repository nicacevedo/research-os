"""The ``researchctl lit`` commands, and the known-item regression guard.

The CLI tests run the real argument parser and the real handlers against a real
on-disk store, with only the network faked, so what they exercise is what a
person runs. Every command in the group is read-or-retrieve: none of them can
touch a project capsule, and the evaluation command exists to fail loudly when
ranking quietly degrades.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from research_os.errors import EXIT_ERROR, EXIT_OK, LiteratureError
from research_os.literature import commands as lit_commands
from research_os.literature.evaluation import evaluate, render_report
from research_os.literature.models import AuthorRecord, KnownItem
from research_os.literature.store import LiteratureStore, database_path
from research_os.textsafe import CONTROL_CHARS
from tests.literature_helpers import default_corpus


@pytest.fixture
def literature_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every Research OS directory at a temporary tree."""

    root = tmp_path / "xdg"
    for name, subdirectory in (
        ("RESEARCH_OS_CONFIG_HOME", "config"),
        ("RESEARCH_OS_DATA_HOME", "data"),
        ("RESEARCH_OS_CACHE_HOME", "cache"),
        ("RESEARCH_OS_STATE_HOME", "state"),
    ):
        path = root / subdirectory
        path.mkdir(parents=True)
        monkeypatch.setenv(name, str(path))
    return root / "data"


def run(argv: list[str]) -> int:
    """Parse and dispatch one ``researchctl lit`` invocation."""

    from research_os.cli import _build_parser

    args = _build_parser().parse_args(argv)
    return lit_commands.dispatch(args)


def seed(corpus: list[dict] | None = None) -> LiteratureStore:
    store = LiteratureStore.open()
    for entry in corpus if corpus is not None else default_corpus():
        store.ingest(
            provider=entry.get("provider", "test"),
            payload=entry.get("payload", {}),
            fields=entry["fields"],
            identifiers=entry["identifiers"],
            authors=[
                AuthorRecord(position=index, name=name)
                for index, name in enumerate(entry.get("authors", ()))
            ],
        )
    return store


# -- the commands -------------------------------------------------------------


def test_sources_reports_every_provider_without_printing_a_credential(
    literature_home: Path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from research_os.literature.sources.openalex import API_KEY_ENV

    monkeypatch.setenv(API_KEY_ENV, "super-secret-token")

    code = run(["lit", "sources"])

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "openalex" in printed and "crossref" in printed and "arxiv" in printed
    assert "super-secret-token" not in printed
    assert API_KEY_ENV in printed, "the variable is named; its value is not"


def test_search_makes_no_network_request(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Searching an index is local; only retrieval reaches a provider."""

    import research_os.literature.http as http_module

    store = seed()
    store.close()

    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("lit search reached the network")

    original = http_module.HttpClient.get
    http_module.HttpClient.get = refuse  # type: ignore[assignment]
    try:
        code = run(["lit", "search", "widget", "--limit", "3"])
    finally:
        http_module.HttpClient.get = original  # type: ignore[assignment]

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "Widget dynamics under load" in printed


def test_search_emits_machine_readable_rows_on_request(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed().close()

    run(["lit", "search", "widget", "--json"])

    rows = json.loads(capsys.readouterr().out)
    assert rows
    assert {"work_key", "title", "score", "is_retracted"} <= set(rows[0])


def test_search_hides_a_retracted_work_unless_asked(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed().close()

    run(["lit", "search", "widget dynamics revisited"])
    without = capsys.readouterr().out
    run(["lit", "search", "widget dynamics revisited", "--include-retracted"])
    with_retracted = capsys.readouterr().out

    assert "retracted-widget" not in without
    assert "RETRACTED" in with_retracted


def test_show_prints_the_provenance_of_every_field(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed().close()

    code = run(["lit", "show", "doi:10.1000/widget-dynamics"])

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert "field provenance" in printed
    assert "archived provider payloads" in printed
    assert "in force" in printed


def test_show_refuses_a_work_that_was_never_retrieved(literature_home: Path) -> None:
    seed().close()

    with pytest.raises(LiteratureError, match="retrieve it first"):
        run(["lit", "show", "doi:10.1/never-seen"])


def test_index_rebuilds_the_search_index(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = seed()
    store.connection.execute("DELETE FROM work_fts")
    store.connection.commit()
    store.close()

    code = run(["lit", "index"])

    assert code == EXIT_OK
    assert "indexed 4 work(s)" in capsys.readouterr().out
    run(["lit", "search", "widget"])
    assert "Widget dynamics under load" in capsys.readouterr().out


def test_status_says_where_everything_lives(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed().close()

    code = run(["lit", "status"])

    printed = capsys.readouterr().out
    assert code == EXIT_OK
    assert str(database_path()) in printed
    assert "works indexed   4" in printed


def test_the_group_prints_help_rather_than_failing_with_no_subcommand(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    code = run(["lit"])

    assert code == EXIT_OK
    assert "usage" in capsys.readouterr().out


# -- known-item evaluation ----------------------------------------------------


def test_a_known_item_that_still_ranks_passes(literature_home: Path) -> None:
    store = seed()
    store.record_known_items(
        [
            KnownItem(
                fixture="widgets",
                query="widget deformation load",
                work_key="doi:10.1000/widget-dynamics",
            )
        ]
    )

    report = evaluate(store, "widgets")

    assert report.ok is True
    assert report.mean_recall == 1.0
    assert report.outcomes[0].ranks["doi:10.1000/widget-dynamics"] >= 1


def test_a_known_item_that_stops_being_retrieved_fails_loudly(
    literature_home: Path,
) -> None:
    """This is the regression the fixture exists to catch."""

    store = seed()
    store.record_known_items(
        [
            KnownItem(
                fixture="widgets",
                query="thermal gadget response",
                work_key="doi:10.1000/widget-dynamics",
            )
        ]
    )

    report = evaluate(store, "widgets")

    assert report.ok is False
    assert report.failing
    assert "doi:10.1000/widget-dynamics" in report.failing[0].missing
    assert "not in the top" in render_report(report)


def test_a_known_item_follows_a_merged_work(literature_home: Path) -> None:
    """A fixture written before a merge must not start failing because of it."""

    store = LiteratureStore.open()
    preprint = store.ingest(
        provider="arxiv",
        payload={},
        fields={"title": "Widget deformation", "publication_year": 2020},
        identifiers={"arxiv": "2001.01234"},
        authors=[AuthorRecord(position=0, name="Roe")],
    )
    store.record_known_items(
        [KnownItem(fixture="merge", query="widget deformation", work_key=preprint)]
    )
    store.ingest(
        provider="crossref",
        payload={},
        fields={"title": "Widget deformation", "publication_year": 2020},
        identifiers={"doi": "10.1/x"},
        authors=[AuthorRecord(position=0, name="Roe")],
    )

    report = evaluate(store, "merge")

    assert report.ok is True


def test_an_unknown_fixture_reports_no_queries_rather_than_passing_vacuously(
    literature_home: Path,
) -> None:
    store = seed()

    report = evaluate(store, "does-not-exist")

    assert report.queries == 0
    assert "0 queries" in report.summary()


def test_the_eval_command_exits_non_zero_when_retrieval_regressed(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = seed()
    store.record_known_items(
        [
            KnownItem(
                fixture="widgets",
                query="completely unrelated terms",
                work_key="doi:10.1000/widget-dynamics",
            )
        ]
    )
    store.close()

    code = run(["lit", "eval", "widgets"])

    assert code == EXIT_ERROR
    assert "FAIL" in capsys.readouterr().out


def test_a_fixture_can_be_recorded_and_listed_from_the_cli(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    seed().close()

    run(
        [
            "lit",
            "known",
            "widgets",
            "--query",
            "widget load",
            "--work",
            "doi:10.1000/widget-dynamics",
            "--note",
            "the canonical measurement paper",
        ]
    )
    capsys.readouterr()
    run(["lit", "known", "widgets"])

    printed = capsys.readouterr().out
    assert "widget load" in printed
    assert "doi:10.1000/widget-dynamics" in printed


def test_recording_a_known_item_without_a_query_is_refused(
    literature_home: Path,
) -> None:
    seed().close()

    with pytest.raises(LiteratureError, match="--query is required"):
        run(["lit", "known", "widgets", "--work", "doi:10.1/x"])


# -- display safety -----------------------------------------------------------


def test_provider_text_cannot_move_a_researcher_s_cursor(
    literature_home: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    store = LiteratureStore.open()
    store.ingest(
        provider="test",
        payload={},
        fields={
            "title": "Widgets \x1b[2J and control sequences",
            "abstract": "benign\rTHIS IS WHAT YOU SEE",
            "publication_year": 2024,
        },
        identifiers={"doi": "10.1/hostile"},
        authors=[AuthorRecord(position=0, name="E Adversary")],
    )
    store.close()

    run(["lit", "search", "widgets"])
    printed = capsys.readouterr().out

    assert not any(item in printed for item in CONTROL_CHARS - {"\n", "\t"})
    assert "\\x1b" in printed
