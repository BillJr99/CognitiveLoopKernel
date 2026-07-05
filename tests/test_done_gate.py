"""Unit tests for the machine-checkable done-gate (offline, no provider)."""

from __future__ import annotations

import json
from pathlib import Path

from clk_harness.config import Paths
from clk_harness.orchestration import blackboard as bb
from clk_harness.orchestration.charter import Charter, derive_done_criteria
from clk_harness.orchestration.done_gate import deliverable_files, evaluate_done_gate
from clk_harness.orchestration.evaluator import CheckResult, EvalResult


def _paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def _full_cfg(**overrides):
    gate = {
        "enabled": True,
        "require_tests_green": True,
        "require_deliverables": True,
        "min_deliverable_files": 1,
        "require_qa_pass": True,
        "require_ralph_pass": True,
        "forbid_todo_markers": False,
        "max_finish_attempts": 5,
    }
    gate.update(overrides)
    return {"done_gate": gate}


def _satisfy_all(paths: Paths) -> EvalResult:
    """Make every default criterion pass and return a green eval result."""
    (paths.root / "app.py").write_text("print('hi')\n", encoding="utf-8")
    bb.post(paths, author="qa", body="PASS — all checks green", post_type="qa")
    (paths.state / "experiments.jsonl").write_text(
        json.dumps({"index": 1, "improved": True}) + "\n", encoding="utf-8"
    )
    return EvalResult(ok=True, checks=[CheckResult(command="pytest", ok=True, rc=0, output="")])


def test_all_criteria_satisfied_passes(tmp_path):
    paths = _paths(tmp_path)
    eval_result = _satisfy_all(paths)
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result)
    assert verdict.passed, verdict.failures
    assert verdict.failures == []


def test_tests_red_fails(tmp_path):
    paths = _paths(tmp_path)
    _satisfy_all(paths)
    red = EvalResult(ok=False, checks=[CheckResult(command="pytest", ok=False, rc=1, output="boom")])
    verdict = evaluate_done_gate(paths, _full_cfg(), red)
    assert not verdict.passed
    assert "tests_red" in verdict.failures


def test_weak_eval_relaxes_tests_green(tmp_path):
    paths = _paths(tmp_path)
    _satisfy_all(paths)
    weak = EvalResult(ok=False, checks=[], weak=True)
    verdict = evaluate_done_gate(paths, _full_cfg(), weak)
    # Adaptive: a weak gate (no real test command) must not block on tests.
    assert "tests_red" not in verdict.failures
    assert verdict.passed, verdict.failures


def test_missing_qa_pass_fails(tmp_path):
    paths = _paths(tmp_path)
    (paths.root / "app.py").write_text("x=1\n", encoding="utf-8")
    (paths.state / "experiments.jsonl").write_text("{}\n", encoding="utf-8")
    eval_result = EvalResult(ok=True, checks=[CheckResult("pytest", True, 0, "")])
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result)
    assert not verdict.passed
    assert "no_qa_pass" in verdict.failures


def test_qa_fail_body_does_not_count_as_pass(tmp_path):
    paths = _paths(tmp_path)
    _satisfy_all(paths)
    bb.post(paths, author="qa", body="FAIL — 2 tests broken", post_type="qa")
    eval_result = EvalResult(ok=True, checks=[CheckResult("pytest", True, 0, "")])
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result)
    assert "no_qa_pass" in verdict.failures


def test_missing_ralph_pass_fails(tmp_path):
    paths = _paths(tmp_path)
    (paths.root / "app.py").write_text("x=1\n", encoding="utf-8")
    bb.post(paths, author="qa", body="PASS", post_type="qa")
    eval_result = EvalResult(ok=True, checks=[CheckResult("pytest", True, 0, "")])
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result)
    assert "no_ralph_pass" in verdict.failures


def test_no_deliverables_fails(tmp_path):
    paths = _paths(tmp_path)
    bb.post(paths, author="qa", body="PASS", post_type="qa")
    (paths.state / "experiments.jsonl").write_text("{}\n", encoding="utf-8")
    eval_result = EvalResult(ok=True, checks=[CheckResult("pytest", True, 0, "")])
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result)
    assert "no_deliverables" in verdict.failures


def test_each_require_flag_disables_its_check(tmp_path):
    paths = _paths(tmp_path)
    # Nothing satisfied; turn every requirement off -> should pass.
    eval_result = EvalResult(ok=False, checks=[], weak=True)
    cfg = _full_cfg(
        require_tests_green=False,
        require_deliverables=False,
        require_qa_pass=False,
        require_ralph_pass=False,
    )
    verdict = evaluate_done_gate(paths, cfg, eval_result)
    assert verdict.passed, verdict.failures


def test_gate_disabled_always_passes(tmp_path):
    paths = _paths(tmp_path)
    verdict = evaluate_done_gate(paths, {"done_gate": {"enabled": False}}, None)
    assert verdict.passed


def test_todo_markers_when_enabled(tmp_path):
    paths = _paths(tmp_path)
    eval_result = _satisfy_all(paths)
    (paths.root / "app.py").write_text("# TODO: finish this\nx=1\n", encoding="utf-8")
    verdict = evaluate_done_gate(paths, _full_cfg(forbid_todo_markers=True), eval_result)
    assert "todo_markers" in verdict.failures


def test_charter_file_criterion(tmp_path):
    paths = _paths(tmp_path)
    eval_result = _satisfy_all(paths)
    charter = Charter(success_criteria=["report.md documents the findings"])
    criteria = derive_done_criteria(charter)
    assert criteria and criteria[0]["type"] == "file" and criteria[0]["value"] == "report.md"
    # Without report.md present, the charter criterion fails the gate.
    verdict = evaluate_done_gate(paths, _full_cfg(), eval_result, extra_criteria=criteria)
    assert any(f.startswith("charter:") for f in verdict.failures)
    # Create it -> passes.
    (paths.root / "report.md").write_text("findings\n", encoding="utf-8")
    verdict2 = evaluate_done_gate(paths, _full_cfg(), eval_result, extra_criteria=criteria)
    assert verdict2.passed, verdict2.failures


def test_deliverable_files_excludes_state(tmp_path):
    paths = _paths(tmp_path)
    (paths.root / "real.py").write_text("x=1\n", encoding="utf-8")
    (paths.root / "PROGRESS.md").write_text("notes\n", encoding="utf-8")
    files = deliverable_files(paths.root)
    assert "real.py" in files
    assert "PROGRESS.md" not in files
    assert not any(f.startswith(".clk") for f in files)
