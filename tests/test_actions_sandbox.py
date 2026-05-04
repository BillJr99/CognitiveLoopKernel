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
    """Agents cannot write into harness state via ACTION:write (except blackboard)."""
    assert actions._resolve_safe(paths.root, ".clk/state/forbidden.md") is None
    assert actions._resolve_safe(paths.root, ".clk/config/agents.json") is None


def test_resolve_safe_allows_blackboard_writes(paths: Paths) -> None:
    """Blackboard is an explicit exception: agents may write there directly."""
    target = actions._resolve_safe(paths.root, ".clk/blackboard/my-post.json", allow_blackboard=True)
    assert target is not None
    assert target == (paths.root / ".clk" / "blackboard" / "my-post.json").resolve()


def test_resolve_safe_rejects_blackboard_without_flag(paths: Paths) -> None:
    """Without allow_blackboard, blackboard paths are rejected like any .clk/ path."""
    assert actions._resolve_safe(paths.root, ".clk/blackboard/my-post.json") is None


def test_normalize_rel_rewrites_bare_blackboard_prefix(paths: Paths) -> None:
    """``blackboard/`` is rewritten to ``.clk/blackboard/`` automatically."""
    result = actions._normalize_rel(paths.root, "blackboard/my-post.json")
    assert result == ".clk/blackboard/my-post.json"


def test_resolve_safe_routes_bare_blackboard_to_clk(paths: Paths) -> None:
    """``blackboard/x`` resolves into ``.clk/blackboard/x``, not project root."""
    target = actions._resolve_safe(paths.root, "blackboard/my-post.json", allow_blackboard=True)
    assert target is not None
    assert target == (paths.root / ".clk" / "blackboard" / "my-post.json").resolve()


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
    # The action is rejected — nothing landed in harness state, and the
    # skipped list records the reason.
    assert not (paths.clk / "state" / "sneaky.md").exists()
    assert any("path_outside" in s or "outside" in s.lower() for s in result.skipped) or \
           result.is_empty() or len(result.skipped) >= 1


def test_apply_actions_allows_blackboard_write(paths: Paths) -> None:
    """Agents can write JSON to blackboard/ (routed to .clk/blackboard/)."""
    import json
    post_data = {"id": "test-post", "author": "test-agent", "post_type": "note",
                 "body": "hello", "ts": "", "stage_id": "", "workflow": "",
                 "consumes": [], "produces": []}
    text = f"""
ACTION: write
PATH: blackboard/test-post.json
CONTENT:
{json.dumps(post_data)}
END_ACTION
"""
    result = actions.apply_actions(paths, text, agent_name="test")
    assert (paths.blackboard / "test-post.json").exists()
    assert not result.is_empty()


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
