"""Tests for DashboardState behavior — token accounting, cost tracking,
error classification, snapshot, conversation handling. These cover the
state surface that the curses front-end and the API both read from.

We do NOT drive curses here. The Worker and TuiApp classes are
exercised separately in tests/test_tui_worker.py and
tests/test_tui_hints.py.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.tui import AgentStatus, DashboardState


@pytest.fixture
def state(tmp_path: Path) -> DashboardState:
    paths = Paths(root=tmp_path)
    paths.ensure()
    return DashboardState(
        agent_names=["chief", "qa", "engineer"],
        paths=paths,
        agents_cfg={
            "agents": {
                "chief":    {"role": "decompose", "provider": "shell"},
                "qa":       {"role": "audit",      "provider": "shell"},
                "engineer": {"role": "implement",  "provider": "claude"},
            }
        },
    )


def test_state_initial_fields(state: DashboardState) -> None:
    assert state.idea == ""
    assert state.phase == "idle"
    assert state.busy is False
    assert state.total_tokens == 0
    assert state.total_usd == 0.0
    assert state.last_error_kind == ""
    assert state.in_tutorial is False
    assert state.github_ahead == 0
    assert set(state.agents.keys()) == {"chief", "qa", "engineer"}


def test_set_idea_clamps_to_500_chars(state: DashboardState) -> None:
    long = "x" * 1000
    state.set_idea(long)
    assert len(state.idea) == 500


def test_set_phase_toggles_busy(state: DashboardState) -> None:
    state.set_phase("running", busy=True)
    assert state.phase == "running"
    assert state.busy is True
    state.set_phase("idle", busy=False)
    assert state.phase == "idle"
    assert state.busy is False


def test_request_and_clear_stop(state: DashboardState) -> None:
    assert not state.is_stop_requested()
    state.request_stop()
    assert state.is_stop_requested()
    state.clear_stop()
    assert not state.is_stop_requested()


def test_user_and_system_messages_routed_to_conversation(state: DashboardState) -> None:
    state.add_user_message("hello")
    state.add_system_message("welcome")
    convo = list(state.conversation)
    assert ("user", "hello") in convo
    assert ("system", "welcome") in convo


def test_upsert_and_drop_agent(state: DashboardState) -> None:
    state.upsert_agent("researcher", role="open question explorer", baseline=False, status="added")
    assert "researcher" in state.agents
    assert state.agents["researcher"].role == "open question explorer"
    state.drop_agent("researcher")
    assert "researcher" not in state.agents


def test_begin_agent_marks_working_and_records_started(state: DashboardState) -> None:
    state.begin_agent("chief", "decompose the idea")
    assert state.agents["chief"].status == AgentStatus.WORKING
    assert state.agents["chief"].current_task == "decompose the idea"
    assert state.agents["chief"].last_started_mono > 0


def test_end_agent_ok_clears_error_state(state: DashboardState) -> None:
    # First inject an error so we can confirm the success path clears it.
    state.last_error_kind = "not_installed"
    state.last_error_command = "/install"
    state.begin_agent("engineer", "build something")
    state.end_agent(
        "engineer",
        ok=True,
        preview="Created src/main.py.\nDone.",
        error="",
        files_written=["src/main.py"],
        usage={"input_tokens": 100, "output_tokens": 200, "total_tokens": 300, "source": "claude-api"},
    )
    card = state.agents["engineer"]
    assert card.status == AgentStatus.DONE
    assert card.runs == 1
    assert card.total_files == 1
    assert card.total_tokens == 300
    # Successful run wipes the stale error hint.
    assert state.last_error_kind == ""
    # Project totals accumulate.
    assert state.total_tokens == 300
    assert state.total_input_tokens == 100
    assert state.total_output_tokens == 200
    # And cost rolled up via the pricing table (engineer has provider=claude).
    assert state.total_usd > 0


def test_end_agent_fail_with_classifiable_error_populates_hint(state: DashboardState) -> None:
    state.begin_agent("engineer", "build something")
    state.end_agent(
        "engineer",
        ok=False,
        preview="",
        error="claude CLI not found on PATH",
        files_written=[],
        usage={},
    )
    card = state.agents["engineer"]
    assert card.status == AgentStatus.PROVIDER_ERROR
    assert state.last_error_kind == "not_installed"
    assert state.last_error_command == "/install"


def test_end_agent_token_totals_accumulate_across_runs(state: DashboardState) -> None:
    state.begin_agent("chief", "step 1")
    state.end_agent("chief", ok=True, preview="ok", error="", files_written=[],
                    usage={"input_tokens": 10, "output_tokens": 20, "total_tokens": 30})
    state.begin_agent("chief", "step 2")
    state.end_agent("chief", ok=True, preview="ok", error="", files_written=[],
                    usage={"input_tokens": 5, "output_tokens": 15, "total_tokens": 20})
    assert state.agents["chief"].runs == 2
    assert state.agents["chief"].total_tokens == 50
    assert state.total_tokens == 50


def test_snapshot_returns_a_copy(state: DashboardState) -> None:
    state.set_idea("test idea")
    state.set_phase("running", busy=True)
    snap = state.snapshot()
    assert snap["idea"] == "test idea"
    assert snap["phase"] == "running"
    assert snap["busy"] is True
    assert "chief" in snap["agents"]
    # Mutating the snapshot doesn't mutate state.
    snap["idea"] = "MUTATED"
    assert state.idea == "test idea"


def test_add_log_records_messages(state: DashboardState) -> None:
    state.add_log("first")
    state.add_log("second")
    msgs = [line.text for line in state.log]
    assert "first" in msgs
    assert "second" in msgs


def test_add_log_splits_multiline_input(state: DashboardState) -> None:
    state.add_log("line one\nline two\nline three")
    msgs = [line.text for line in state.log]
    assert "line one" in msgs
    assert "line two" in msgs
    assert "line three" in msgs


def test_report_progress_start_records_pid_from_message(state: DashboardState) -> None:
    state.begin_agent("chief", "x")
    state.report_progress("chief", "start", "pid=12345 cmd=claude --print")
    state.report_progress("chief", "stdout_line", "thinking...")
    card = state.agents["chief"]
    assert card.live_pid == 12345
    assert "thinking" in card.live_last_line


def test_cost_uses_per_provider_override(tmp_path: Path) -> None:
    # When .clk/config/providers.json declares a pricing override,
    # end_agent should pick it up via the load_providers_config import.
    paths = Paths(root=tmp_path)
    paths.ensure()
    # Write a providers.json with an override on claude.
    import json
    cfg = {
        "providers": {
            "claude": {
                "type": "claude",
                "pricing": {"input_per_1k": 100.0, "output_per_1k": 200.0},
            }
        },
        "active": "claude",
    }
    (paths.config / "providers.json").write_text(json.dumps(cfg), encoding="utf-8")
    state = DashboardState(
        agent_names=["engineer"],
        paths=paths,
        agents_cfg={"agents": {"engineer": {"provider": "claude"}}},
    )
    state.begin_agent("engineer", "x")
    state.end_agent(
        "engineer", ok=True, preview="ok", error="",
        files_written=[],
        usage={"input_tokens": 1000, "output_tokens": 1000},
    )
    # $100/1k * 1k + $200/1k * 1k = $300
    assert abs(state.total_usd - 300.0) < 0.001
