"""Web-UI activity endpoints: history, harness logs, snapshot, SSE stream.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, Dict, List, Optional

from fastapi import Query, Request
from fastapi.responses import StreamingResponse

from .. import web_snapshot
from ..config import (
    load_clk_config,
    load_providers_config,
)
from ..log import get_logger
from .router import _activity_path, _read_idea, _require_workspace, router

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Activity: history + snapshot + SSE stream
# ---------------------------------------------------------------------------

@router.get("/api/workspaces/{workspace_id}/activity")
async def get_activity(
    workspace_id: str,
    offset: int = Query(0, ge=0),
    limit: int = Query(500, ge=1, le=5000),
    kinds: Optional[str] = None,
) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    log_path = _activity_path(paths)
    raw_events, new_offset = web_snapshot.iter_events(log_path, offset)
    wanted = {k.strip() for k in kinds.split(",")} if kinds else None
    out: List[dict] = []
    for i, raw in enumerate(raw_events):
        if wanted and raw.get("event") not in wanted:
            continue
        out.append(web_snapshot.normalize_event(raw, offset + i))
    out = out[:limit]
    return {"ok": True, "events": out, "next_offset": new_offset, "count": len(out)}


@router.get("/api/workspaces/{workspace_id}/logs")
async def get_harness_logs(
    workspace_id: str,
    tail: int = Query(400, ge=1, le=5000),
) -> Dict[str, Any]:
    """Tail the harness session logs (init/idea/run/...).

    These are the human-readable ``.clk/logs/*.log`` files the CLI writes —
    distinct from activity.jsonl. The web Log tab shows them so users can see
    initialization progress and orchestration decisions without a terminal.
    """
    paths = _require_workspace(workspace_id)
    logs_dir = paths.logs
    entries: List[Dict[str, Any]] = []
    # The UI polls this every few seconds, so reads must stay bounded as
    # logs grow: read at most ~120 bytes/line of tail from the end of each
    # file instead of the whole file.
    max_bytes = tail * 120
    if logs_dir.is_dir():
        files = sorted(
            (p for p in logs_dir.glob("*.log") if p.is_file()),
            key=lambda p: p.stat().st_mtime,
        )
        # Read newest files last so the tail keeps the most recent lines.
        for p in files:
            try:
                size = p.stat().st_size
                with p.open("rb") as fh:
                    if size > max_bytes:
                        fh.seek(size - max_bytes)
                        fh.readline()  # drop the partial first line
                    text = fh.read().decode("utf-8", errors="replace")
            except OSError:
                continue
            for line in text.splitlines():
                if line.strip():
                    entries.append({"file": p.name, "line": line})
    if len(entries) > tail:
        entries = entries[-tail:]
    return {"ok": True, "lines": entries, "count": len(entries)}


@router.get("/api/workspaces/{workspace_id}/snapshot")
async def get_snapshot(workspace_id: str) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    log_path = _activity_path(paths)
    raw_events, _ = web_snapshot.iter_events(log_path, 0)
    prov_cfg = load_providers_config(paths)
    snap = web_snapshot.build_snapshot(
        raw_events,
        provider_overrides=(prov_cfg.get("providers") or {}),
        idea=_read_idea(paths),
        active_provider=prov_cfg.get("active") or load_clk_config(paths).get("default_provider") or "",
    )
    return {"ok": True, "snapshot": snap}


@router.get("/api/workspaces/{workspace_id}/activity/stream")
async def stream_activity(
    workspace_id: str,
    request: Request,
    from_: str = Query("end", alias="from"),
) -> StreamingResponse:
    paths = _require_workspace(workspace_id)
    log_path = _activity_path(paths)

    async def _generate():
        # from=start replays the whole log then follows; from=end follows
        # only new events (default).
        if from_ == "start":
            offset = 0
        else:
            _, offset = web_snapshot.iter_events(log_path, 0)
        seq = 0
        idle_ticks = 0
        while True:
            if await request.is_disconnected():
                break
            events, new_offset = web_snapshot.iter_events(log_path, offset)
            offset = new_offset
            if events:
                idle_ticks = 0
                for raw in events:
                    payload = json.dumps(web_snapshot.normalize_event(raw, seq))
                    seq += 1
                    yield f"data: {payload}\n\n"
            else:
                idle_ticks += 1
                if idle_ticks % 30 == 0:  # ~every 9s, keep proxies alive
                    yield ": keepalive\n\n"
            await asyncio.sleep(0.3)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no", "Connection": "keep-alive"},
    )
