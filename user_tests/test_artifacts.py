"""Tests that the documented filesystem artefacts actually appear.

The README promises that ``.clk/`` is the single source of truth for
harness state.  These tests assert that the right files appear in the
right places after the corresponding user actions.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_clk


def test_init_writes_default_configs(initialized_project: Path) -> None:
    clk = initialized_project / ".clk"
    cfg = json.loads((clk / "config" / "clk.config.json").read_text())
    # These are the documented top-level keys
    for key in (
        "version", "project_name", "default_provider", "default_workflow",
        "max_iterations", "auto_commit", "provider_retry",
        "supervise", "validation", "casting",
    ):
        assert key in cfg, f"clk.config.json missing key: {key}"

    providers = json.loads((clk / "config" / "providers.json").read_text())
    # Every shipped provider registered
    for p in ("shell", "claude", "codex", "gemini", "pi", "ollama", "openwebui"):
        assert p in providers["providers"], f"provider {p!r} not registered"
    assert providers["active"] == "shell"


def test_init_creates_progress_and_decisions(initialized_project: Path) -> None:
    state = initialized_project / ".clk" / "state"
    assert (state / "progress.md").is_file()
    assert (state / "decisions.md").is_file()
    assert "harness initialized" in (state / "progress.md").read_text()


def test_init_commits_scaffold_to_git(initialized_project: Path) -> None:
    import subprocess
    res = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(initialized_project),
        capture_output=True, text=True,
    )
    assert res.returncode == 0
    log = res.stdout
    # The init pass should leave at least one [clk-init] commit
    assert "clk-init" in log, f"init did not commit; git log:\n{log}"


def test_idea_commits_to_git(initialized_project: Path) -> None:
    import subprocess
    res = run_clk(
        "idea", "Some idea", "--title", "Title", "--no-cast",
        cwd=initialized_project,
    )
    assert res.returncode == 0, res.stderr
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(initialized_project),
        capture_output=True, text=True,
    ).stdout
    assert "clk-idea" in log, f"idea did not commit; git log:\n{log}"


def test_run_writes_run_logs(initialized_project: Path) -> None:
    run_clk("idea", "Anything", "--no-cast", cwd=initialized_project, check=True)
    res = run_clk("run", "--workflow", "engineering", cwd=initialized_project)
    # Engineering with shell provider always at least runs the stages.
    assert res.returncode in (0, 1)
    runs_dir = initialized_project / ".clk" / "runs"
    # The runner records every dispatch under .clk/runs/<run_id>/
    assert runs_dir.is_dir()
    children = list(runs_dir.iterdir())
    assert children, f".clk/runs/ is empty after `clk run`"


def test_logs_directory_populated(initialized_project: Path) -> None:
    """Every command should drop a *.log into .clk/logs/."""
    run_clk("idea", "X", "--no-cast", cwd=initialized_project, check=True)
    logs = initialized_project / ".clk" / "logs"
    assert logs.is_dir()
    log_files = list(logs.glob("*.log"))
    assert log_files, f".clk/logs/ has no .log files: {list(logs.iterdir())}"


def test_gitignore_excludes_harness_state(initialized_project: Path) -> None:
    gi = (initialized_project / ".gitignore").read_text()
    # Either CLK adds its detailed block, or the whole .clk/ is already excluded.
    assert ".clk" in gi, f"missing .clk in .gitignore: {gi!r}"


def test_shell_provider_writes_stub(initialized_project: Path) -> None:
    """The shell provider promises to drop a per-agent stub under
    .clk/runs/shell-stubs/.  Run the engineering workflow and check."""
    run_clk("idea", "X", "--no-cast", cwd=initialized_project, check=True)
    run_clk("run", "--workflow", "engineering", cwd=initialized_project)
    stubs_dir = initialized_project / ".clk" / "runs" / "shell-stubs"
    assert stubs_dir.is_dir(), (
        f"shell-stubs/ missing — .clk/runs contents: "
        f"{[p.name for p in (initialized_project / '.clk' / 'runs').iterdir()]}"
    )
    stubs = list(stubs_dir.glob("*.md"))
    assert stubs, f"shell-stubs/ is empty"
