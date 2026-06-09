"""Unit tests for activity.jsonl -> dashboard snapshot (clk_harness/web_snapshot.py)."""

from __future__ import annotations

import json
from pathlib import Path

from clk_harness import web_snapshot


def _events() -> list:
    return [
        {"event": "default_agent_created", "agent": "engineer", "role": "build the thing"},
        {"event": "agent_dispatch", "agent": "engineer", "run_id": "r1",
         "provider": "claude (claude)", "role": "build", "workflow": "engineering"},
        {"event": "prompt_sent", "agent": "engineer", "run_id": "r1",
         "prompt": "please build", "prompt_chars": 12, "prompt_path": ".clk/runs/r1/prompt.txt"},
        {"event": "agent_response", "agent": "engineer", "run_id": "r1", "ok": True,
         "tokens_in": 1000, "tokens_out": 500, "tokens_total": 1500,
         "response_text": "Decision: ship it\nmore", "files_reported": ["app.py", "test.py"]},
        {"event": "action_applied", "agent": "engineer", "path": "README.md", "op": "write"},
        {"event": "git_commit", "message": "add app"},
    ]


def test_build_snapshot_basic_fold() -> None:
    snap = web_snapshot.build_snapshot(_events(), active_provider="claude", idea="my idea")
    card = snap["agents"]["engineer"]
    assert card["status"] == "done"
    assert card["runs"] == 1
    assert card["tokens_total"] == 1500
    assert card["last_thought"].startswith("Decision:")
    assert card["role"] == "build"
    assert card["provider"] == "claude"
    assert "app.py" in card["files"]

    totals = snap["totals"]
    assert totals["total_tokens"] == 1500
    assert totals["total_usd"] > 0  # claude has non-zero pricing
    assert totals["commits"] == 1
    assert totals["peak_run_tokens"] == 1500
    # files from response + action
    assert "README.md" in snap["files_changed"]
    assert "app.py" in snap["files_changed"]
    assert snap["idea"] == "my idea"
    assert snap["phase"] == "engineering"


def test_failed_response_marks_provider_or_failed() -> None:
    evs = [
        {"event": "agent_dispatch", "agent": "qa", "run_id": "r1", "provider": "claude (claude)"},
        {"event": "agent_response", "agent": "qa", "run_id": "r1", "ok": False,
         "error": "rate limit exceeded", "tokens_in": 0, "tokens_out": 0, "tokens_total": 0},
    ]
    snap = web_snapshot.build_snapshot(evs, active_provider="claude")
    card = snap["agents"]["qa"]
    assert card["status"] == "provider"  # rate_limit -> provider error class
    assert card["error_kind"] == "rate_limit"


def test_recovering_status_on_retry() -> None:
    evs = [
        {"event": "agent_dispatch", "agent": "eng", "run_id": "r1", "provider": "shell (shell)"},
        {"event": "provider_retry", "agent": "eng", "run_id": "r1", "attempt": 1, "error": "boom"},
    ]
    snap = web_snapshot.build_snapshot(evs, active_provider="shell")
    assert snap["agents"]["eng"]["status"] == "recovering"
    assert snap["busy"] is True  # dispatch with no response yet


def test_normalize_event_severity_and_category() -> None:
    ok = web_snapshot.normalize_event({"event": "agent_response", "ok": True, "agent": "a"}, 0)
    assert ok["severity"] == "success"
    bad = web_snapshot.normalize_event({"event": "agent_response", "ok": False, "agent": "a"}, 1)
    assert bad["severity"] == "error"
    act = web_snapshot.normalize_event({"event": "action_applied", "agent": "a", "path": "x"}, 2)
    assert act["category"] == "action"
    unknown = web_snapshot.normalize_event({"event": "totally_new_kind"}, 3)
    assert unknown["severity"] == "info" and unknown["category"] == "event"


def test_iter_events_offset_and_partial_line(tmp_path: Path) -> None:
    log = tmp_path / "activity.jsonl"
    e1 = json.dumps({"event": "a"})
    e2 = json.dumps({"event": "b"})
    log.write_text(e1 + "\n" + e2 + "\n", encoding="utf-8")

    events, offset = web_snapshot.iter_events(log, 0)
    assert [e["event"] for e in events] == ["a", "b"]
    assert offset == len((e1 + "\n" + e2 + "\n").encode())

    # No new complete lines -> nothing, offset unchanged.
    events2, offset2 = web_snapshot.iter_events(log, offset)
    assert events2 == []
    assert offset2 == offset

    # Append a partial (no newline) line: must NOT be consumed.
    with log.open("a", encoding="utf-8") as fh:
        fh.write('{"event": "c"')
    events3, offset3 = web_snapshot.iter_events(log, offset)
    assert events3 == []
    assert offset3 == offset

    # Complete the line -> now it parses.
    with log.open("a", encoding="utf-8") as fh:
        fh.write("}\n")
    events4, _ = web_snapshot.iter_events(log, offset)
    assert [e["event"] for e in events4] == ["c"]


def test_iter_events_missing_file(tmp_path: Path) -> None:
    events, offset = web_snapshot.iter_events(tmp_path / "nope.jsonl", 0)
    assert events == []
    assert offset == 0
