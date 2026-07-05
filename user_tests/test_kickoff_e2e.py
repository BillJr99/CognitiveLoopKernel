"""End-to-end tests for ``kickoff.sh``.

kickoff.sh is the primary user entry point — these tests verify that the
non-interactive (``CLK_NO_TUI=true``) pipeline produces the documented
artefacts when driven with the shell provider.

The interactive TUI path is intentionally not exercised here (it requires
a real terminal); see the README and ``clk_harness/tui.py`` for that
flow.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path

import pytest


def _run_kickoff(
    sandbox: Path,
    *args: str,
    extra_env: dict | None = None,
    timeout: float = 240.0,
) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["CLK_DISABLE_API"] = "1"          # don't start the background server
    env["CLK_NO_TUI"] = "true"            # non-interactive pipeline
    # Use the shell provider for deterministic CI runs.  The orchestrator
    # script (scripts/run_all_tests.sh) may set CLK_PROVIDER to a real
    # backend it collected from the user — kickoff_real_provider below is
    # the test that exercises that path.
    env["CLK_PROVIDER"] = "shell"
    env["CLK_PROJECT_NAME"] = "ut-kickoff"
    env["CLK_MAX_ITERATIONS"] = "1"
    # Provide a stable git identity so commits succeed in containers without
    # global git config.
    env.setdefault("CLK_GIT_NAME", "CLK Test")
    env.setdefault("CLK_GIT_EMAIL", "test@clk.invalid")
    if extra_env:
        env.update(extra_env)
    return subprocess.run(
        ["bash", str(sandbox / "kickoff.sh"), *args],
        cwd=str(sandbox),
        env=env,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def test_kickoff_help_prints_usage(kickoff_sandbox: Path) -> None:
    res = _run_kickoff(kickoff_sandbox, "--help")
    assert res.returncode == 0
    assert "usage" in res.stdout.lower() or "Usage" in res.stdout
    assert "--setup" in res.stdout
    assert "--provider" in res.stdout
    assert "--no-tui" in res.stdout


def test_kickoff_non_interactive_pipeline(kickoff_sandbox: Path) -> None:
    """The full init → idea → plan → run → loop pipeline must produce a
    self-contained kickoff directory under workspace/."""
    res = _run_kickoff(
        kickoff_sandbox,
        "A local-first journaling app",
        timeout=300,
    )
    # kickoff.sh tolerates per-stage failures, but must complete the script.
    assert res.returncode == 0, (
        f"kickoff exited {res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert "[kickoff] complete" in res.stdout

    # Locate the kickoff directory it created
    workspace = kickoff_sandbox / "workspace"
    assert workspace.is_dir(), "kickoff.sh did not create workspace/"
    kickoffs = sorted(workspace.glob("kickoff-*"))
    assert kickoffs, f"no kickoff-* dir under {workspace}"
    kdir = kickoffs[-1]

    # Manifest and harness layout
    assert (kdir / "KICKOFF.md").is_file()
    assert (kdir / ".clk" / "harness" / "clk_harness").is_dir()
    assert (kdir / ".clk" / "scripts" / "clk").is_file()
    assert (kdir / ".gitignore").is_file()

    # State files written by the harness during init/idea/plan
    state = kdir / ".clk" / "state"
    assert state.is_dir()
    assert (state / "idea.json").is_file(), \
        f"idea.json missing — state files: {sorted(p.name for p in state.iterdir())}"
    idea = json.loads((state / "idea.json").read_text())
    assert idea["statement"] == "A local-first journaling app"

    # Config matches the requested provider
    cfg = json.loads((kdir / ".clk" / "config" / "clk.config.json").read_text())
    assert cfg["default_provider"] == "shell"
    providers = json.loads((kdir / ".clk" / "config" / "providers.json").read_text())
    assert providers["active"] == "shell"


def test_kickoff_with_provider_override(kickoff_sandbox: Path) -> None:
    """--provider on the CLI should win over any .env / defaults."""
    res = _run_kickoff(
        kickoff_sandbox,
        "--provider", "shell",
        "--project-name", "override-test",
        "An idea",
    )
    assert res.returncode == 0, res.stderr

    kdir = sorted((kickoff_sandbox / "workspace").glob("kickoff-*"))[-1]
    cfg = json.loads((kdir / ".clk" / "config" / "clk.config.json").read_text())
    assert cfg["default_provider"] == "shell"
    assert cfg["project_name"] == "override-test"


def test_kickoff_rejects_unknown_option(kickoff_sandbox: Path) -> None:
    res = _run_kickoff(kickoff_sandbox, "--definitely-not-a-real-flag")
    assert res.returncode != 0
    # The shell parser said "unknown option"; argparse (which the thin
    # wrapper delegates to) says "unrecognized arguments".
    out = (res.stdout + res.stderr).lower()
    assert "unknown option" in out or "unrecognized arguments" in out


def test_kickoff_creates_independent_git_repo(kickoff_sandbox: Path) -> None:
    """Each kickoff dir gets its own .git so its commits don't land in
    whichever repo happens to wrap the workspace."""
    res = _run_kickoff(
        kickoff_sandbox,
        "Some idea",
    )
    assert res.returncode == 0, res.stderr
    kdir = sorted((kickoff_sandbox / "workspace").glob("kickoff-*"))[-1]
    assert (kdir / ".git").is_dir(), "kickoff dir should have its own .git"

    # And the harness should have made at least one commit during init/idea
    log = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(kdir),
        capture_output=True, text=True,
    )
    assert log.returncode == 0, log.stderr
    assert log.stdout.strip(), "expected at least one commit in the kickoff repo"


# ---------------------------------------------------------------------------
# Opt-in: exercise the chosen LLM provider end-to-end via kickoff.sh.
#
# The orchestrator (scripts/run_all_tests.sh) collects an LLM provider +
# credentials interactively and exports them as CLK_* env vars.  If the
# user selected anything other than 'shell' AND provided enough state to
# actually hit it, run kickoff one more time against that real backend
# and assert the same plumbing.  Otherwise this test is skipped.
# ---------------------------------------------------------------------------


def _real_provider_ready() -> tuple[bool, str]:
    """Return (ready, reason).  Ready means a non-shell provider was
    selected and the minimum credentials for it are present."""
    p = (os.environ.get("CLK_PROVIDER") or "shell").lower()
    if p in ("", "shell"):
        return False, "CLK_PROVIDER is shell or unset"
    auth = (os.environ.get("CLK_AUTH_MODE") or "cli").lower()
    if p == "claude" and auth == "apikey" and not os.environ.get("ANTHROPIC_API_KEY"):
        return False, "ANTHROPIC_API_KEY missing"
    if p == "codex" and auth == "apikey" and not os.environ.get("OPENAI_API_KEY"):
        return False, "OPENAI_API_KEY missing"
    if p == "gemini" and auth == "apikey" and not (
        os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    ):
        return False, "GEMINI_API_KEY/GOOGLE_API_KEY missing"
    if p == "ollama" and not os.environ.get("CLK_OLLAMA_ENDPOINT"):
        return False, "CLK_OLLAMA_ENDPOINT missing"
    if p == "openwebui" and not (
        os.environ.get("CLK_OPENWEBUI_ENDPOINT") and os.environ.get("CLK_OPENWEBUI_MODEL")
    ):
        return False, "OpenWebUI endpoint/model missing"
    return True, ""


def test_kickoff_with_user_selected_provider(kickoff_sandbox: Path) -> None:
    """Opt-in smoke test: kickoff against whichever provider the orchestrator
    chose interactively.  Skipped when only the shell provider is configured."""
    ready, reason = _real_provider_ready()
    if not ready:
        pytest.skip(f"skip: real-provider smoke test not configured ({reason})")

    # Forward the orchestrator's CLK_* env vars verbatim into the child
    # kickoff.sh — overriding the shell default we set in _run_kickoff.
    forward = {
        k: v for k, v in os.environ.items()
        if k.startswith("CLK_") or k in (
            "ANTHROPIC_API_KEY", "OPENAI_API_KEY",
            "GEMINI_API_KEY", "GOOGLE_API_KEY",
        )
    }
    forward["CLK_MAX_ITERATIONS"] = "1"
    res = _run_kickoff(
        kickoff_sandbox,
        "A local-first journaling app",
        extra_env=forward,
        timeout=600,
    )
    assert res.returncode == 0, (
        f"kickoff with provider={forward.get('CLK_PROVIDER')!r} exited "
        f"{res.returncode}\nSTDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    kdir = sorted((kickoff_sandbox / "workspace").glob("kickoff-*"))[-1]
    providers = json.loads((kdir / ".clk" / "config" / "providers.json").read_text())
    assert providers["active"] == forward.get("CLK_PROVIDER")
