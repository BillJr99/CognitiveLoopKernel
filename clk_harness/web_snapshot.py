"""Turn the structured activity log into the web dashboard model.

The harness already writes one JSON object per line to
``.clk/logs/activity.jsonl`` (see
:func:`clk_harness.utils.activity_log.log_event`). That stream is far
richer than raw stdout — it carries agent dispatches, prompts, token
usage, applied actions, retries, git commits, role minting, and
workflow lifecycle. This module folds that stream into the same shape
the curses TUI's ``DashboardState`` exposes, so the browser can render
live agent cards, a colour-coded activity timeline, and token/cost
meters without re-deriving anything client-side.

Everything here is pure and side-effect-free (apart from reading the
log file) so it is trivial to unit-test with a synthetic JSONL fixture.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .log import get_logger
from .pricing import estimate_usd
from .utils.text_extract import classify_error, extract_thought

logger = get_logger(__name__)

# ---------------------------------------------------------------------------
# Low-level event iteration (with byte-offset seek for streaming)
# ---------------------------------------------------------------------------


def iter_events(path: Path, start_offset: int = 0) -> Tuple[List[dict], int]:
    """Read JSONL events from ``path`` starting at byte ``start_offset``.

    Returns ``(events, new_offset)``. A trailing partial line (a write
    still in flight) is NOT consumed — ``new_offset`` stops at the start
    of that partial line so the next call re-reads it once complete.
    This makes the function safe to poll for a tail-follow stream.
    """
    if not path.exists():
        return [], start_offset
    try:
        with path.open("rb") as fh:
            fh.seek(start_offset)
            data = fh.read()
    except Exception:
        return [], start_offset

    if not data:
        return [], start_offset

    # Only consume up to the last newline; keep any trailing partial line.
    last_nl = data.rfind(b"\n")
    if last_nl == -1:
        return [], start_offset
    consumable = data[: last_nl + 1]
    new_offset = start_offset + len(consumable)

    events: List[dict] = []
    for raw_line in consumable.split(b"\n"):
        if not raw_line.strip():
            continue
        try:
            obj = json.loads(raw_line.decode("utf-8", errors="replace"))
            if isinstance(obj, dict):
                events.append(obj)
        except Exception as _exc:
            logger.debug("skipping unparseable activity line: %s", _exc)
            continue
    return events, new_offset


# ---------------------------------------------------------------------------
# Event → UI normalisation
# ---------------------------------------------------------------------------

# kind -> (severity, category). Severity drives colour; category drives
# the icon/grouping in the timeline. Unknown kinds fall back to info/event.
_KIND_MAP: Dict[str, Tuple[str, str]] = {
    "agent_dispatch": ("info", "dispatch"),
    "prompt_sent": ("muted", "dispatch"),
    "provider_attempt": ("muted", "provider"),
    "provider_retry": ("warn", "recovery"),
    "agent_quality_retry": ("warn", "recovery"),
    "agent_quality_final": ("warn", "recovery"),
    "workflow_stage_retry": ("warn", "recovery"),
    "ralph_plateau_escalate": ("warn", "recovery"),
    "ralph_regression_detected": ("warn", "recovery"),
    "consensus_started": ("info", "coordination"),
    "consensus_samples_completed": ("info", "coordination"),
    "blackboard_post": ("info", "coordination"),
    "refine_critic_verdict": ("info", "coordination"),
    "meta_prompt_drafted": ("muted", "coordination"),
    "action_applied": ("success", "action"),
    "action_skipped": ("muted", "action"),
    "action_path_normalized": ("muted", "action"),
    "action_error": ("error", "action"),
    "git_commit": ("success", "git"),
    "workflow_written": ("info", "workflow"),
    "workflow_round_complete": ("success", "workflow"),
    "workflow_outputs_unmet": ("warn", "workflow"),
    "workflow_aborted": ("error", "workflow"),
    "subprocess_start": ("muted", "provider"),
    "subprocess_end": ("muted", "provider"),
    "subprocess_timeout": ("error", "provider"),
    "default_agent_created": ("info", "roster"),
    "role_minted": ("info", "roster"),
    "role_refreshed": ("info", "roster"),
}


def _provider_name(describe: Optional[str]) -> str:
    """Extract the provider name from a ``provider.describe()`` string
    like ``"claude (claude)"`` -> ``"claude"``."""
    if not describe:
        return ""
    return str(describe).split(" ", 1)[0].strip()


def _summarize(raw: dict) -> str:
    kind = raw.get("event", "")
    agent = raw.get("agent", "")
    if kind == "agent_dispatch":
        obj = (raw.get("objective") or "").strip().replace("\n", " ")
        return f"{agent} dispatched — {obj[:120]}" if obj else f"{agent} dispatched"
    if kind == "prompt_sent":
        return f"{agent} prompt sent ({raw.get('prompt_chars', 0)} chars)"
    if kind == "agent_response":
        if raw.get("ok"):
            return f"{agent} responded ({raw.get('tokens_total', 0)} tokens)"
        return f"{agent} failed: {(raw.get('error') or 'error')[:120]}"
    if kind == "action_applied":
        return f"{agent} {raw.get('op', 'wrote')} {raw.get('path', '')}".strip()
    if kind == "git_commit":
        return f"commit: {(raw.get('message') or raw.get('subject') or '').strip()[:100]}"
    if kind == "provider_retry":
        return f"{agent} provider retry (attempt {raw.get('attempt', '?')}) — {(raw.get('error') or '')[:80]}"
    if kind == "workflow_round_complete":
        return f"workflow round complete: {raw.get('workflow', '')}"
    if kind == "consensus_started":
        return f"{agent} consensus: {raw.get('samples', '?')} samples"
    if kind == "default_agent_created":
        return f"role ready: {agent} — {raw.get('role', '')}"
    # Generic fallback: kind + a couple of interesting fields.
    extra = raw.get("message") or raw.get("status") or raw.get("path") or ""
    return f"{kind}{(' :: ' + str(extra)) if extra else ''}"


def normalize_event(raw: dict, seq: int) -> dict:
    """Produce a stable UI event shape from a raw activity record."""
    kind = raw.get("event", "event")
    severity, category = _KIND_MAP.get(kind, ("info", "event"))
    # agent_response severity depends on the ok flag.
    if kind == "agent_response":
        severity = "success" if raw.get("ok") else "error"
    return {
        "seq": seq,
        "ts": raw.get("ts", ""),
        "kind": kind,
        "agent": raw.get("agent", ""),
        "run_id": raw.get("run_id", ""),
        "severity": severity,
        "category": category,
        "summary": _summarize(raw),
        "payload": raw,
    }


# ---------------------------------------------------------------------------
# Snapshot folding (mirrors tui.DashboardState)
# ---------------------------------------------------------------------------


def _new_card(name: str) -> Dict[str, Any]:
    return {
        "name": name,
        "status": "idle",
        "role": "",
        "provider": "",
        "runs": 0,
        "tokens_in": 0,
        "tokens_out": 0,
        "tokens_total": 0,
        "last_run_tokens": 0,
        "usd": 0.0,
        "files": [],
        "last_thought": "",
        "last_prompt_path": "",
        "last_response_path": "",
        "last_error": "",
        "error_kind": "",
    }


def build_snapshot(
    events: List[dict],
    *,
    provider_overrides: Optional[Dict[str, Any]] = None,
    idea: str = "",
    active_provider: str = "",
) -> Dict[str, Any]:
    """Fold an ordered list of raw activity events into a dashboard snapshot.

    ``provider_overrides`` is the workspace ``providers.json`` ``providers``
    block (so per-provider pricing overrides apply). ``idea`` and
    ``active_provider`` are passed in by the caller (they come from
    ``.clk/state`` / ``providers.json``, not the activity log).
    """
    overrides = provider_overrides or {}
    cards: Dict[str, Dict[str, Any]] = {}
    files_changed: List[str] = []
    cost_per_provider: Dict[str, float] = {}
    total_tokens = 0
    total_usd = 0.0
    peak_run_tokens = 0
    phase = ""
    commits = 0

    def card(name: str) -> Dict[str, Any]:
        if name not in cards:
            cards[name] = _new_card(name)
        return cards[name]

    for raw in events:
        kind = raw.get("event", "")
        agent = raw.get("agent", "")

        if kind == "agent_dispatch":
            c = card(agent)
            c["status"] = "working"
            c["role"] = raw.get("role") or c["role"]
            c["provider"] = _provider_name(raw.get("provider")) or c["provider"]
            if raw.get("phase"):
                phase = str(raw.get("phase"))
            elif raw.get("workflow"):
                phase = str(raw.get("workflow"))

        elif kind == "prompt_sent":
            c = card(agent)
            c["last_prompt_path"] = raw.get("prompt_path") or c["last_prompt_path"]

        elif kind in ("provider_retry", "agent_quality_retry", "workflow_stage_retry"):
            c = card(agent)
            if c["status"] == "working":
                c["status"] = "recovering"

        elif kind == "agent_response":
            c = card(agent)
            c["runs"] += 1
            tin = int(raw.get("tokens_in") or 0)
            tout = int(raw.get("tokens_out") or 0)
            ttot = int(raw.get("tokens_total") or (tin + tout))
            c["tokens_in"] += tin
            c["tokens_out"] += tout
            c["tokens_total"] += ttot
            c["last_run_tokens"] = ttot
            c["last_response_path"] = raw.get("response_path") or c["last_response_path"]
            thought = extract_thought(raw.get("response_text") or "")
            if thought:
                c["last_thought"] = thought
            prov = c["provider"] or _provider_name(active_provider)
            usd = estimate_usd(prov, None, tin, tout, overrides.get(prov) if prov else None)
            c["usd"] += usd
            total_usd += usd
            total_tokens += ttot
            if prov:
                cost_per_provider[prov] = cost_per_provider.get(prov, 0.0) + usd
            peak_run_tokens = max(peak_run_tokens, ttot)
            for f in (raw.get("files_reported") or []):
                if f not in c["files"]:
                    c["files"].append(f)
                if f not in files_changed:
                    files_changed.append(f)
            if raw.get("ok"):
                c["status"] = "done"
            else:
                err = raw.get("error") or ""
                c["last_error"] = err
                kind_str, _, _ = classify_error(err)
                c["error_kind"] = kind_str
                c["status"] = "provider" if kind_str in (
                    "rate_limit", "timeout", "auth", "policy", "not_installed"
                ) else "failed"

        elif kind == "action_applied":
            path = raw.get("path")
            if path:
                if agent:
                    c = card(agent)
                    if path not in c["files"]:
                        c["files"].append(path)
                if path not in files_changed:
                    files_changed.append(path)

        elif kind == "git_commit":
            commits += 1

        elif kind in ("workflow_written", "workflow_round_complete"):
            if raw.get("workflow"):
                phase = str(raw.get("workflow"))

        elif kind in ("default_agent_created", "role_minted", "role_refreshed"):
            c = card(agent)
            if raw.get("role"):
                c["role"] = raw.get("role")

    # An agent is "in flight" while its card is still working or recovering
    # from a retry. Deriving busy from the final folded statuses (rather than
    # toggling it on each response) stays correct when several agents run
    # concurrently -- the loop is busy until every agent has settled.
    busy = any(c["status"] in ("working", "recovering") for c in cards.values())

    return {
        "idea": idea,
        "provider": active_provider,
        "phase": phase,
        "busy": busy,
        "agents": cards,
        "totals": {
            "total_tokens": total_tokens,
            "total_usd": round(total_usd, 6),
            "peak_run_tokens": peak_run_tokens,
            "total_files": len(files_changed),
            "commits": commits,
            "cost_per_provider": {k: round(v, 6) for k, v in cost_per_provider.items()},
        },
        "files_changed": files_changed,
        "event_count": len(events),
    }


__all__ = ["iter_events", "normalize_event", "build_snapshot"]
