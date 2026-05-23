"""Tail ``activity.jsonl`` and yield interesting events for push.

Used by the bot to send live updates to subscribed chats. Implements a
simple poll-based tailer (mtime + seek) so it works on every filesystem
including the Pi's SD card.

Coalescing: if more than ``BURST_LIMIT`` events arrive within
``BURST_WINDOW`` seconds, they are collapsed into a single summary
message to stay safely under Telegram's 30 messages-per-second cap.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import AsyncIterator, Dict, Iterable, Optional, Set

INTERESTING_EVENTS: Set[str] = {
    "agent_dispatch",
    "agent_response",
    "action_applied",
    "git_commit",
    "iteration_outcome",
    "eval_result",
    "role_registered",
    "error",
    "subprocess_timeout",
}

BURST_LIMIT = 3
BURST_WINDOW = 2.0


def format_event(evt: Dict[str, object]) -> str:
    """Pretty-print a single activity-log event for chat."""
    ev = str(evt.get("event", "?"))
    ts = str(evt.get("ts", ""))
    agent = evt.get("agent") or evt.get("from") or ""
    obj = evt.get("objective") or evt.get("path") or evt.get("summary") or ""
    head = f"[{ts}] {ev}"
    if agent:
        head += f" · {agent}"
    if obj:
        # Trim objective so the line stays short.
        text = str(obj)
        if len(text) > 200:
            text = text[:197] + "..."
        head += f"\n  {text}"
    return head


async def tail_activity(
    path: Path,
    *,
    poll_interval: float = 0.5,
    from_start: bool = False,
) -> AsyncIterator[Dict[str, object]]:
    """Async generator yielding parsed JSON events as they're appended.

    Tolerates the file not existing yet (waits for it). Skips malformed
    lines silently -- the log is best-effort and we never want a bad row
    to crash the bot.
    """
    pos = 0
    while not path.exists():
        await asyncio.sleep(poll_interval)
    if not from_start:
        try:
            pos = path.stat().st_size
        except OSError:
            pos = 0
    while True:
        try:
            size = path.stat().st_size
        except OSError:
            await asyncio.sleep(poll_interval)
            continue
        if size < pos:
            # File was truncated/rotated.
            pos = 0
        if size > pos:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                fh.seek(pos)
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue
                pos = fh.tell()
        await asyncio.sleep(poll_interval)


async def interesting_events(
    path: Path,
    *,
    poll_interval: float = 0.5,
    kinds: Optional[Iterable[str]] = None,
) -> AsyncIterator[Dict[str, object]]:
    """Filter ``tail_activity`` down to ``kinds`` (defaults to interesting)."""
    allowed = set(kinds) if kinds is not None else INTERESTING_EVENTS
    async for evt in tail_activity(path, poll_interval=poll_interval):
        if str(evt.get("event", "")) in allowed:
            yield evt


class Coalescer:
    """Collapse event bursts into a single summary message.

    Drop-in helper around ``interesting_events``. Call ``feed(evt)`` for
    every event and then ``drain()`` periodically (or rely on
    ``maybe_emit`` for an event-driven model).
    """

    def __init__(
        self,
        *,
        limit: int = BURST_LIMIT,
        window: float = BURST_WINDOW,
        clock=time.monotonic,
    ) -> None:
        self.limit = limit
        self.window = window
        self._clock = clock
        self._buf: list = []
        self._first_ts: float = 0.0

    def feed(self, evt: Dict[str, object]) -> Optional[str]:
        """Add ``evt``. Returns a message string if a flush should happen."""
        now = self._clock()
        if not self._buf:
            self._first_ts = now
        self._buf.append(evt)
        if len(self._buf) >= self.limit and (now - self._first_ts) <= self.window:
            return self._flush_summary()
        if (now - self._first_ts) > self.window:
            return self._flush_individual()
        return None

    def drain(self) -> Optional[str]:
        if not self._buf:
            return None
        return self._flush_individual()

    def _flush_summary(self) -> str:
        count = len(self._buf)
        last = self._buf[-1]
        msg = f"({count} events in {self.window:.0f}s burst)\n" + format_event(last)
        self._buf.clear()
        return msg

    def _flush_individual(self) -> str:
        msgs = [format_event(e) for e in self._buf]
        self._buf.clear()
        return "\n\n".join(msgs)
