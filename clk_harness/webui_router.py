"""Web-UI REST surface: config, .env, providers/doctor, and the live
activity stream + snapshot.

This is a :class:`fastapi.APIRouter` that :mod:`clk_harness.api` includes,
keeping ``api.py`` itself small. Everything reuses the helpers and the
``{ok, ...}`` / ``{ok:false, error}`` envelope already defined there.

Scope notes
-----------
* ``clk.config.json`` / ``providers.json`` / ``agents.json`` are edited
  **per workspace** — those are the files the ``clk`` subprocess reads at
  ``cwd=ws_path``.
* ``.env`` is a single **global** file (``CLK_ENV_FILE`` or repo-root
  ``.env``). The API injects it into each agent subprocess's environment
  (see ``api._run_task``) so edits take effect on the next run without a
  server restart. Secret values are masked on read and preserved on write
  via the :data:`clk_harness.env_file.MASK_SENTINEL` sentinel.
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from . import env_file
from . import web_snapshot
from .config import (
    Paths,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    save_agents_config,
    save_json,
    save_providers_config,
)
from .providers import available_providers

router = APIRouter()


# ---------------------------------------------------------------------------
# Helpers (import lazily from api to avoid a circular import at module load)
# ---------------------------------------------------------------------------

def _api():
    from . import api  # local import: api imports this module
    return api


def _ws_paths(workspace_id: str) -> Paths:
    api = _api()
    ws_path = api._workspace_path(workspace_id)
    return Paths(root=ws_path)


def _require_workspace(workspace_id: str) -> Paths:
    api = _api()
    if workspace_id not in api.WORKSPACES and not api._workspace_path(workspace_id).exists():
        raise api._err("workspace_not_found", f"Workspace {workspace_id!r} not found.", 404)
    return _ws_paths(workspace_id)


def _activity_path(paths: Paths) -> Path:
    """Resolve ``<ws>/.clk/logs/activity.jsonl`` with a traversal guard."""
    target = (paths.logs / "activity.jsonl").resolve()
    root = paths.root.resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise _api()._err("forbidden", "Path escapes workspace boundary.", 403)
    return target


# Directories that are harness internals / noise -- never listed as "files the
# agents generated for you", and never writable through the file editor.
_HIDDEN_DIRS = {".clk", ".git", "node_modules", "__pycache__", ".venv", ".mypy_cache", ".pytest_cache"}
_MAX_FILES = 3000
_MAX_FILE_BYTES = 1_000_000  # 1 MB read/write cap for the in-browser editor


def _safe_ws_file(paths: Paths, rel: str, *, for_write: bool = False) -> Path:
    """Resolve ``<ws>/<rel>`` with a traversal guard (and a hidden-dir guard
    for writes), raising the standard 403/400 envelope on violation."""
    api = _api()
    if not rel or rel.strip() == "":
        raise api._err("invalid_path", "A file path is required.", 400)
    root = paths.root.resolve()
    target = (root / rel).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        raise api._err("forbidden", "Path escapes workspace boundary.", 403)
    parts = set(target.relative_to(root).parts)
    if for_write and parts & _HIDDEN_DIRS:
        raise api._err("forbidden", "Cannot write to a harness-internal path.", 403)
    return target


def _is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data


_SECRET_PROVIDER_FIELDS = ("api_key", "apikey", "token", "secret", "password")


def _mask_provider_block(providers: Dict[str, Any]) -> Dict[str, Any]:
    """Return a deep-ish copy of a providers map with secret-looking
    fields masked."""
    masked: Dict[str, Any] = {}
    for name, block in (providers or {}).items():
        if not isinstance(block, dict):
            masked[name] = block
            continue
        copy = dict(block)
        for fld in list(copy.keys()):
            if any(s in fld.lower() for s in _SECRET_PROVIDER_FIELDS) and copy[fld]:
                copy[fld] = env_file.MASK_SENTINEL
        masked[name] = copy
    return masked


def _unmask_provider_block(incoming: Dict[str, Any], existing: Dict[str, Any]) -> Dict[str, Any]:
    """Replace any masked secret field in ``incoming`` with the stored
    value from ``existing`` so a round-trip never clobbers a real secret."""
    out: Dict[str, Any] = {}
    for name, block in (incoming or {}).items():
        if not isinstance(block, dict):
            out[name] = block
            continue
        copy = dict(block)
        prev = (existing or {}).get(name) or {}
        for fld, val in list(copy.items()):
            if val == env_file.MASK_SENTINEL:
                copy[fld] = prev.get(fld, "")
        out[name] = copy
    return out


def _read_idea(paths: Paths) -> str:
    idea_path = paths.state / "idea.json"
    if not idea_path.exists():
        return ""
    try:
        data = json.loads(idea_path.read_text(encoding="utf-8"))
        return str(data.get("title") or data.get("idea") or data.get("text") or "")
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class ClkConfigUpdate(BaseModel):
    config: Dict[str, Any]


class ProvidersUpdate(BaseModel):
    providers: Dict[str, Any]
    active: Optional[str] = None


class AgentsUpdate(BaseModel):
    agents: Dict[str, Any]


class FileWrite(BaseModel):
    path: str
    content: str


class IdeaUpdate(BaseModel):
    statement: str
    title: Optional[str] = None
    tags: List[str] = Field(default_factory=list)


class EnvUpdate(BaseModel):
    # value == MASK_SENTINEL -> leave unchanged; null -> blank; else set.
    values: Dict[str, Optional[str]] = Field(default_factory=dict)
    removals: List[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Per-workspace config
# ---------------------------------------------------------------------------

@router.get("/api/workspaces/{workspace_id}/config/clk")
async def get_clk_config(workspace_id: str) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    return {"ok": True, "config": load_clk_config(paths)}


@router.put("/api/workspaces/{workspace_id}/config/clk")
async def put_clk_config(workspace_id: str, body: ClkConfigUpdate) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    paths.ensure()
    save_json(paths.config / "clk.config.json", body.config)
    return {"ok": True, "config": load_clk_config(paths)}


@router.get("/api/workspaces/{workspace_id}/config/providers")
async def get_providers_config(workspace_id: str) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    cfg = load_providers_config(paths)
    return {
        "ok": True,
        "active": cfg.get("active"),
        "providers": _mask_provider_block(cfg.get("providers") or {}),
        "available": available_providers(cfg),
    }


@router.put("/api/workspaces/{workspace_id}/config/providers")
async def put_providers_config(workspace_id: str, body: ProvidersUpdate) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    paths.ensure()
    existing = load_providers_config(paths)
    merged = dict(existing)
    merged["providers"] = _unmask_provider_block(
        body.providers, existing.get("providers") or {}
    )
    if body.active is not None:
        merged["active"] = body.active
    save_providers_config(paths, merged)
    cfg = load_providers_config(paths)
    return {
        "ok": True,
        "active": cfg.get("active"),
        "providers": _mask_provider_block(cfg.get("providers") or {}),
        "available": available_providers(cfg),
    }


@router.get("/api/workspaces/{workspace_id}/config/agents")
async def get_agents_config(workspace_id: str) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    return {"ok": True, "agents": (load_agents_config(paths).get("agents") or {})}


@router.put("/api/workspaces/{workspace_id}/config/agents")
async def put_agents_config(workspace_id: str, body: AgentsUpdate) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    paths.ensure()
    save_agents_config(paths, {"agents": body.agents})
    return {"ok": True, "agents": (load_agents_config(paths).get("agents") or {})}


@router.get("/api/workspaces/{workspace_id}/doctor")
async def workspace_doctor(workspace_id: str) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    clk_cfg = load_clk_config(paths)
    prov_cfg = load_providers_config(paths)
    auth_mode = (clk_cfg.get("auth_mode") or "cli").lower()
    avail = available_providers(prov_cfg)
    active = prov_cfg.get("active") or clk_cfg.get("default_provider") or "shell"
    findings: List[Dict[str, str]] = []
    # The most common "it runs but does nothing" trap: the active provider is
    # the shell stub (echoes prompts, never calls an LLM), or it points at a
    # name with no config block (which silently degrades to shell at runtime).
    if active == "shell":
        findings.append({
            "level": "warn", "name": "active_provider",
            "message": "active provider is 'shell' — a stub that echoes prompts and never calls an LLM. Pick a real provider on this tab.",
        })
    elif active not in (prov_cfg.get("providers") or {}):
        findings.append({
            "level": "fail", "name": "active_provider",
            "message": f"active provider '{active}' has no config block — runs will silently fall back to the shell stub.",
        })
    for name, ok in sorted(avail.items()):
        if ok:
            findings.append({"level": "ok", "name": name, "message": "available"})
        else:
            findings.append({
                "level": "fail" if name == active else "warn",
                "name": name, "message": "unavailable",
            })
    env = env_file.read_env()
    if active == "claude" and auth_mode == "apikey" and not (env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY")):
        findings.append({"level": "fail", "name": "anthropic_key", "message": "auth=apikey but ANTHROPIC_API_KEY unset"})
    if active == "codex" and auth_mode == "apikey" and not (env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")):
        findings.append({"level": "fail", "name": "openai_key", "message": "auth=apikey but OPENAI_API_KEY unset"})
    if active == "gemini" and auth_mode == "apikey" and not any(
        env.get(k) or os.environ.get(k) for k in ("GEMINI_API_KEY", "GOOGLE_API_KEY")
    ):
        findings.append({"level": "fail", "name": "gemini_key", "message": "auth=apikey but GEMINI/GOOGLE key unset"})
    return {"ok": True, "active_provider": active, "auth_mode": auth_mode, "findings": findings}


# ---------------------------------------------------------------------------
# Global .env
# ---------------------------------------------------------------------------

@router.get("/api/env")
async def get_env() -> Dict[str, Any]:
    variables, groups = env_file.describe_env(reveal=False)
    return {"ok": True, "path": str(env_file.env_path()), "groups": groups, "vars": variables}


@router.get("/api/env/schema")
async def get_env_schema() -> Dict[str, Any]:
    groups: Dict[str, List[dict]] = {}
    for v in env_file.ENV_SCHEMA:
        groups.setdefault(v.group, []).append({
            "key": v.key, "label": v.label, "type": v.type,
            "choices": v.choices, "default": v.default, "help": v.help,
            "is_secret": v.is_secret,
        })
    ordered = [{"name": g, "vars": groups[g]} for g in env_file.GROUP_ORDER if g in groups]
    return {"ok": True, "groups": ordered}


@router.put("/api/env")
async def put_env(body: EnvUpdate) -> Dict[str, Any]:
    env_file.write_env(body.values, removals=set(body.removals or []))
    variables, groups = env_file.describe_env(reveal=False)
    return {"ok": True, "path": str(env_file.env_path()), "groups": groups, "vars": variables}


@router.get("/api/env/reveal/{key}")
async def reveal_env(key: str) -> Dict[str, Any]:
    """Explicitly reveal a single secret value. Disabled unless
    ``CLK_API_ALLOW_REVEAL`` is truthy (defence-in-depth for the
    masked-by-default contract)."""
    allow = (os.environ.get("CLK_API_ALLOW_REVEAL") or "").strip().lower() in ("1", "true", "yes", "on")
    if not allow:
        raise _api()._err("reveal_disabled", "Secret reveal is disabled. Set CLK_API_ALLOW_REVEAL=1.", 403)
    return {"ok": True, "key": key, "value": env_file.read_env().get(key, "")}


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
    seq = offset  # use byte offset as a coarse monotonic base for history
    for i, raw in enumerate(raw_events):
        if wanted and raw.get("event") not in wanted:
            continue
        out.append(web_snapshot.normalize_event(raw, offset + i))
    out = out[:limit]
    return {"ok": True, "events": out, "next_offset": new_offset, "count": len(out)}


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


# ---------------------------------------------------------------------------
# Workspace files: list / read / write + the "follow up with the agents" idea
# ---------------------------------------------------------------------------

@router.get("/api/workspaces/{workspace_id}/files")
async def list_files(workspace_id: str) -> Dict[str, Any]:
    """List the files the agents have produced in the workspace.

    Harness-internal directories (``.clk``, ``.git``, ``node_modules`` …) are
    skipped so this reflects the user-facing deliverables, not bookkeeping.
    """
    from datetime import datetime, timezone

    paths = _require_workspace(workspace_id)
    root = paths.root.resolve()
    files: List[Dict[str, Any]] = []
    truncated = False
    if root.exists():
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune hidden/internal dirs in place so os.walk doesn't descend.
            dirnames[:] = sorted(d for d in dirnames if d not in _HIDDEN_DIRS)
            for name in sorted(filenames):
                if len(files) >= _MAX_FILES:
                    truncated = True
                    break
                fp = Path(dirpath) / name
                try:
                    stat = fp.stat()
                except OSError:
                    continue
                files.append({
                    "path": str(fp.relative_to(root)),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                })
            if truncated:
                break
    files.sort(key=lambda f: f["path"])
    return {"ok": True, "files": files, "count": len(files), "truncated": truncated}


@router.get("/api/workspaces/{workspace_id}/file")
async def read_file(workspace_id: str, path: str = Query(...)) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    target = _safe_ws_file(paths, path)
    if not target.exists() or not target.is_file():
        raise _api()._err("file_not_found", f"File {path!r} not found.", 404)
    raw = target.read_bytes()
    too_big = len(raw) > _MAX_FILE_BYTES
    chunk = raw[:_MAX_FILE_BYTES]
    if _is_probably_binary(chunk):
        return {"ok": True, "path": path, "binary": True, "size": len(raw)}
    return {
        "ok": True,
        "path": path,
        "binary": False,
        "size": len(raw),
        "truncated": too_big,
        "content": chunk.decode("utf-8", errors="replace"),
    }


@router.put("/api/workspaces/{workspace_id}/file")
async def write_file(workspace_id: str, body: FileWrite) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    target = _safe_ws_file(paths, body.path, for_write=True)
    data = body.content.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise _api()._err("too_large", "File exceeds the 1 MB editor limit.", 413)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": body.path, "size": len(data)}


@router.put("/api/workspaces/{workspace_id}/idea")
async def set_idea(workspace_id: str, body: IdeaUpdate) -> Dict[str, Any]:
    """Seed the workspace idea/brief used by the next ``run``.

    Mirrors ``clk idea`` (idea.json + system_brief.md) but without the chief
    casting pass, so the Files-view chat can attach context and immediately
    kick off a workflow.
    """
    import textwrap
    from datetime import datetime

    paths = _require_workspace(workspace_id)
    paths.ensure()
    statement = body.statement.strip()
    if not statement:
        raise _api()._err("invalid_idea", "An idea statement is required.", 400)
    title = (body.title or statement.split(".")[0])[:80]
    captured_at = datetime.now().isoformat(timespec="seconds")
    save_json(paths.state / "idea.json", {
        "title": title,
        "statement": statement,
        "captured_at": captured_at,
        "tags": body.tags or [],
    })
    brief = textwrap.dedent(
        f"""\
        # System brief

        **Title:** {title}

        ## Idea
        {statement}

        ## Captured at
        {captured_at}
        """
    )
    (paths.state / "system_brief.md").write_text(brief, encoding="utf-8")
    return {"ok": True, "title": title}


@router.get("/api/providers")
async def list_providers(workspace: Optional[str] = None) -> Dict[str, Any]:
    if workspace:
        paths = _require_workspace(workspace)
    else:
        from .config import project_paths
        paths = project_paths()
    cfg = load_providers_config(paths)
    return {"ok": True, "active": cfg.get("active"), "available": available_providers(cfg)}


class ProbeRequest(BaseModel):
    type: str
    endpoint: Optional[str] = None
    api_key: Optional[str] = None


@router.post("/api/providers/probe")
async def probe_provider(body: ProbeRequest) -> Dict[str, Any]:
    """Probe an HTTP provider endpoint and return its available models.

    Used by the Providers form to offer a model dropdown. For provider
    types that don't expose an HTTP model list (claude/codex/gemini/pi/
    shell) ``supported`` is False so the UI keeps a free-text box. Never
    raises on a bad endpoint — returns ``reachable: false`` instead.
    """
    ptype = (body.type or "").lower()
    endpoint = (body.endpoint or "").strip()
    if ptype == "ollama":
        from .providers.ollama import list_models as _ollama_models
        from .providers._endpoint_fallback import probe_endpoint, docker_host_swap
        ep = endpoint or "http://localhost:11434"
        models = _ollama_models(ep)
        reachable = bool(models) or probe_endpoint(ep) or (
            bool(docker_host_swap(ep)) and probe_endpoint(docker_host_swap(ep) or "")
        )
        return {"ok": True, "supported": True, "reachable": reachable, "models": models}
    if ptype == "openwebui":
        from .providers.openwebui import list_models as _owui_models
        from .providers._endpoint_fallback import probe_endpoint, docker_host_swap
        ep = endpoint or "http://localhost:8080"
        models = _owui_models(ep, body.api_key or "")
        reachable = bool(models) or probe_endpoint(ep) or (
            bool(docker_host_swap(ep)) and probe_endpoint(docker_host_swap(ep) or "")
        )
        return {"ok": True, "supported": True, "reachable": reachable, "models": models}
    return {"ok": True, "supported": False, "reachable": None, "models": []}


__all__ = ["router"]
