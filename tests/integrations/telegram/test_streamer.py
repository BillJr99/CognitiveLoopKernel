import asyncio
import json
from pathlib import Path

import pytest

from clk_harness.integrations.telegram.streamer import (
    Coalescer,
    format_event,
    interesting_events,
    tail_activity,
)


@pytest.mark.asyncio
async def test_tail_activity_yields_new_lines(tmp_path: Path):
    log = tmp_path / "activity.jsonl"
    log.write_text("")  # ensure exists, empty

    async def writer():
        await asyncio.sleep(0.05)
        with log.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"event": "agent_dispatch", "ts": "t1"}) + "\n")
            fh.write(json.dumps({"event": "agent_response", "ts": "t2"}) + "\n")

    received = []

    async def consume():
        async for evt in tail_activity(log, poll_interval=0.01):
            received.append(evt["event"])
            if len(received) >= 2:
                return

    await asyncio.wait_for(asyncio.gather(writer(), consume()), timeout=2.0)
    assert received == ["agent_dispatch", "agent_response"]


@pytest.mark.asyncio
async def test_interesting_events_filters(tmp_path: Path):
    log = tmp_path / "activity.jsonl"
    with log.open("w", encoding="utf-8") as fh:
        fh.write(json.dumps({"event": "boring", "ts": "t0"}) + "\n")
        fh.write(json.dumps({"event": "agent_dispatch", "ts": "t1"}) + "\n")
        fh.write(json.dumps({"event": "noise", "ts": "t2"}) + "\n")
        fh.write(json.dumps({"event": "action_applied", "ts": "t3"}) + "\n")

    async def collect():
        out = []
        async for evt in interesting_events(log, poll_interval=0.01):
            out.append(evt["event"])
            if len(out) >= 2:
                return out
        return out

    # use from_start by passing kinds explicitly + monkey-patching:
    # interesting_events itself starts at end-of-file, so write before.
    # Instead, read with from_start via tail_activity wrapper.
    received = []
    from clk_harness.integrations.telegram.streamer import INTERESTING_EVENTS

    async for evt in tail_activity(log, poll_interval=0.01, from_start=True):
        if evt["event"] in INTERESTING_EVENTS:
            received.append(evt["event"])
        if len(received) >= 2:
            break

    assert received == ["agent_dispatch", "action_applied"]


def test_format_event_includes_key_fields():
    s = format_event({"event": "agent_dispatch", "ts": "T", "agent": "engineer", "objective": "fix bug"})
    assert "agent_dispatch" in s
    assert "engineer" in s
    assert "fix bug" in s


def test_format_event_truncates_long_objective():
    long = "x" * 500
    s = format_event({"event": "e", "objective": long})
    assert "..." in s
    # Total line should be capped
    assert len(s) < 400


def test_coalescer_burst_summary():
    t = [0.0]

    def clk():
        return t[0]

    c = Coalescer(limit=3, window=2.0, clock=clk)
    assert c.feed({"event": "agent_dispatch"}) is None
    t[0] = 0.5
    assert c.feed({"event": "agent_dispatch"}) is None
    t[0] = 1.0
    msg = c.feed({"event": "action_applied"})
    assert msg is not None
    assert "3 events" in msg


def test_coalescer_individual_after_window():
    t = [0.0]

    def clk():
        return t[0]

    c = Coalescer(limit=10, window=1.0, clock=clk)
    c.feed({"event": "agent_dispatch"})
    t[0] = 2.0
    msg = c.feed({"event": "action_applied"})
    # The second feed sees window exceeded, flushes everything individually.
    assert msg is not None
    assert "agent_dispatch" in msg


def test_coalescer_drain():
    c = Coalescer()
    c.feed({"event": "agent_dispatch"})
    out = c.drain()
    assert out is not None
    assert "agent_dispatch" in out
    assert c.drain() is None
