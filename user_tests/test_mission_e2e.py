"""End-to-end tests for the autonomous mission (shell provider, offline).

The shell provider echoes prompts and emits no ACTION blocks, so it is ideal
for verifying the *gates* fire: with no real deliverables / qa pass / ralph
pass, the done-gate must REJECT completion (no ``done_granted.md``), proving
premature done is blocked. We also verify the charter-first ordering, the
living-plan artifacts, the per-cycle telemetry event, and the commit trace.

Everything is capped hard (single phase, one total cycle, refinement off) so
the suite stays fast without any network access.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .conftest import run_clk


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                pass
    return out


def _patch_config(proj: Path, **overrides: Any) -> None:
    cfg_path = proj / ".clk" / "config" / "clk.config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _fast_mission(proj: Path) -> None:
    """Cap the mission to one short phase/cycle with refinement off (speed)."""
    _patch_config(
        proj,
        mission={"default_phases": ["engineering"], "max_total_cycles": 1,
                 "max_iterations_per_phase": 1, "telemetry_stdout": True},
        supervise={"max_cycles": 1},
        robustness={"auto_refine": "off", "auto_consensus": "off",
                    "max_quality_retries": 0, "plateau_action": "off"},
        meta_prompt={"dispatch": "off", "role": "off"},
        deliberation={"enabled": False},
    )


def _git_log(proj: Path) -> List[str]:
    r = subprocess.run(["git", "log", "--oneline"], cwd=str(proj),
                       capture_output=True, text=True)
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


def test_mission_help_lists_subcommand(initialized_project: Path):
    res = run_clk("mission", "--help", cwd=initialized_project)
    assert res.returncode == 0
    assert "objective" in res.stdout.lower()


def test_mission_dry_run_bootstraps_plan(initialized_project: Path):
    proj = initialized_project
    _fast_mission(proj)
    res = run_clk("mission", "build a tiny tool", "--dry-run", cwd=proj, timeout=180)
    assert res.returncode == 0, res.stderr
    assert (proj / ".clk" / "state" / "mission.json").exists()
    assert (proj / ".clk" / "state" / "CHARTER.md").exists()
    assert (proj / ".clk" / "state" / "MISSION.md").exists()
    plan = json.loads((proj / ".clk" / "state" / "mission.json").read_text())
    assert [p["id"] for p in plan["phases"]] == ["engineering"]


def test_mission_blocks_premature_done(initialized_project: Path):
    proj = initialized_project
    _fast_mission(proj)
    res = run_clk("mission", "build a tiny tool", "--max-cycles", "1",
                  cwd=proj, timeout=600)
    # With the shell provider there is no qa PASS / ralph pass / deliverable,
    # so the done-gate must NOT grant completion.
    assert not (proj / ".clk" / "state" / "done_granted.md").exists()
    plan_path = proj / ".clk" / "state" / "mission.json"
    assert plan_path.exists()
    plan = json.loads(plan_path.read_text())
    assert plan["status"] in ("stalled", "running")
    # Telemetry event was emitted for the cycle.
    events = _read_jsonl(proj / ".clk" / "logs" / "activity.jsonl")
    assert any(e.get("event") == "loop_cycle_summary" for e in events)


def test_mission_charter_before_plan_in_trace(initialized_project: Path):
    proj = initialized_project
    _fast_mission(proj)
    run_clk("mission", "build a tiny tool", "--max-cycles", "1", cwd=proj, timeout=600)
    log = _git_log(proj)
    charter_idx = next((i for i, l in enumerate(log) if "[clk:charter]" in l), None)
    plan_idx = next((i for i, l in enumerate(log) if "[clk:plan]" in l), None)
    assert charter_idx is not None, f"no charter commit in: {log}"
    assert plan_idx is not None, f"no plan commit in: {log}"
    # git log is newest-first, so the charter (older) has a HIGHER index.
    assert charter_idx > plan_idx, f"charter should precede plan: {log}"


def test_mission_writes_plan_post_to_blackboard(initialized_project: Path):
    proj = initialized_project
    _fast_mission(proj)
    run_clk("mission", "build a tiny tool", "--max-cycles", "1", cwd=proj, timeout=600)
    bb = proj / ".clk" / "blackboard"
    kinds = []
    for f in bb.glob("*.json"):
        try:
            kinds.append(json.loads(f.read_text()).get("post_type"))
        except Exception:
            pass
    assert "plan" in kinds or "charter" in kinds
