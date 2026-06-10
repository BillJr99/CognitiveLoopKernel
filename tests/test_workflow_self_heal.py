"""Self-healing knobs on WorkflowRunner: stall rescue, outputs recovery."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List

import pytest

from clk_harness.config import DEFAULT_CLK_CONFIG, Paths
from clk_harness.orchestration.workflow import WorkflowRunner


class _StubRunner:
    """Minimal stand-in for AgentRunner: just enough for the config
    properties and to record chief dispatches."""

    def __init__(self, clk_cfg: Dict[str, Any]) -> None:
        self.clk_cfg = clk_cfg
        self.dispatches: List[Dict[str, Any]] = []
        self.observer = None

    def run(self, agent_name: str, objective: str, *, extra=None, dry_run=None):
        self.dispatches.append(
            {"agent": agent_name, "objective": objective, "extra": dict(extra or {})}
        )

        class _Resp:
            ok = True
            text = "CHECKPOINT: continue"
            error = None

        class _Run:
            agent = agent_name
            response = _Resp()
            files_written: List[str] = []
            committed = False

        return _Run()


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def _make(paths: Paths, clk_cfg: Dict[str, Any]) -> WorkflowRunner:
    return WorkflowRunner(paths, _StubRunner(clk_cfg))  # type: ignore[arg-type]


def test_stall_rescue_enabled_by_default(paths: Paths) -> None:
    wr = _make(paths, {})
    assert wr.stall_rescue_enabled is True


def test_stall_rescue_disabled_via_config(paths: Paths) -> None:
    wr = _make(paths, {"supervise": {"stall_rescue": False}})
    assert wr.stall_rescue_enabled is False
    wr = _make(paths, {"supervise": {"stall_rescue": "off"}})
    assert wr.stall_rescue_enabled is False


def test_outputs_recovery_enabled_by_default(paths: Paths) -> None:
    wr = _make(paths, {})
    assert wr._outputs_recovery_enabled is True


def test_outputs_recovery_disabled_via_config(paths: Paths) -> None:
    wr = _make(paths, {"recovery": {"dispatch_on_unmet_outputs": False}})
    assert wr._outputs_recovery_enabled is False


def test_default_config_carries_new_keys() -> None:
    assert DEFAULT_CLK_CONFIG["supervise"]["stall_rescue"] is True
    assert DEFAULT_CLK_CONFIG["recovery"]["dispatch_on_unmet_outputs"] is True
    assert DEFAULT_CLK_CONFIG["recovery"]["max_per_stage"] == 3
    assert DEFAULT_CLK_CONFIG["validation"]["rollback_on_failure"] == "careful"


def test_rollback_policy_careful_default(paths: Paths) -> None:
    from clk_harness.orchestration.workflow import WorkflowStage

    wr = _make(paths, {})
    plain = WorkflowStage(id="s", agent="engineer", objective="x")
    careful = WorkflowStage(id="s", agent="engineer", objective="x", careful=True)
    # Default policy: only careful stages hard-rollback on failed validation;
    # ordinary stages keep their work so batch commits survive on disk.
    assert wr._should_rollback(plain) is False
    assert wr._should_rollback(careful) is True


def test_rollback_policy_always_and_never(paths: Paths) -> None:
    from clk_harness.orchestration.workflow import WorkflowStage

    plain = WorkflowStage(id="s", agent="engineer", objective="x")
    careful = WorkflowStage(id="s", agent="engineer", objective="x", careful=True)
    always = _make(paths, {"validation": {"rollback_on_failure": "always"}})
    assert always._should_rollback(plain) is True
    never = _make(paths, {"validation": {"rollback_on_failure": "never"}})
    assert never._should_rollback(careful) is False


def test_stall_rescue_dispatches_chief_in_recovery_phase(paths: Paths) -> None:
    from clk_harness.orchestration.workflow import Workflow

    wr = _make(paths, {})
    wf = Workflow(name="demo", description="", stages=[])
    wr._dispatch_stall_rescue(wf, cycle=6, cycle_results=[])
    stub = wr.runner  # type: ignore[assignment]
    assert len(stub.dispatches) == 1
    d = stub.dispatches[0]
    assert d["agent"] == "chief"
    assert d["extra"]["phase"] == "recovery"
    assert "STALL RESCUE" in d["objective"]


def test_outputs_recovery_dispatches_chief(paths: Paths) -> None:
    from clk_harness.orchestration.workflow import Workflow, WorkflowStage

    wr = _make(paths, {})
    wf = Workflow(name="demo", description="", stages=[])
    stage = WorkflowStage(
        id="research", agent="researcher", objective="investigate",
        outputs=["brief", "facts"],
    )
    wr._dispatch_outputs_recovery(wf, stage, ["brief"], "", None)
    stub = wr.runner  # type: ignore[assignment]
    assert len(stub.dispatches) == 1
    d = stub.dispatches[0]
    assert d["agent"] == "chief"
    assert d["extra"]["phase"] == "recovery"
    assert "brief" in d["objective"]
