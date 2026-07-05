"""WorkflowStage parsing for new fields and runner helpers."""

from __future__ import annotations

from pathlib import Path
from typing import Dict

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration.workflow import (
    WorkflowStage,
    _round_status,
    load_workflow,
)


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def test_workflow_stage_default_field_values() -> None:
    s = WorkflowStage(id="x", agent="engineer", objective="implement")
    assert s.inputs == []
    assert s.outputs == []
    assert s.phase == ""
    assert s.rounds == 1
    assert s.careful is False


def test_load_workflow_parses_new_fields(tmp_path: Path) -> None:
    yaml_text = """
name: demo
description: demo workflow
stages:
  - id: research_a
    agent: researcher
    objective: investigate X
    outputs: [brief, facts]
  - id: research_b
    agent: researcher
    objective: investigate Y
    inputs: [stage:research_a]
    outputs: [brief]
  - id: review
    agent: chief
    objective: review the research
    depends_on: [research_a, research_b]
    phase: review
  - id: implement
    agent: engineer
    objective: implement based on the review
    depends_on: [review]
    careful: true
    rounds: 3
"""
    wf_path = tmp_path / "demo.yaml"
    wf_path.write_text(yaml_text, encoding="utf-8")
    wf = load_workflow(wf_path)
    by_id: Dict[str, WorkflowStage] = {s.id: s for s in wf.stages}
    assert by_id["research_a"].outputs == ["brief", "facts"]
    assert by_id["research_b"].inputs == ["stage:research_a"]
    assert by_id["review"].phase == "review"
    assert by_id["review"].depends_on == ["research_a", "research_b"]
    assert by_id["implement"].careful is True
    assert by_id["implement"].rounds == 3


def test_load_workflow_clamps_invalid_rounds(tmp_path: Path) -> None:
    yaml_text = """
name: t
stages:
  - id: s
    agent: engineer
    objective: x
    rounds: not-a-number
"""
    wf_path = tmp_path / "t.yaml"
    wf_path.write_text(yaml_text, encoding="utf-8")
    wf = load_workflow(wf_path)
    assert wf.stages[0].rounds == 1


def test_round_status_default_done_when_no_marker() -> None:
    assert _round_status("blah blah no marker") == "done"


def test_round_status_continue_when_marker_present() -> None:
    assert _round_status("body\n\nROUND_STATUS: continue\n") == "continue"


def test_round_status_done_when_explicit_done() -> None:
    assert _round_status("ROUND_STATUS: done\n") == "done"


def test_round_status_takes_last_marker() -> None:
    text = "ROUND_STATUS: continue\nlater changed:\nROUND_STATUS: done\n"
    assert _round_status(text) == "done"
