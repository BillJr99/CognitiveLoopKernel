"""Action sandbox: project root anchor, .clk/ exclusion, workspace/ legacy stripping."""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration import actions


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def test_resolve_safe_accepts_project_root_path(paths: Paths) -> None:
    target = actions._resolve_safe(paths.root, "src/main.py")
    assert target is not None
    assert target == (paths.root / "src" / "main.py").resolve()


def test_resolve_safe_strips_legacy_workspace_prefix(paths: Paths) -> None:
    target = actions._resolve_safe(paths.root, "workspace/src/main.py")
    assert target is not None
    assert target == (paths.root / "src" / "main.py").resolve()


def test_resolve_safe_rejects_dot_clk_writes(paths: Paths) -> None:
    """Agents cannot write into harness state via ACTION:write."""
    assert actions._resolve_safe(paths.root, ".clk/state/forbidden.md") is None
    assert actions._resolve_safe(paths.root, ".clk/blackboard/anything.json") is None
    assert actions._resolve_safe(paths.root, ".clk/config/agents.json") is None


def test_resolve_safe_rejects_absolute_paths(paths: Paths) -> None:
    assert actions._resolve_safe(paths.root, "/etc/passwd") is None


def test_resolve_safe_rejects_escapes(paths: Paths) -> None:
    assert actions._resolve_safe(paths.root, "../escape.md") is None
    assert actions._resolve_safe(paths.root, "src/../../escape.md") is None


def test_apply_actions_blocks_dot_clk_write(paths: Paths) -> None:
    text = """
ACTION: write
PATH: .clk/state/sneaky.md
CONTENT:
hello
END_ACTION
"""
    result = actions.apply_actions(paths, text, agent_name="test")
    # The action is rejected — nothing landed in workspace, and the
    # skipped list records the reason.
    assert not (paths.clk / "state" / "sneaky.md").exists()
    assert any("path_outside" in s or "outside" in s.lower() for s in result.skipped) or \
           result.is_empty() or len(result.skipped) >= 1


def test_apply_actions_writes_at_project_root(paths: Paths) -> None:
    text = """
ACTION: write
PATH: README.md
CONTENT:
# Hello
END_ACTION
"""
    result = actions.apply_actions(paths, text, agent_name="test")
    assert (paths.root / "README.md").exists()
    assert any(f.endswith("README.md") for f in result.files_written)
