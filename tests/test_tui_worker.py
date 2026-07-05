"""Tests for the Worker's slash-command handlers in clk_harness.tui.

The Worker runs in a daemon thread, but we don't need the thread loop
here — each ``_do_*`` handler is callable directly. We instantiate a
Worker via ``__new__`` to bypass argument validation, then attach the
minimum dependencies it touches: state, paths, providers_cfg.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness.config import (
    DEFAULT_AGENTS,
    DEFAULT_CLK_CONFIG,
    DEFAULT_PROVIDERS,
    Paths,
    save_json,
)
from clk_harness.tui import DashboardState, Worker


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Initialize a CLK workspace under tmp_path."""
    paths = Paths(root=tmp_path)
    paths.ensure()
    save_json(paths.config / "clk.config.json", DEFAULT_CLK_CONFIG)
    save_json(paths.config / "providers.json", DEFAULT_PROVIDERS)
    save_json(paths.config / "agents.json", DEFAULT_AGENTS)
    return tmp_path


class _StubRunner:
    """Minimal stand-in so handlers that touch worker.runner don't crash."""
    providers_cfg: dict = {}


@pytest.fixture
def worker(workspace: Path) -> Worker:
    paths = Paths(root=workspace)
    state = DashboardState(
        ["chief", "qa", "ralph"],
        paths=paths,
        agents_cfg=DEFAULT_AGENTS,
    )
    state.provider = "shell"
    w = Worker.__new__(Worker)
    w.paths = paths
    w.state = state
    w.clk_cfg = DEFAULT_CLK_CONFIG
    w.providers_cfg = DEFAULT_PROVIDERS
    w.runner = _StubRunner()
    # The handlers we test don't need a real evaluator.
    return w


def _init_git_no_signing(workspace: Path) -> None:
    """Init a local git repo and disable commit signing.

    The sandboxed environment routes commits through a signing server
    that isn't available offline, so we disable signing for test
    commits via `commit.gpgsign=false`.
    """
    import subprocess
    subprocess.run(["git", "init", "-q"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.email", "t@t.t"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "commit.gpgsign", "false"], cwd=workspace, check=True)
    subprocess.run(["git", "config", "tag.gpgsign", "false"], cwd=workspace, check=True)


def test_emit_status_prints_session_snapshot(worker: Worker) -> None:
    worker.state.set_idea("a journaling app")
    worker.state.set_phase("workflow:engineering", busy=True)
    worker._emit_status()
    msgs = [text for _, text in worker.state.conversation if text]
    joined = "\n".join(msgs)
    assert "session snapshot" in joined
    assert "workflow:engineering" in joined or "phase" in joined
    assert "session cost" in joined.lower() or "est. cost" in joined.lower()


def test_do_install_warns_on_empty_tool(worker: Worker) -> None:
    worker._do_install("")
    logged = [line.text for line in worker.state.log]
    assert any("no tool specified" in line for line in logged)


def test_do_configure_warns_on_empty_tool(worker: Worker) -> None:
    worker._do_configure("")
    logged = [line.text for line in worker.state.log]
    assert any("no tool specified" in line for line in logged)


def test_do_set_provider_unknown_logs_warning(worker: Worker) -> None:
    worker._do_set_provider("definitely-not-a-real-provider")
    logged = [line.text for line in worker.state.log]
    assert any("isn't a known provider" in line for line in logged)


def test_do_set_provider_switches_active_when_known(worker: Worker) -> None:
    worker._do_set_provider("shell")
    msgs = [text for _, text in worker.state.conversation]
    assert any("→ shell" in m or "provider" in m.lower() for m in msgs)
    assert worker.state.provider == "shell"


def test_do_undo_refuses_with_uncommitted_changes(worker: Worker, workspace: Path) -> None:
    # Initialize a git repo with one commit, then leave a dirty file.
    import subprocess
    _init_git_no_signing(workspace)
    (workspace / "README.md").write_text("hi", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=workspace, check=True)
    (workspace / "DIRTY.md").write_text("uncommitted change", encoding="utf-8")
    worker._do_undo(confirm=False)
    logged = [line.text for line in worker.state.log]
    assert any("undo refused" in line for line in logged)


def test_do_undo_preview_then_confirm_reverts(worker: Worker, workspace: Path) -> None:
    import subprocess
    _init_git_no_signing(workspace)
    (workspace / "a.txt").write_text("first", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "first"], cwd=workspace, check=True)
    (workspace / "b.txt").write_text("second", encoding="utf-8")
    subprocess.run(["git", "add", "."], cwd=workspace, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "second-commit-to-undo"], cwd=workspace, check=True)
    # Preview pass.
    worker._do_undo(confirm=False)
    msgs = [text for _, text in worker.state.conversation]
    assert any("/undo confirm" in m for m in msgs)
    # Confirm pass.
    worker._do_undo(confirm=True)
    log = subprocess.run(["git", "log", "--oneline"], cwd=workspace, capture_output=True, text=True)
    # New revert commit added — there are now 3 commits, latest being a revert.
    assert "Revert" in log.stdout or "revert" in log.stdout


def test_do_workspaces_list_runs_without_workspace(worker: Worker) -> None:
    # No workspace/ parent dir → handler should print a friendly message.
    worker._do_workspaces({"action": "list", "args": []})
    msgs = [text for _, text in worker.state.conversation]
    assert any("workspaces" in m.lower() for m in msgs)


def test_do_workspaces_rename_validates_args(worker: Worker) -> None:
    worker._do_workspaces({"action": "rename", "args": ["only-one-arg"]})
    logged = [line.text for line in worker.state.log]
    assert any("usage" in line.lower() for line in logged)


def test_do_doctor_emits_findings(worker: Worker) -> None:
    worker._do_doctor(fix=False)
    msgs = [text for _, text in worker.state.conversation]
    joined = "\n".join(msgs)
    assert "doctor" in joined.lower()


def test_do_diag_writes_tarball(worker: Worker, workspace: Path) -> None:
    (workspace / ".env").write_text("ANTHROPIC_API_KEY=sk-fake-secret-test\n", encoding="utf-8")
    worker._do_diag()
    tarballs = list(workspace.glob("clk-diag-*.tar.gz"))
    assert tarballs, "expected /diag to write a tarball at the project root"
    msgs = [text for _, text in worker.state.conversation]
    assert any("diag" in m.lower() for m in msgs)
