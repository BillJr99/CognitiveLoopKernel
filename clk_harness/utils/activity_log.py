"""Single consolidated activity log for a CLK kickoff.

Every meaningful event in the harness flows through here as one
JSONL entry per line, written to ``.clk/logs/activity.jsonl``.
Captures, in chronological order:

  * agent dispatches (objective, stage, workflow, iteration)
  * full prompt text (with size and a link to the on-disk copy
    under .clk/runs/<run_id>/prompt.txt)
  * subprocess lifecycle (start/stdout/stderr/end/timeout)
  * agent responses (ok, error, token usage, files reported)
  * role minting / refresh / removal (with the prompt body length)
  * workflow YAML rewrites
  * every executed action (write / edit / append / delete / run /
    done) with the resolved path and outcome
  * git commits triggered by action batches

Design choices
- JSONL because it's easy to ``jq``, ``grep``, or load into pandas.
- Fully self-contained: each line carries enough context (agent +
  ts + event kind + payload) to be read without correlation.
- Lives under .clk/logs/ which is gitignored, so we can be verbose
  without polluting commits. Persists across sessions for analysis.
- Process-level singleton handle so callers don't pay for repeat
  open() syscalls. Best-effort: failures are swallowed (we never
  let logging break a real run).
"""

from __future__ import annotations

import json
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional

from ..config import Paths


_LOCK = threading.Lock()
_HANDLES: Dict[str, Any] = {}


def _open_for(paths: Paths):
    """Lazily open / cache the activity log file handle for ``paths``.

    Cached by absolute path so reopening across multiple commands in
    the same process is cheap. Returns None if open() fails.
    """
    target = paths.logs / "activity.jsonl"
    key = str(target)
    with _LOCK:
        existing = _HANDLES.get(key)
        if existing is not None:
            return existing
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fh = target.open("a", encoding="utf-8")
            _HANDLES[key] = fh
            return fh
        except Exception as exc:
            print(f"[activity_log._open_for] failed for {target}: {exc}", file=sys.stderr)
            return None


def log_event(paths: Paths, event: str, **fields: Any) -> None:
    """Append one JSON object to the activity log.

    ``event`` is the canonical event kind (e.g. ``"agent_dispatch"``,
    ``"action_applied"``). Extra ``fields`` are merged into the same
    object so consumers can filter on whatever attribute they need.
    Never raises.
    """
    if paths is None:
        return
    fh = _open_for(paths)
    if fh is None:
        return
    payload: Dict[str, Any] = {
        "ts": datetime.now().isoformat(timespec="milliseconds"),
        "event": event,
    }
    for k, v in fields.items():
        # JSON-stringify any value that isn't natively serializable.
        try:
            json.dumps(v)
            payload[k] = v
        except (TypeError, ValueError):
            payload[k] = str(v)
    line = json.dumps(payload, ensure_ascii=False)
    try:
        with _LOCK:
            fh.write(line + "\n")
            fh.flush()
    except Exception as exc:
        print(f"[activity_log.log_event] failed: {exc}", file=sys.stderr)


def close_all() -> None:
    """Close every cached handle. Safe to call multiple times."""
    with _LOCK:
        for fh in list(_HANDLES.values()):
            try:
                fh.close()
            except Exception:
                pass
        _HANDLES.clear()
