"""Unit tests for the adversarial debate-panel refinement (offline)."""

from __future__ import annotations

from pathlib import Path

from clk_harness.config import Paths
from clk_harness.orchestration.workflow import WorkflowRunner, WorkflowStage


class _StubRunner:
    """Minimal stand-in for AgentRunner — debate gating only reads these."""

    def __init__(self, clk_cfg, agents_cfg=None):
        self.clk_cfg = clk_cfg
        self.agents_cfg = agents_cfg or {"agents": {"critic": {}}}


def _runner(tmp_path: Path, debate="careful_only", lenses=None) -> WorkflowRunner:
    robustness = {"debate": debate}
    if lenses is not None:
        robustness["debate_lenses"] = lenses
    p = Paths(root=tmp_path)
    return WorkflowRunner(p, _StubRunner({"robustness": robustness}))


def _stage(agent="engineer", careful=False, refine=None) -> WorkflowStage:
    return WorkflowStage(id="implement", agent=agent, objective="do it",
                         careful=careful, refine=refine)


def test_debate_careful_only_default(tmp_path):
    wr = _runner(tmp_path, debate="careful_only")
    assert wr._debate_enabled(_stage(careful=True))
    assert not wr._debate_enabled(_stage(careful=False))


def test_debate_all(tmp_path):
    wr = _runner(tmp_path, debate="all")
    assert wr._debate_enabled(_stage(agent="engineer"))
    # prose/verdict agents never debate
    assert not wr._debate_enabled(_stage(agent="chief"))
    assert not wr._debate_enabled(_stage(agent="qa"))
    assert not wr._debate_enabled(_stage(agent="critic"))


def test_debate_off(tmp_path):
    wr = _runner(tmp_path, debate="off")
    assert not wr._debate_enabled(_stage(careful=True))


def test_debate_explicit_mode_overrides_global_off(tmp_path):
    wr = _runner(tmp_path, debate="off")
    assert wr._debate_enabled(_stage(refine={"mode": "debate"}))


def test_debate_lenses_default(tmp_path):
    wr = _runner(tmp_path)
    assert wr._debate_lenses(_stage()) == ["correctness", "security", "simplicity"]


def test_debate_lenses_config_override(tmp_path):
    wr = _runner(tmp_path, lenses=["correctness", "performance"])
    assert wr._debate_lenses(_stage()) == ["correctness", "performance"]


def test_debate_lenses_stage_override(tmp_path):
    wr = _runner(tmp_path)
    stage = _stage(refine={"mode": "debate", "critics": ["Security", "tests"]})
    assert wr._debate_lenses(stage) == ["security", "tests"]


def test_lens_guidance_table_has_core_lenses():
    for lens in ("correctness", "security", "simplicity", "performance"):
        assert lens in WorkflowRunner._DEBATE_LENS_GUIDANCE
