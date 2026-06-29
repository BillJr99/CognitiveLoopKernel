"""Unit tests for the mission living-plan, charter, and derived validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration.mission import (
    MissionPlan, PhaseSpec, load_plan, save_plan,
)
from clk_harness.orchestration.charter import Charter, load_charter, save_charter
from clk_harness.orchestration.evaluator import Evaluator, EvalResult, derive_validation


def _paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def test_plan_roundtrip(tmp_path):
    paths = _paths(tmp_path)
    plan = MissionPlan(
        objective="build x",
        title="build x",
        phases=[
            PhaseSpec(id="discovery", engine="workflow", order=0,
                      exit_criteria=["brief written"]),
            PhaseSpec(id="engineering", engine="workflow", order=1),
        ],
    )
    save_plan(paths, plan)
    assert (paths.state / "mission.json").exists()
    assert (paths.state / "MISSION.md").exists()
    loaded = load_plan(paths)
    assert loaded is not None
    assert [p.id for p in loaded.phases] == ["discovery", "engineering"]
    assert loaded.phases[0].exit_criteria == ["brief written"]


def test_next_pending_and_all_done():
    plan = MissionPlan(phases=[
        PhaseSpec(id="a", order=0, status="done"),
        PhaseSpec(id="b", order=1, status="pending"),
        PhaseSpec(id="c", order=2, status="pending"),
    ])
    assert plan.next_pending().id == "b"
    assert not plan.all_done()
    for p in plan.phases:
        p.status = "done"
    assert plan.next_pending() is None
    assert plan.all_done()


def test_plan_from_dict_defaults():
    raw = {"objective": "o", "phases": [{"id": "x"}]}
    plan = MissionPlan.from_dict(raw)
    assert plan.phases[0].engine == "workflow"
    assert plan.phases[0].workflow == "x"  # defaults to id


def test_charter_roundtrip(tmp_path):
    paths = _paths(tmp_path)
    charter = Charter(
        objective="o",
        mission_statement="ship it",
        scope=["a", "b"],
        success_criteria=["tests pass", "README.md exists"],
    )
    save_charter(paths, charter)
    assert (paths.state / "charter.json").exists()
    assert (paths.state / "CHARTER.md").exists()
    loaded = load_charter(paths)
    assert loaded is not None
    assert loaded.mission_statement == "ship it"
    assert loaded.success_criteria == ["tests pass", "README.md exists"]


def test_evaluator_empty_checks_not_vacuous_pass(tmp_path):
    # No checks + auto_derive off -> must NOT silently pass.
    ev = Evaluator(root=tmp_path, default_checks=[], auto_derive=False)
    result = ev.run()
    assert result.ok is False
    assert result.weak is True


def test_evaluator_runs_real_check(tmp_path):
    ev = Evaluator(root=tmp_path, default_checks=["true"])
    assert ev.run().ok is True
    ev2 = Evaluator(root=tmp_path, default_checks=["false"])
    assert ev2.run().ok is False


def test_derive_validation_pytest(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_smoke.py").write_text("def test_x():\n    assert True\n")
    cmds, weak = derive_validation(tmp_path)
    assert any("pytest" in c for c in cmds)
    assert weak is False


def test_derive_validation_npm(tmp_path):
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"test": "jest"}}), encoding="utf-8"
    )
    cmds, weak = derive_validation(tmp_path)
    assert any("npm test" in c for c in cmds)
    assert weak is False


def test_derive_validation_weak_fallback(tmp_path):
    # Bare dir with no tests / package.json -> weak gate.
    cmds, weak = derive_validation(tmp_path)
    assert cmds  # always returns something runnable
    assert weak is True


def test_evaluator_auto_derive_weak_when_no_tests(tmp_path):
    ev = Evaluator(root=tmp_path, default_checks=[], auto_derive=True)
    result = ev.run()
    # weak smoke (test -d .) passes but is flagged weak.
    assert result.weak is True
