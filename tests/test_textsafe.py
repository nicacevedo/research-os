"""Untrusted text reaches the terminal as text, and the archive keeps the bytes.

A run report quotes strings Research OS did not write. A terminal reads some of
those bytes as commands: ESC opens an ANSI sequence that can clear the screen,
move the cursor back over a line already printed, recolour output, or retitle
the window. A reviewer summary carrying one could make the report a human reads
say something other than what the run actually recorded.

Two halves are pinned here. The rendered view makes such a character visible
instead of acting on it, and the stored artifact still holds the original bytes
so provenance is unaffected.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from research_os.automation.commands import _events, _status
from research_os.automation.models import AutomationRun, Role
from research_os.automation.report import render_plan, render_report, render_status
from research_os.automation.store import RunStore
from research_os.textsafe import CONTROL_CHARS, terminal_safe
from tests.automation_helpers import (
    FIXED_MODULE,
    fake_config,
    init_repo,
    make_controller,
    plan_payload,
    review_payload,
)
from tests.fake_providers import FakeProvider, ScriptedResponse

CLEAR_SCREEN = "\x1b[2J"
SET_TITLE = "\x1b]0;owned\x07"
OVERWRITE = "benign summary\rTHIS IS WHAT YOU SEE"
EIGHT_BIT_CSI = "\x9b31m"


# -- the transformation itself ------------------------------------------------


def test_ordinary_text_is_returned_unchanged() -> None:
    text = "T-001 implemented add()\n\tin adder.py\n"

    assert terminal_safe(text) is text


def test_an_escape_is_shown_rather_than_executed() -> None:
    rendered = terminal_safe(f"summary: {CLEAR_SCREEN}gone")

    assert "\x1b" not in rendered
    assert rendered == "summary: \\x1b[2Jgone"


def test_a_carriage_return_cannot_overwrite_a_printed_line() -> None:
    rendered = terminal_safe(OVERWRITE)

    assert "\r" not in rendered
    assert "benign summary" in rendered
    assert rendered == "benign summary\\x0dTHIS IS WHAT YOU SEE"


def test_an_operating_system_command_is_neutralised() -> None:
    rendered = terminal_safe(SET_TITLE)

    assert "\x1b" not in rendered and "\x07" not in rendered
    assert rendered == "\\x1b]0;owned\\x07"


def test_the_eight_bit_control_introducer_is_covered_too() -> None:
    """0x9B is CSI in an 8-bit terminal, so filtering ESC alone is not enough."""

    rendered = terminal_safe(EIGHT_BIT_CSI)

    assert "\x9b" not in rendered
    assert rendered == "\\x9b31m"


def test_newlines_and_tabs_are_the_report_s_own_structure() -> None:
    assert terminal_safe("a\nb\tc") == "a\nb\tc"


def test_every_other_control_character_is_escaped() -> None:
    hostile = "".join(sorted(CONTROL_CHARS - {"\n", "\t"}))

    rendered = terminal_safe(hostile)

    assert not any(character in rendered for character in CONTROL_CHARS)
    assert rendered.count("\\x") == len(hostile)


def test_what_is_kept_is_configurable_for_a_stricter_caller() -> None:
    assert terminal_safe("a\nb", keep=frozenset()) == "a\\x0ab"


# -- through a real run -------------------------------------------------------


def hostile_plan() -> dict[str, Any]:
    payload = plan_payload()
    payload["summary"] = f"Implement add.{CLEAR_SCREEN}"
    payload["tasks"][0]["title"] = f"Implement add{SET_TITLE}"
    payload["tasks"][0]["goal"] = f"Make add return the sum.{EIGHT_BIT_CSI}"
    return payload


def hostile_review() -> dict[str, Any]:
    return review_payload(
        findings=[
            {
                "severity": "minor",
                "message": OVERWRITE,
                "path": "adder.py",
            }
        ]
    )


@pytest.fixture
def hostile_run(
    automation_home: Path, tmp_path: Path
) -> tuple[RunStore, AutomationRun]:
    repo = init_repo(tmp_path / "project")
    provider = FakeProvider(
        responses={
            str(Role.PLANNER): [ScriptedResponse(structured=hostile_plan())],
            str(Role.CODER): [
                ScriptedResponse(
                    text=f"done{CLEAR_SCREEN}", write_files={"adder.py": FIXED_MODULE}
                )
            ],
            str(Role.REVIEWER): [ScriptedResponse(structured=hostile_review())],
        },
    )
    controller = make_controller({"fake": provider}, config=fake_config())
    store, _ = controller.start(project_path=repo, goal="Implement the function.")
    return store, controller.execute(store)


def test_the_report_carries_no_terminal_control_sequence(
    hostile_run: tuple[RunStore, AutomationRun],
) -> None:
    store, run = hostile_run

    rendered = render_report(run, store)

    assert not any(character in rendered for character in CONTROL_CHARS - {"\n", "\t"})
    assert "\\x1b" in rendered, "the report hid the escape instead of showing it"


def test_status_and_plan_are_sanitised_the_same_way(
    hostile_run: tuple[RunStore, AutomationRun],
) -> None:
    store, run = hostile_run

    for rendered in (render_status(run, store), render_plan(run)):
        assert not any(
            character in rendered for character in CONTROL_CHARS - {"\n", "\t"}
        )


def test_the_reviewer_finding_is_still_readable(
    hostile_run: tuple[RunStore, AutomationRun],
) -> None:
    """Neutralised, not censored: the human still sees what was said."""

    store, run = hostile_run

    rendered = render_report(run, store)

    assert "benign summary" in rendered
    assert "THIS IS WHAT YOU SEE" in rendered


def test_the_archived_model_output_keeps_the_original_bytes(
    hostile_run: tuple[RunStore, AutomationRun],
) -> None:
    """Display safety must not become silent rewriting of the evidence."""

    store, run = hostile_run

    coder = next(item for item in run.invocations if item.role is Role.CODER)
    raw = store.path(*(coder.raw_output_path or "").split("/")).read_text(
        encoding="utf-8"
    )
    assert CLEAR_SCREEN in raw

    reviewed = json.loads(
        store.path("reviews", f"{run.work_orders[0].task_id}.json").read_text(
            encoding="utf-8"
        )
    )
    assert "\r" in reviewed["findings"][0]["message"]


def test_the_event_ledger_view_is_sanitised(
    hostile_run: tuple[RunStore, AutomationRun],
    capsys: pytest.CaptureFixture[str],
) -> None:
    store, run = hostile_run
    import argparse

    _events(argparse.Namespace(run_id=run.run_id, limit=0))

    printed = capsys.readouterr().out
    assert not any(character in printed for character in CONTROL_CHARS - {"\n", "\t"})
    parsed = [json.loads(line) for line in printed.splitlines() if line.strip()]
    assert parsed, "the ledger view printed nothing"
    # The ledger file itself is append-only evidence and is not rewritten: the
    # escape survives round-tripping through it, JSON-escaped rather than lost.
    stored = [
        json.loads(line)
        for line in store.events_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(CLEAR_SCREEN in str(item.get("summary", "")) for item in stored), (
        "the ledger lost the original bytes"
    )


def test_the_json_status_view_stays_parseable_and_control_free(
    hostile_run: tuple[RunStore, AutomationRun],
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The machine views escape rather than rewrite: still JSON, still the data."""

    _, run = hostile_run
    import argparse

    _status(argparse.Namespace(run_id=run.run_id, json=True))

    printed = capsys.readouterr().out
    assert not any(character in printed for character in CONTROL_CHARS - {"\n", "\t"})
    parsed = json.loads(printed)
    assert parsed["run_id"] == run.run_id
    assert CLEAR_SCREEN in parsed["plan_summary"], "the record was rewritten"


