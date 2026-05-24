"""Broad coverage of every `clk` subcommand.

These tests spin up a fresh shell-provider workspace and run each
subcommand end-to-end via ``python -m clk_harness.cli``. The shell
provider returns canned echo responses so we exercise the real
orchestration without any external API.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run_clk(*args, cwd: Path, stdin: str | None = None, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["CLK_DISABLE_API"] = "1"
    env["CLK_PROVIDER"] = "shell"
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        [sys.executable, "-m", "clk_harness.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    proc = _run_clk("init", "--name", "subcmd-test", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    return tmp_path


# ---------------------------------------------------------------------------
# version / help
# ---------------------------------------------------------------------------


def test_clk_version_exits_zero(workspace: Path) -> None:
    proc = _run_clk("--version", cwd=workspace)
    assert proc.returncode == 0
    # Should print a version string of the form X.Y.Z
    assert "." in proc.stdout.strip()


def test_clk_help_lists_every_subcommand(workspace: Path) -> None:
    proc = subprocess.run(
        [sys.executable, "-m", "clk_harness.cli", "--help"],
        capture_output=True, text=True, check=False,
        cwd=str(workspace),
        env={**os.environ, "PYTHONPATH": str(ROOT), "CLK_DISABLE_API": "1"},
    )
    assert proc.returncode == 0
    for cmd in ("init", "idea", "cast", "roles", "plan", "run", "loop", "tui",
                "status", "providers", "configure", "doctor", "diag"):
        assert cmd in proc.stdout, f"missing subcommand in help: {cmd}"


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_clk_init_creates_clk_layout(tmp_path: Path) -> None:
    proc = _run_clk("init", "--name", "fresh", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    assert (tmp_path / ".clk" / "config" / "clk.config.json").exists()
    assert (tmp_path / ".clk" / "config" / "providers.json").exists()
    assert (tmp_path / ".clk" / "config" / "agents.json").exists()
    assert (tmp_path / ".clk" / "state").exists()
    assert (tmp_path / ".clk" / "prompts").exists()


def test_clk_init_is_idempotent(workspace: Path) -> None:
    # Re-running init should not error or clobber existing state.
    proc = _run_clk("init", cwd=workspace)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# idea
# ---------------------------------------------------------------------------


def test_clk_idea_captures_statement_and_title(workspace: Path) -> None:
    proc = _run_clk("idea", "Build a todo app", "--title", "Todo", "--no-cast", cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    idea_path = workspace / ".clk" / "state" / "idea.json"
    assert idea_path.exists()
    data = json.loads(idea_path.read_text(encoding="utf-8"))
    assert data["title"] == "Todo"
    assert "todo" in data["statement"].lower()


def test_clk_idea_with_tags(workspace: Path) -> None:
    proc = _run_clk(
        "idea", "An app",
        "--title", "X",
        "--tag", "alpha",
        "--tag", "beta",
        "--no-cast",
        cwd=workspace,
    )
    assert proc.returncode == 0
    data = json.loads((workspace / ".clk" / "state" / "idea.json").read_text(encoding="utf-8"))
    assert "alpha" in data.get("tags", [])
    assert "beta" in data.get("tags", [])


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_clk_status_shows_project_info(workspace: Path) -> None:
    proc = _run_clk("status", cwd=workspace)
    assert proc.returncode == 0, proc.stderr
    assert "project_name" in proc.stdout
    assert "Providers" in proc.stdout
    assert "shell" in proc.stdout


# ---------------------------------------------------------------------------
# providers
# ---------------------------------------------------------------------------


def test_clk_providers_emits_valid_json(workspace: Path) -> None:
    proc = _run_clk("providers", cwd=workspace)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "active" in data
    assert "available" in data
    assert data["available"].get("shell") is True


# ---------------------------------------------------------------------------
# configure
# ---------------------------------------------------------------------------


def test_clk_configure_show_prints_full_config(workspace: Path) -> None:
    proc = _run_clk("configure", "--show", cwd=workspace)
    assert proc.returncode == 0
    data = json.loads(proc.stdout)
    assert "project_name" in data
    assert "default_provider" in data


def test_clk_configure_set_persists_string(workspace: Path) -> None:
    proc = _run_clk("configure", "--set", "default_workflow=engineering", cwd=workspace)
    assert proc.returncode == 0
    cfg = json.loads((workspace / ".clk" / "config" / "clk.config.json").read_text(encoding="utf-8"))
    assert cfg["default_workflow"] == "engineering"


def test_clk_configure_set_parses_bool_and_int(workspace: Path) -> None:
    proc = _run_clk(
        "configure",
        "--set", "auto_commit=false",
        "--set", "max_iterations=42",
        cwd=workspace,
    )
    assert proc.returncode == 0
    cfg = json.loads((workspace / ".clk" / "config" / "clk.config.json").read_text(encoding="utf-8"))
    assert cfg["auto_commit"] is False
    assert cfg["max_iterations"] == 42


# ---------------------------------------------------------------------------
# roles
# ---------------------------------------------------------------------------


def test_clk_roles_list_includes_baseline(workspace: Path) -> None:
    proc = _run_clk("roles", "list", cwd=workspace)
    assert proc.returncode == 0
    # The default roster has chief, qa, ralph.
    for n in ("chief", "qa", "ralph"):
        assert n in proc.stdout


def test_clk_roles_add_and_remove_dynamic_role(workspace: Path) -> None:
    proc = _run_clk(
        "roles", "add",
        "--name", "researcher",
        "--role", "explore open questions",
        cwd=workspace,
    )
    assert proc.returncode == 0
    proc = _run_clk("roles", "list", cwd=workspace)
    assert "researcher" in proc.stdout

    proc = _run_clk("roles", "remove", "--name", "researcher", cwd=workspace)
    assert proc.returncode == 0
    proc = _run_clk("roles", "list", cwd=workspace)
    assert "researcher" not in proc.stdout


# ---------------------------------------------------------------------------
# doctor / diag (already covered in test_doctor_cli.py; small confidence test here)
# ---------------------------------------------------------------------------


def test_clk_doctor_runs_on_fresh_init(workspace: Path) -> None:
    proc = _run_clk("doctor", cwd=workspace)
    assert proc.returncode == 0


# ---------------------------------------------------------------------------
# plan / run / loop (dry-run mode so we don't spin up real agents)
# ---------------------------------------------------------------------------


def test_clk_plan_dry_run(workspace: Path) -> None:
    # Capture an idea first so plan has something to work with.
    _run_clk("idea", "An idea", "--title", "I", "--no-cast", cwd=workspace)
    proc = _run_clk("plan", "--dry-run", cwd=workspace)
    # Dry-run should not error on a fresh shell-provider workspace.
    assert proc.returncode == 0, proc.stderr


def test_clk_run_dry_run(workspace: Path) -> None:
    _run_clk("idea", "An idea", "--title", "I", "--no-cast", cwd=workspace)
    proc = _run_clk("run", "--dry-run", cwd=workspace)
    assert proc.returncode == 0, proc.stderr


def test_clk_loop_dry_run(workspace: Path) -> None:
    _run_clk("idea", "An idea", "--title", "I", "--no-cast", cwd=workspace)
    proc = _run_clk("loop", "--mode", "ralph", "--max-iterations", "1", "--dry-run", cwd=workspace)
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# unknown subcommand
# ---------------------------------------------------------------------------


def test_clk_unknown_subcommand_exits_nonzero(workspace: Path) -> None:
    proc = _run_clk("not-a-real-command", cwd=workspace)
    assert proc.returncode != 0
