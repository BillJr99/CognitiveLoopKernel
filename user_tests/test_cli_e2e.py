"""End-to-end CLI tests.

Drive the harness exactly the way a user would: invoke ``clk`` as a
subprocess and assert on the artefacts it produces.  These tests use the
``shell`` provider (always available, no API keys) and assert only on
plumbing — that commands exit cleanly and that the documented state
files appear.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from .conftest import run_clk


# ---------------------------------------------------------------------------
# init
# ---------------------------------------------------------------------------


def test_init_creates_clk_layout(clk_project: Path) -> None:
    res = run_clk("init", "--name", "demo", cwd=clk_project)
    assert res.returncode == 0, res.stderr
    assert "CLK initialized." in res.stdout

    clk = clk_project / ".clk"
    assert (clk / "config" / "clk.config.json").is_file()
    assert (clk / "config" / "providers.json").is_file()
    assert (clk / "config" / "agents.json").is_file()
    assert (clk / "config" / "workflows").is_dir()
    assert (clk / "prompts").is_dir()
    assert (clk / "state").is_dir()
    assert (clk / "logs").is_dir()


def test_init_seeds_baseline_workflows(initialized_project: Path) -> None:
    wf_dir = initialized_project / ".clk" / "config" / "workflows"
    bundled = {p.name for p in wf_dir.iterdir() if p.suffix == ".yaml"}
    for required in ("engineering.yaml", "discovery.yaml", "product.yaml", "ralph_loop.yaml"):
        assert required in bundled, f"missing workflow: {required}"


def test_init_writes_baseline_agent_prompts(initialized_project: Path) -> None:
    prompts = initialized_project / ".clk" / "prompts"
    for required in ("chief.md", "qa.md", "ralph.md"):
        assert (prompts / required).is_file(), f"missing prompt: {required}"


def test_init_is_idempotent(initialized_project: Path) -> None:
    """Re-running clk init should succeed without clobbering existing state."""
    cfg_before = (initialized_project / ".clk" / "config" / "clk.config.json").read_text()
    res = run_clk("init", "--name", "demo", cwd=initialized_project)
    assert res.returncode == 0
    cfg_after = (initialized_project / ".clk" / "config" / "clk.config.json").read_text()
    assert cfg_before == cfg_after


def test_commands_fail_clearly_before_init(clk_project: Path) -> None:
    res = run_clk("status", cwd=clk_project)
    assert res.returncode != 0
    assert "not initialized" in (res.stderr + res.stdout).lower()


# ---------------------------------------------------------------------------
# idea
# ---------------------------------------------------------------------------


def test_idea_writes_state_files(initialized_project: Path) -> None:
    res = run_clk(
        "idea", "A local-first journaling app",
        "--title", "Journal",
        "--no-cast",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr

    state = initialized_project / ".clk" / "state"
    idea_path = state / "idea.json"
    brief_path = state / "system_brief.md"
    assert idea_path.is_file()
    assert brief_path.is_file()

    idea = json.loads(idea_path.read_text())
    assert idea["title"] == "Journal"
    assert idea["statement"] == "A local-first journaling app"
    assert "captured_at" in idea


def test_idea_with_tags(initialized_project: Path) -> None:
    res = run_clk(
        "idea", "Some idea",
        "--tag", "alpha", "--tag", "beta",
        "--no-cast",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    idea = json.loads((initialized_project / ".clk" / "state" / "idea.json").read_text())
    assert "alpha" in idea["tags"]
    assert "beta" in idea["tags"]


def test_idea_auto_casts_with_shell_provider(initialized_project: Path) -> None:
    """Without --no-cast, the harness should run the chief casting pass
    automatically; the shell provider always returns ok so this should
    succeed."""
    res = run_clk("idea", "Test idea", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    assert "casting" in res.stdout.lower() or "chief casting" in res.stdout.lower()


# ---------------------------------------------------------------------------
# cast / roles
# ---------------------------------------------------------------------------


def test_cast_fails_without_idea(initialized_project: Path) -> None:
    res = run_clk("cast", cwd=initialized_project)
    assert res.returncode != 0
    assert "no idea" in (res.stderr + res.stdout).lower()


def test_roles_list_shows_baseline(initialized_project: Path) -> None:
    res = run_clk("roles", "list", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    for baseline in ("chief", "qa", "ralph"):
        assert baseline in res.stdout, f"baseline role {baseline!r} missing from `roles list`"


def test_roles_add_dynamic_role(initialized_project: Path) -> None:
    res = run_clk(
        "roles", "add",
        "--name", "data_steward",
        "--role", "owns the data model and migrations",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    # Verify persisted in agents.json
    agents = json.loads(
        (initialized_project / ".clk" / "config" / "agents.json").read_text()
    )
    assert "data_steward" in agents.get("agents", {})

    listed = run_clk("roles", "list", cwd=initialized_project)
    assert listed.returncode == 0
    assert "data_steward" in listed.stdout


def test_roles_remove_baseline_rejected(initialized_project: Path) -> None:
    """Baseline agents (chief, qa, ralph) must never be removable."""
    res = run_clk("roles", "remove", "--name", "chief", cwd=initialized_project)
    # The harness should refuse — either via non-zero exit or a clear message.
    msg = (res.stdout + res.stderr).lower()
    assert (
        res.returncode != 0
        or "baseline" in msg
        or "cannot" in msg
        or "reserved" in msg
        or "refused" in msg
    ), f"baseline removal was not refused: {res.stdout!r} / {res.stderr!r}"
    agents = json.loads(
        (initialized_project / ".clk" / "config" / "agents.json").read_text()
    )
    assert "chief" in agents.get("agents", {})


def test_roles_remove_dynamic_role(initialized_project: Path) -> None:
    # Pick a name that won't trip the name-similarity guard.
    add = run_clk(
        "roles", "add",
        "--name", "data_curator",
        "--role", "curates training data",
        cwd=initialized_project,
    )
    assert add.returncode == 0, f"add failed: {add.stdout!r} / {add.stderr!r}"
    agents = json.loads(
        (initialized_project / ".clk" / "config" / "agents.json").read_text()
    )
    assert "data_curator" in agents.get("agents", {}), \
        f"role didn't actually get added: {agents}"

    res = run_clk("roles", "remove", "--name", "data_curator", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    agents = json.loads(
        (initialized_project / ".clk" / "config" / "agents.json").read_text()
    )
    assert "data_curator" not in agents.get("agents", {})


# ---------------------------------------------------------------------------
# plan / run / loop (dry-run with shell provider)
# ---------------------------------------------------------------------------


def test_plan_runs_discovery_and_product_dry_run(initialized_project: Path) -> None:
    # Capture an idea first so the chief / researcher have something to chew on.
    run_clk("idea", "Anything", "--no-cast", cwd=initialized_project, check=True)
    res = run_clk("plan", "--dry-run", cwd=initialized_project)
    # Plan can legitimately exit non-zero if a stage validation fails, but the
    # harness must at least *invoke* both workflows.
    out = res.stdout + res.stderr
    assert "discovery" in out.lower() or "Plan" in out
    assert "product" in out.lower() or "Plan" in out


def test_run_engineering_dry_run(initialized_project: Path) -> None:
    run_clk("idea", "Anything", "--no-cast", cwd=initialized_project, check=True)
    res = run_clk("run", "--workflow", "engineering", "--dry-run", cwd=initialized_project)
    assert res.returncode in (0, 1), res.stderr
    assert "engineering" in (res.stdout + res.stderr).lower()


def test_run_unknown_workflow_reports_missing(initialized_project: Path) -> None:
    res = run_clk("run", "--workflow", "does-not-exist", cwd=initialized_project)
    assert res.returncode != 0
    assert "not found" in (res.stdout + res.stderr).lower()


def test_loop_ralph_dry_run(initialized_project: Path) -> None:
    run_clk("idea", "Anything", "--no-cast", cwd=initialized_project, check=True)
    res = run_clk(
        "loop", "--mode", "ralph", "--max-iterations", "1", "--dry-run",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    assert "ralph" in (res.stdout + res.stderr).lower()


def test_loop_autoresearch_dry_run(initialized_project: Path) -> None:
    run_clk("idea", "Anything", "--no-cast", cwd=initialized_project, check=True)
    res = run_clk(
        "loop", "--mode", "autoresearch", "--max-iterations", "1", "--dry-run",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    assert "autoresearch" in (res.stdout + res.stderr).lower()


# ---------------------------------------------------------------------------
# status / providers / configure
# ---------------------------------------------------------------------------


def test_status_reports_initialized_project(initialized_project: Path) -> None:
    res = run_clk("status", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    assert "CLK status" in res.stdout
    assert "default_provider" in res.stdout
    assert "Providers" in res.stdout
    # Every shipped provider should be listed
    for name in ("shell", "claude", "codex", "gemini", "pi", "ollama", "openwebui"):
        assert name in res.stdout, f"provider {name!r} missing from status output"


def test_providers_lists_all_providers(initialized_project: Path) -> None:
    res = run_clk("providers", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert "active" in data
    assert "available" in data
    # shell is always available
    assert data["available"].get("shell") is True


def test_configure_show_emits_json(initialized_project: Path) -> None:
    res = run_clk("configure", "--show", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    cfg = json.loads(res.stdout)
    assert "default_provider" in cfg
    assert "default_workflow" in cfg


def test_configure_set_updates_key(initialized_project: Path) -> None:
    res = run_clk(
        "configure", "--set", "max_iterations=7",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    cfg = json.loads(
        (initialized_project / ".clk" / "config" / "clk.config.json").read_text()
    )
    assert cfg["max_iterations"] == 7


def test_configure_set_coerces_booleans(initialized_project: Path) -> None:
    res = run_clk(
        "configure", "--set", "auto_commit=false",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    cfg = json.loads(
        (initialized_project / ".clk" / "config" / "clk.config.json").read_text()
    )
    assert cfg["auto_commit"] is False


# ---------------------------------------------------------------------------
# Top-level smoke
# ---------------------------------------------------------------------------


def test_help_lists_all_subcommands(clk_project: Path) -> None:
    res = run_clk("--help", cwd=clk_project)
    assert res.returncode == 0
    out = res.stdout
    for sub in (
        "init", "idea", "cast", "roles", "plan",
        "run", "loop", "tui", "status", "providers", "configure",
    ):
        assert sub in out, f"subcommand {sub!r} missing from --help"


def test_version_prints(clk_project: Path) -> None:
    res = run_clk("--version", cwd=clk_project)
    assert res.returncode == 0
    assert res.stdout.strip(), "--version should print something"
