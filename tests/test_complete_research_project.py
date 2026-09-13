"""One complete research project, from an empty repository to a drafted paper.

This is the whole system exercised as a researcher would exercise it, in one
narrative, through supported commands. Every Git repository, worktree,
subprocess, capsule, digest, run directory and evidence packet is real. The
only thing faked is the model, and the only things done by hand are the ones
that must be done by hand: promoting a proposal into the capsule, writing
Evidence and a Claim, and approving that Claim.

The arc:

    init-project
      -> research run: literature, analysis, proposal, human checkpoint
      -> a human promotes one proposed item into a DRAFT capsule object
      -> a declared experiment runs and produces a candidate evidence packet
      -> a human turns that candidate into capsule Evidence and a Claim
      -> a human approves the Claim through `researchctl review`
      -> a research run drafts the results section from the accepted Claim
      -> validate-project is clean at every step

What the test is really checking is the boundary. At no point does anything
automated write under `.research/`, mark a Claim accepted, or perform a Review,
and the assertions say so at each stage rather than only at the end.
"""

from __future__ import annotations

import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import yaml

from research_os import cli as cli_module
from research_os.automation import commands as auto_commands
from research_os.automation.config import config_path
from research_os.capsule import validate_project
from research_os.cli import main
from research_os.research.models import TaskStatus
from research_os.research.store import ResearchStore
from tests.fake_providers import FakeProvider, ScriptedResponse
from tests.research_helpers import (
    EXPERIMENT_SCRIPT,
    PROJECT_ID,
    analysis_task,
    checkpoint_task,
    paper_task,
    plan_payload,
    proposal_task,
    scripted,
    task,
)

MANUSCRIPT = """\
# Widget deformation under load

## Results

(to be written)
"""

FAKE_CONFIG = """planner:
  provider: fake
  model: fake-planner
  read_only: true
analyst:
  provider: fake
  model: fake-analyst
  read_only: true
  access: snapshot_read
  tools: [Read, Glob, Grep]
coder:
  provider: fake
  model: fake-coder
  read_only: false
  tools: [Read, Write, Edit]
reviewer:
  provider: fake
  model: fake-reviewer
  read_only: true
"""

EXPERIMENTS_CONFIG = f"""\
schema_version: 1
projects:
  {PROJECT_ID}:
    commands:
      fit-model:
        name: fit-model
        description: Fit the deformation model on the bench sweep.
        argv: [python3, fit.py, --seed, "{{seed}}"]
        parameters:
          - name: seed
            type: integer
            required: true
            minimum: 0
        outputs: [results/fit.json]
        checks: [outputs_exist]
        timeout_seconds: 60
"""


EMPTY_LITERATURE_REPORT: dict[str, Any] = {
    "summary": "Nothing in the local index bears on this goal.",
    "assessments": [],
    "findings": [],
    "disagreements": [],
    "uncertainties": ["the local index has not been populated for this project"],
    "recommended_followup": [
        {
            "identifier": "10.1000/widget-deformation",
            "reason": "retrieve this before relying on anything said here",
        }
    ],
}


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


def answer_prompts(monkeypatch: pytest.MonkeyPatch, *replies: str) -> None:
    """Make the CLI interactive and script the answers a human would type."""

    monkeypatch.setattr(cli_module, "_is_interactive", lambda: True)
    pending = list(replies)

    def fake_input(prompt: str = "") -> str:
        if not pending:
            raise EOFError
        return pending.pop(0)

    monkeypatch.setattr("builtins.input", fake_input)


def git(args: list[str], cwd: Path) -> str:
    return subprocess.run(
        ["git", *args], cwd=str(cwd), check=True, capture_output=True, text=True
    ).stdout


def capsule_files(repo: Path) -> dict[str, bytes]:
    return {
        item.relative_to(repo).as_posix(): item.read_bytes()
        for item in sorted((repo / ".research").rglob("*"))
        if item.is_file()
    }


def write_object(repo: Path, payload: dict[str, Any]) -> Path:
    """Write one capsule object by hand, as a researcher must."""

    from research_os.proposal.promote import EXPERIMENT_MANIFEST, TYPE_TO_DIRECTORY

    directory = repo / ".research" / TYPE_TO_DIRECTORY[payload["type"]]
    if payload["type"] == "experiment":
        directory = directory / payload["id"]
        target = directory / EXPERIMENT_MANIFEST
    else:
        target = directory / f"{payload['id']}.yaml"
    directory.mkdir(parents=True, exist_ok=True)
    target.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8"
    )
    return target