def test_the_prompt_boundary_is_untouched_by_the_display_boundary() -> None:
    """They agree on which characters are control characters and nothing else."""

    from research_os.automation import promptdata

    assert promptdata.CONTROL_CHARS is CONTROL_CHARS
    assert promptdata.prompt_safe(f"a{CLEAR_SCREEN}b") == "a [2Jb"


# -- characters that are not control characters and are not text ------------
#
# Written as `chr(...)` throughout. A test about invisible characters that
# embeds them as literals is a test whose own source cannot be reviewed -- and
# ruff refuses the file, for the same reason.
RLO = chr(0x202E)  # RIGHT-TO-LEFT OVERRIDE
ZWSP = chr(0x200B)  # ZERO WIDTH SPACE
TAG_A = chr(0xE0041)  # TAG LATIN CAPITAL LETTER A


def test_a_bidi_override_is_made_visible() -> None:
    """A finding that reads one way and archives another.

    U+202E contains no control character, so ``CONTROL_CHARS`` let it through.
    It reverses the display order of everything after it, which means the
    sentence a researcher reads in a run report is not the sentence stored --
    at the exact boundary the authority model is protecting.
    """

    rendered = terminal_safe(f"the effect is {RLO}real")

    assert RLO not in rendered
    assert rendered == "the effect is \\u202ereal"


def test_zero_width_characters_cannot_hide_inside_an_identifier() -> None:
    """Two ids that render identically are two ids a reader cannot tell apart."""

    assert terminal_safe(f"EXP-0001{ZWSP} vs EXP-0001") == (
        "EXP-0001\\u200b vs EXP-0001"
    )


def test_a_tag_character_is_escaped_as_its_own_code_point() -> None:
    """Above the BMP, so the escape has to widen rather than truncate."""

    assert terminal_safe(f"hi{TAG_A}") == "hi\\U000e0041"


def test_legitimate_text_is_untouched() -> None:
    """The set is deliberately narrow. Accented and non-Latin text is text."""

    for text in (
        "caf\u00e9",
        "\u03a9\u03bc\u03ad\u03b3\u03b1",
        "\u05e2\u05d1\u05e8\u05d9\u05ea",
        "a\tb\nc",
    ):
        assert terminal_safe(text) == text


def test_the_prompt_boundary_replaces_them_with_spaces() -> None:
    """A visible escape is four characters of noise inside a prompt.

    The prompt boundary neutralises rather than displays, which is what the
    rest of ``_scrub_control`` does to every control character.
    """

    from research_os.automation.promptdata import prompt_safe

    assert RLO not in prompt_safe(f"the effect is {RLO}real")
    assert ZWSP not in prompt_safe(f"EXP{ZWSP}-0001")
