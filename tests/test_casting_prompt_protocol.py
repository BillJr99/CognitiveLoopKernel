"""Dynamic role prompts must carry the harness protocol blocks.

The chief drafts PROPOSE_ROLE prompt bodies focused on domain expertise;
the harness appends the ACTION/POST protocol and compliance footer so
dynamic agents emit parseable blocks instead of prose. These tests pin
that behavior for the write, heal, and scaffold paths plus the
dispatch-time in-memory healing in AgentRunner.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration import casting


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def test_protocol_suffix_contains_required_blocks() -> None:
    s = casting._harness_protocol_suffix()
    assert casting._PROTOCOL_MARKER in s
    assert "END_ACTION" in s
    assert "PRODUCES" in s
    assert "FINAL COMPLIANCE CHECK" in s
    assert "$outputs_contract" in s


def test_chief_drafted_prompt_gets_protocol_appended(paths: Paths) -> None:
    body = "You are the **Content Creator** agent.\n\nWrite LinkedIn posts.\n"
    fname = casting._ensure_prompt_file(paths, "content_creator", body, "writes posts")
    written = (paths.prompts / fname).read_text(encoding="utf-8")
    assert written.startswith("You are the **Content Creator** agent.")
    assert casting._PROTOCOL_MARKER in written
    assert "FINAL COMPLIANCE CHECK" in written


def test_prompt_already_carrying_protocol_not_duplicated(paths: Paths) -> None:
    body = "Domain prompt.\n\n" + casting._harness_protocol_suffix()
    fname = casting._ensure_prompt_file(paths, "specialist", body, "role")
    written = (paths.prompts / fname).read_text(encoding="utf-8")
    assert written.count(casting._PROTOCOL_MARKER) == 1


def test_existing_stale_prompt_healed_in_place(paths: Paths) -> None:
    paths.prompts.mkdir(parents=True, exist_ok=True)
    stale = paths.prompts / "old_role.md"
    stale.write_text("You are an old role with no protocol.\n", encoding="utf-8")
    casting._ensure_prompt_file(paths, "old_role", "", "old role")
    healed = stale.read_text(encoding="utf-8")
    assert casting._PROTOCOL_MARKER in healed
    assert healed.startswith("You are an old role with no protocol.")


def test_scaffold_includes_protocol(paths: Paths) -> None:
    fname = casting._ensure_prompt_file(paths, "fresh_role", "", "brand new")
    written = (paths.prompts / fname).read_text(encoding="utf-8")
    assert casting._PROTOCOL_MARKER in written
    assert "ACTION blocks" in written


def test_dispatch_time_healing_skips_footer_only_prompts(paths: Paths) -> None:
    # critic.md-style prompts deliberately carry only the base footer
    # ("Self-assessment footer ...") — dispatch-time healing must not
    # append the action protocol to those.
    from clk_harness.orchestration.agent import AgentRunner

    paths.prompts.mkdir(parents=True, exist_ok=True)
    (paths.prompts / "scorer.md").write_text(
        "You are a scorer.\n\nSelf-assessment footer (read by the harness)\n",
        encoding="utf-8",
    )
    (paths.prompts / "bare.md").write_text(
        "You are a bare dynamic agent.\n", encoding="utf-8"
    )
    runner = AgentRunner.__new__(AgentRunner)  # skip __init__: only paths needed
    runner.paths = paths
    footer_only = runner._load_prompt_template("scorer.md")
    assert casting._PROTOCOL_MARKER not in footer_only
    bare = runner._load_prompt_template("bare.md")
    assert casting._PROTOCOL_MARKER in bare