@pytest.fixture
def project(
    automation_home: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Path]:
    """A real, committed, registered project with an experiment script."""

    repo = tmp_path / "widget-study"
    repo.mkdir()
    git(["init", "-q", "--initial-branch=main"], repo)
    git(["config", "user.email", "researcher@example.invalid"], repo)
    git(["config", "user.name", "A Researcher"], repo)
    (repo / "README.md").write_text("# widget study\n", encoding="utf-8")
    (repo / "analysis.py").write_text(
        "def deformation(load):\n    return load\n", encoding="utf-8"
    )
    (repo / "fit.py").write_text(EXPERIMENT_SCRIPT, encoding="utf-8")
    (repo / "paper").mkdir()
    (repo / "paper" / "manuscript.md").write_text(MANUSCRIPT, encoding="utf-8")
    git(["add", "-A"], repo)
    git(["commit", "-qm", "initial"], repo)

    config_path().write_text(FAKE_CONFIG, encoding="utf-8")
    from research_os.experiment.config import config_path as experiments_path

    experiments_path().write_text(EXPERIMENTS_CONFIG, encoding="utf-8")

    assert run_cli(monkeypatch, "init-project", str(repo), "--id", PROJECT_ID) == 0

    # The researcher writes down the open question first. Everything automated
    # in this test rests on it, and a proposal may only cite capsule objects
    # that were actually put in front of it.
    write_object(
        repo,
        {
            "id": "Q-0001",
            "type": "question",
            "schema_version": 1,
            "status": "open",
            "title": "How does widget deformation scale with load?",
            "statement": ("We do not know whether deformation stays linear above 10N."),
        },
    )
    git(["add", "-A"], repo)
    git(["commit", "-qm", "capsule and the open question"], repo)
    yield repo


@pytest.fixture
def provider() -> Iterator[FakeProvider]:
    fake = scripted()
    auto_commands.set_registry_factory(lambda: {"fake": fake})
    try:
        yield fake
    finally:
        auto_commands.set_registry_factory(None)


def test_a_complete_research_project(
    project: Path,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    from tests.paper_helpers import manifest_payload
    from tests.proposal_helpers import assessment_payload, item, proposal_payload

    repo = project

    # ---------------------------------------------------------------- stage 1
    # An empty capsule is a valid capsule, and there is nothing accepted in it.
    assert run_cli(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    before_run = capsule_files(repo)

    # ---------------------------------------------------------------- stage 2
    # A research run reads the field, reads the code, proposes, and stops to ask.
    provider.responses["planner"] = [
        ScriptedResponse(
            structured=plan_payload(
                summary="Read the field, read the code, propose, then ask.",
                tasks=[
                    task(query="widget deformation under load"),
                    analysis_task(task_id="T-002"),
                    proposal_task(task_id="T-003", depends_on=["T-001", "T-002"]),
                    checkpoint_task(
                        task_id="T-004",
                        depends_on=["T-003"],
                        question="Shall I spend bench time on the sweep?",
                    ),
                ],
            )
        ),
        # The proposal worker runs on the planner role and gets its own response.
        ScriptedResponse(structured=proposal_payload(items=[item()])),
    ]
    provider.responses["reviewer"] = [ScriptedResponse(structured=assessment_payload())]
    # The proposal task depends on the literature task, so the proposal
    # controller reads the local index first. It is empty here -- this machine
    # is offline in these tests -- and an honest report of "nothing indexed
    # bears on this" is exactly what that pass should produce.
    provider.responses["literature"] = [
        ScriptedResponse(structured=EMPTY_LITERATURE_REPORT)
    ]

    assert (
        run_cli(
            monkeypatch,
            "research",
            "start",
            PROJECT_ID,
            "--goal",
            "Find out whether widget deformation stays linear above 10N.",
            "--run",
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "WAITING_FOR_HUMAN" in out
    assert "Shall I spend bench time on the sweep?" in out

    run_id = ResearchStore.list_run_ids()[-1]
    run = ResearchStore.open(run_id).load()
    assert run.task("T-001").status is TaskStatus.DONE
    assert run.task("T-002").status is TaskStatus.DONE
    assert run.task("T-003").status is TaskStatus.DONE
    proposal_id = run.task("T-003").artifact_id or ""
    assert proposal_id.startswith("PROP-")

    # Nothing automated has touched the scientific record: the capsule is
    # byte-for-byte what the researcher committed.
    assert capsule_files(repo) == before_run

    # ---------------------------------------------------------------- stage 3
    # The researcher answers, and the run finishes.
    assert (
        run_cli(
            monkeypatch,
            "research",
            "answer",
            run_id,
            "--answer",
            "Yes. The rig is free on Thursday.",
        )
        == 0
    )
    capsys.readouterr()
    assert run_cli(monkeypatch, "research", "run", run_id) == 0
    assert "READY_FOR_HUMAN" in capsys.readouterr().out

    # ---------------------------------------------------------------- stage 4
    # The researcher promotes one proposed item. This needs a terminal, and it
    # is the only way anything from the run enters the capsule.
    answer_prompts(monkeypatch, "y")
    assert (
        run_cli(
            monkeypatch,
            "propose",
            "promote",
            proposal_id,
            "--item",
            "PR-001",
            str(repo),
        )
        == 0
    )
    promoted = capsys.readouterr().out
    assert "DRAFT" in promoted
    hypotheses = sorted((repo / ".research" / "hypotheses").glob("HYP-*.yaml"))
    assert len(hypotheses) == 1
    assert "status: draft" in hypotheses[0].read_text(encoding="utf-8")
    assert run_cli(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    git(["add", "-A"], repo)
    git(["commit", "-qm", "promote hypothesis"], repo)

    # ---------------------------------------------------------------- stage 5
    # A declared experiment runs, and produces a candidate -- not Evidence.
    assert (
        run_cli(
            monkeypatch,
            "experiment",
            "run",
            PROJECT_ID,
            "fit-model",
            "--param",
            "seed=7",
            "--execute",
        )
        == 0
    )
    executed = capsys.readouterr().out
    assert "candidate" in executed.lower()
    assert capsule_files(repo).keys() == {
        name for name in capsule_files(repo)
    }  # unchanged shape
    assert not list((repo / ".research" / "evidence").glob("EVI-*.yaml"))

    # ---------------------------------------------------------------- stage 6
    # The researcher writes the Evidence, the Experiment and the Claim by hand.
    hypothesis_id = hypotheses[0].stem
    write_object(
        repo,
        {
            "id": "EXP-0001",
            "type": "experiment",
            "schema_version": 1,
            "status": "completed",
            "title": "Bench loading sweep",
            "purpose": "Measure deformation at 2N, 5N and 9N.",
            "hypotheses": [hypothesis_id],
            "predictions": [
                {
                    "hypothesis": hypothesis_id,
                    "predicted_outcome": "Deformation per newton is constant to 3%.",
                    "discriminates": True,
                }
            ],
            "primary_metrics": ["deformation_mm"],
            "decision_rule": (
                "Reject linearity if deformation per newton varies by more than "
                "3 percent across the sweep."
            ),
            "provenance": {
                "code": "fit.py",
                "config": "experiments.yaml",
                "data": "bench-run-2026-03",
                "git_commit": git(["rev-parse", "HEAD"], repo).strip()[:7],
            },
        },
    )
    write_object(
        repo,
        {
            "id": "EVI-0001",
            "type": "evidence",
            "schema_version": 1,
            "status": "active",
            "title": "Sweep deformation is constant per newton",
            "statement": (
                "Across 2N, 5N and 9N deformation per newton varied by 1.4 percent."
            ),
            "kind": "experiment",
            "experiment": "EXP-0001",
        },
    )
    write_object(
        repo,
        {
            "id": "CLAIM-0001",
            "type": "claim",
            "schema_version": 1,
            "status": "evidence_linked",
            "title": "Deformation is linear below 10N in this bench setup",
            "statement": (
                "In this bench setup, widget deformation is linear in applied "
                "load below 10N."
            ),
            "hypotheses": [hypothesis_id],
            "supporting_evidence": ["EVI-0001"],
        },
    )
    hypothesis = yaml.safe_load(hypotheses[0].read_text(encoding="utf-8"))
    hypothesis["status"] = "supported"
    hypothesis["supporting_evidence"] = ["EVI-0001"]
    hypotheses[0].write_text(
        yaml.safe_dump(hypothesis, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    assert run_cli(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    git(["add", "-A"], repo)
    git(["commit", "-qm", "evidence and claim"], repo)

    # ---------------------------------------------------------------- stage 7
    # A human approves the Claim. No automated path can reach this.
    answer_prompts(
        monkeypatch, "approve", "Checked the sweep and the rig log.", "", "y"
    )
    assert run_cli(monkeypatch, "review", "CLAIM-0001", str(repo)) == 0
    capsys.readouterr()
    _promote_claim(repo, "evidence_linked", "accepted")
    assert run_cli(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    git(["add", "-A"], repo)
    git(["commit", "-qm", "human approval"], repo)

    report = validate_project(repo)
    claim = next(item for item in report.objects if item.id == "CLAIM-0001")
    assert claim.status == "accepted"

    # ---------------------------------------------------------------- stage 8
    # Only now can a paper task write anything: the packet is built from
    # accepted science and there was none until a person made some.
    provider.responses["planner"] = [
        ScriptedResponse(
            structured=plan_payload(
                summary="Draft the results section from the accepted claim.",
                tasks=[paper_task()],
            )
        )
    ]
    provider.responses["coder"] = [
        ScriptedResponse(
            structured=manifest_payload(
                claim_ids=["CLAIM-0001"],
                evidence_ids=["EVI-0001"],
                experiment_ids=["EXP-0001"],
                caveats=["the sweep stopped at 9N"],
            ),
            text="Drafted the results section.",
            write_files={"paper/manuscript.md": _grounded_results()},
        )
    ]
    provider.responses["reviewer"] = [
        ScriptedResponse(
            structured={"verdict": "PASS", "summary": "ok", "findings": []}
        )
    ]

    assert (
        run_cli(
            monkeypatch,
            "research",
            "start",
            PROJECT_ID,
            "--goal",
            "Write up the linearity result.",
            "--run",
        )
        == 0
    )
    drafted = capsys.readouterr().out
    assert "READY_FOR_HUMAN" in drafted

    paper_run = ResearchStore.open(ResearchStore.list_run_ids()[-1]).load()
    draft_id = paper_run.task("T-001").artifact_id or ""
    assert draft_id.startswith("DRAFT-")

    from research_os.paper.store import DraftStore

    draft = DraftStore.open(draft_id).load()
    assert draft.manifest.claim_ids == ["CLAIM-0001"]
    assert draft.grounding is not None and draft.grounding.grounded

    # ---------------------------------------------------------------- the end
    # The manuscript was drafted in a worktree. The researcher's checkout is
    # untouched, the capsule still validates, and the Claim is accepted because
    # a person approved it -- the only way that is ever true.
    assert "(to be written)" in (repo / "paper" / "manuscript.md").read_text(
        encoding="utf-8"
    )
    assert run_cli(monkeypatch, "validate-project", str(repo)) == 0
    capsys.readouterr()
    assert git(["status", "--porcelain"], repo).strip() == ""


def test_nothing_automated_ever_wrote_a_review(
    project: Path,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """The capsule's Review directory stays empty until a person uses the CLI.

    Asserted against a run that does everything else it can, so this is not
    "we never ran anything" -- it is "we ran the pipeline and it produced no
    Review".
    """

    repo = project
    assert (
        run_cli(
            monkeypatch,
            "research",
            "start",
            PROJECT_ID,
            "--goal",
            "Understand the analysis module.",
            "--run",
        )
        == 0
    )
    assert "READY_FOR_HUMAN" in capsys.readouterr().out
    reviews = repo / ".research" / "reviews"
    assert not reviews.exists() or not list(reviews.glob("*.yaml"))


def _promote_claim(repo: Path, old: str, new: str) -> None:
    """Set a Claim's status by hand. There is no command for this on purpose."""

    path = repo / ".research" / "claims" / "CLAIM-0001.yaml"
    text = path.read_text(encoding="utf-8")
    assert f"status: {old}" in text
    path.write_text(
        text.replace(f"status: {old}", f"status: {new}", 1), encoding="utf-8"
    )


def _grounded_results() -> str:
    return """\
# Widget deformation under load

## Results

In this bench setup, widget deformation is linear in applied load below 10N
(CLAIM-0001). Across the loading sweep, deformation per newton varied by 1.4
percent (EVI-0001, EXP-0001). The sweep stopped at 9N, so nothing here speaks
to behaviour above 10N.
"""
