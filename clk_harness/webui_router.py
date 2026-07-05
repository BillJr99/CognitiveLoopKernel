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
import copy
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel, Field

from . import env_file, web_snapshot
from .config import (
    Paths,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    save_agents_config,
    save_json,
    save_providers_config,
)
from .log import get_logger
from .providers import available_providers

logger = get_logger(__name__)

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
    """Resolve ``<ws>/<rel>`` with a traversal guard and a harness-internal
    (``.clk``/``.git``/…) guard, raising the standard 403/400 envelope on
    violation. The hidden-dir guard applies to reads and writes alike so
    internal logs/state can't be fetched through the file endpoints.
    """
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
    if parts & _HIDDEN_DIRS:
        verb = "write to" if for_write else "read from"
        raise api._err("forbidden", f"Cannot {verb} harness-internal paths.", 403)
    return target


def _is_probably_binary(data: bytes) -> bool:
    return b"\x00" in data


def _safe_unlink(path: str) -> None:
    """Best-effort temp-file removal that never raises.

    Used for download-zip cleanup both on the build-failure path and as the
    response ``BackgroundTask``; swallows ``OSError`` so a TOCTOU race (file
    already gone) or a permission hiccup can't surface a noisy exception
    during response finalization.
    """
    try:
        os.remove(path)
    except OSError:
        pass


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
    """Merge ``incoming`` over ``existing``, restoring masked secrets.

    Starts from ``existing`` so provider blocks the caller didn't include
    are preserved (dropping them would leave ``active`` pointing at a
    missing block, which silently degrades the run to the shell stub).
    Masked secret fields fall back to the stored value so a round-trip
    never clobbers a real secret.
    """
    out: Dict[str, Any] = dict(existing or {})
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
    # Always surface the full set of built-in providers (merged under any saved
    # overrides) so the UI can always show every provider card — otherwise a
    # sparse/empty providers.json leaves no "make active" buttons to click.
    from .config import DEFAULT_PROVIDERS
    merged_blocks: Dict[str, Any] = dict(DEFAULT_PROVIDERS.get("providers") or {})
    for name, block in (cfg.get("providers") or {}).items():
        merged_blocks[name] = block
    # available_providers() does blocking network probes for HTTP providers;
    # run it off the event loop so the Providers tab never stalls the server.
    available = await asyncio.to_thread(available_providers, {"providers": merged_blocks})
    return {
        "ok": True,
        "active": cfg.get("active"),
        "providers": _mask_provider_block(merged_blocks),
        "available": available,
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
    env = env_file.read_env()
    # CLK_PROVIDER in the global .env overrides the workspace 'active' at
    # runtime (see AgentRunner.get_provider), so the doctor must reflect it --
    # otherwise it would report 'shell' even when .env makes runs use Ollama.
    env_provider = (env.get("CLK_PROVIDER") or os.environ.get("CLK_PROVIDER") or "").strip()
    active = env_provider or prov_cfg.get("active") or clk_cfg.get("default_provider") or "shell"
    from .config import DEFAULT_PROVIDERS
    # Probe the saved providers *plus* the resolved active provider's block.
    # available_providers() only probes blocks present in providers.json, but
    # the active provider can be an env/default-selected built-in with no saved
    # block (e.g. CLK_PROVIDER=ollama on a workspace whose providers.json only
    # has 'shell'). Without seeding the default block here, that provider would
    # never get an availability finding and could never be marked 'fail'.
    probe_blocks: Dict[str, Any] = dict(prov_cfg.get("providers") or {})
    if active != "shell" and active not in probe_blocks:
        default_block = (DEFAULT_PROVIDERS.get("providers") or {}).get(active)
        if default_block is not None:
            # Deep-copy so a probe that rewrites 'endpoint' in place
            # (ollama/openwebui docker-host fallback) can't mutate the shared
            # module-global DEFAULT_PROVIDERS across requests.
            probe_blocks[active] = copy.deepcopy(default_block)
    # available_providers() does blocking TCP/network probes; run it off the
    # event loop (as the Providers tab does) so the doctor endpoint can't stall
    # the server while a probe waits on its socket timeout.
    avail = await asyncio.to_thread(available_providers, {"providers": probe_blocks})
    findings: List[Dict[str, str]] = []
    # The most common "it runs but does nothing" trap: the active provider is
    # the shell stub (echoes prompts, never calls an LLM), or it points at a
    # name with no config block (which silently degrades to shell at runtime).
    if active == "shell":
        findings.append({
            "level": "warn", "name": "active_provider",
            "message": (
                "active provider is 'shell' — a stub that echoes prompts and never calls an LLM. "
                "Pick a real provider on this tab."
            ),
        })
    else:
        known = set(prov_cfg.get("providers") or {}) | set(DEFAULT_PROVIDERS.get("providers") or {})
        if active not in known:
            findings.append({
                "level": "fail", "name": "active_provider",
                "message": (
                    f"active provider '{active}' has no config block — runs will silently fall back to the shell stub."
                ),
            })
    for name, ok in sorted(avail.items()):
        if ok:
            findings.append({"level": "ok", "name": name, "message": "available"})
        else:
            findings.append({
                "level": "fail" if name == active else "warn",
                "name": name, "message": "unavailable",
            })
    if (
        active == "claude"
        and auth_mode == "apikey"
        and not (env.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_API_KEY"))
    ):
        findings.append(
            {"level": "fail", "name": "anthropic_key", "message": "auth=apikey but ANTHROPIC_API_KEY unset"}
        )
    if (
        active == "codex"
        and auth_mode == "apikey"
        and not (env.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY"))
    ):
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
                # Skip symlinks: following them with stat() could leak
                # size/mtime for targets outside the workspace.
                if fp.is_symlink():
                    continue
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


@router.get("/api/workspaces/{workspace_id}/download")
async def download_workspace(workspace_id: str) -> FileResponse:
    """Download the workspace's deliverables as a zip.

    Mirrors the file listing: harness-internal directories (``.clk``,
    ``.git``, ``node_modules`` …) and symlinks are excluded so the archive
    is just the user-facing files the agents produced.
    """
    paths = _require_workspace(workspace_id)
    root = paths.root.resolve()

    def _build_zip_to_tempfile() -> str:
        # Build to a temp file on disk so we stream it back in chunks rather
        # than holding the whole archive in memory.
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="clk-ws-")
        os.close(fd)
        # If zipping fails (e.g. a permission error while walking/writing) the
        # FileResponse -- and its cleanup BackgroundTask -- is never created, so
        # remove the temp file here before re-raising to avoid leaking it.
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if root.exists():
                    for dirpath, dirnames, filenames in os.walk(root):
                        dirnames[:] = [d for d in dirnames if d not in _HIDDEN_DIRS]
                        for name in filenames:
                            fp = Path(dirpath) / name
                            if fp.is_symlink():
                                continue
                            try:
                                zf.write(fp, fp.relative_to(root).as_posix())
                            except OSError:
                                continue
        except BaseException:
            _safe_unlink(tmp_path)
            raise
        return tmp_path

    tmp_path = await asyncio.to_thread(_build_zip_to_tempfile)
    entry = _api().WORKSPACES.get(workspace_id) or {}
    raw_name = str(entry.get("name") or workspace_id[:8])
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-") or "workspace"
    from starlette.background import BackgroundTask
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{safe}.zip",
        background=BackgroundTask(lambda: _safe_unlink(tmp_path)),
    )


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
    if body.path.endswith(("/", "\\")) or (target.exists() and target.is_dir()):
        raise _api()._err("invalid_path", f"{body.path!r} is a directory, not a file.", 400)
    data = body.content.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise _api()._err("too_large", "File exceeds the 1 MB editor limit.", 413)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": body.path, "size": len(data)}


# ---------------------------------------------------------------------------
# Git history: the Files tab's "how did these files evolve" view
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _require_sha(sha: str) -> str:
    if not _SHA_RE.match(sha or ""):
        raise _api()._err("invalid_sha", "A hex commit sha is required.", 400)
    return sha


@router.get("/api/workspaces/{workspace_id}/git/log")
async def git_log(
    workspace_id: str,
    path: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """Commit history (newest first) for the workspace, or for one file.

    Harness-internal paths (.clk, .git, …) are filtered out so the
    history mirrors what the Files tab lists.
    """
    from . import git_ops

    paths = _require_workspace(workspace_id)
    if path:
        _safe_ws_file(paths, path)  # traversal + hidden-dir guard
    commits = await asyncio.to_thread(
        git_ops.log_entries, paths.root, path=path, limit=limit
    )
    return {"ok": True, "commits": commits, "count": len(commits)}


@router.get("/api/workspaces/{workspace_id}/git/commit/{sha}")
async def git_commit_detail(workspace_id: str, sha: str) -> Dict[str, Any]:
    """One commit's metadata + unified diff (internal paths excluded)."""
    from . import git_ops

    paths = _require_workspace(workspace_id)
    _require_sha(sha)
    commits = await asyncio.to_thread(
        git_ops.log_entries, paths.root, limit=1, rev=sha
    )
    meta = commits[0] if commits else None
    patch = await asyncio.to_thread(git_ops.commit_patch, paths.root, sha)
    if meta is None and patch is None:
        raise _api()._err("commit_not_found", f"Commit {sha!r} not found.", 404)
    return {
        "ok": True,
        "commit": meta,
        "patch": (patch or {}).get("patch", ""),
        "patch_truncated": bool((patch or {}).get("truncated")),
    }


@router.get("/api/workspaces/{workspace_id}/git/status")
async def git_status(workspace_id: str) -> Dict[str, Any]:
    """Uncommitted working-tree changes vs HEAD (the Files tab's
    "not yet committed" view)."""
    from . import git_ops

    paths = _require_workspace(workspace_id)
    files = await asyncio.to_thread(git_ops.status_entries, paths.root)
    return {"ok": True, "dirty": bool(files), "files": files, "count": len(files)}


@router.get("/api/workspaces/{workspace_id}/git/diff")
async def git_working_diff(workspace_id: str) -> Dict[str, Any]:
    """Unified diff of uncommitted changes vs HEAD. Untracked files don't
    appear in the patch — pair with /git/status to list them."""
    from . import git_ops

    paths = _require_workspace(workspace_id)
    patch = await asyncio.to_thread(git_ops.working_tree_patch, paths.root)
    return {
        "ok": True,
        "patch": (patch or {}).get("patch", ""),
        "truncated": bool((patch or {}).get("truncated")),
    }


@router.get("/api/workspaces/{workspace_id}/git/file")
async def git_file_at(
    workspace_id: str, sha: str = Query(...), path: str = Query(...)
) -> Dict[str, Any]:
    """File content as of a specific commit (read-only time travel)."""
    from . import git_ops

    paths = _require_workspace(workspace_id)
    _require_sha(sha)
    _safe_ws_file(paths, path)  # traversal + hidden-dir guard
    raw = await asyncio.to_thread(git_ops.file_at, paths.root, sha, path)
    if raw is None:
        raise _api()._err(
            "file_not_found", f"File {path!r} not found at commit {sha[:12]}.", 404
        )
    too_big = len(raw) > _MAX_FILE_BYTES
    chunk = raw[:_MAX_FILE_BYTES]
    if _is_probably_binary(chunk):
        return {"ok": True, "path": path, "sha": sha, "binary": True, "size": len(raw)}
    return {
        "ok": True,
        "path": path,
        "sha": sha,
        "binary": False,
        "size": len(raw),
        "truncated": too_big,
        "content": chunk.decode("utf-8", errors="replace"),
    }


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


def _probe_blocking(ptype: str, endpoint: str, api_key: str) -> Dict[str, Any]:
    """Synchronous probe worker (runs off the event loop via to_thread)."""
    from .providers._endpoint_fallback import (
        docker_host_swap,
        normalize_endpoint,
        probe_endpoint,
    )
    _list: Callable[[str], list]
    if ptype == "ollama":
        from .providers.ollama import list_models as _ollama_models
        ep = normalize_endpoint(endpoint) or "http://localhost:11434"
        _list = _ollama_models
    elif ptype == "openwebui":
        from .providers.openwebui import list_models as _owui_models
        ep = normalize_endpoint(endpoint) or "http://localhost:8080"
        _list = lambda e: _owui_models(e, api_key)  # noqa: E731
    else:
        return {"ok": True, "supported": False, "reachable": None, "models": [], "endpoint": None}
    # Probe each candidate explicitly — the configured endpoint first, then
    # the docker-host swap — and report the endpoint that actually answered
    # so callers persist a URL that works. list_models() is only called on
    # TCP-reachable candidates; this matters because ollama's list_models
    # has its own silent internal fallback, which would return models found
    # at the swap while we attribute them to the dead original endpoint.
    candidates = [ep]
    swap = docker_host_swap(ep)
    if swap and swap != ep:
        candidates.append(swap)
    models: list = []
    resolved = ep
    reachable = False
    for cand in candidates:
        if not probe_endpoint(cand):
            continue
        if not reachable:
            reachable = True
            resolved = cand
        found = _list(cand)
        if found:
            models, resolved = found, cand
            break
    return {
        "ok": True, "supported": True, "reachable": reachable,
        "models": models, "endpoint": resolved,
    }


# ---------------------------------------------------------------------------
# Provider discovery (guided mode)
# ---------------------------------------------------------------------------

# Which env var unlocks each key-capable provider (gemini accepts either).
_DISCOVER_KEY_ENVS: Dict[str, List[str]] = {
    "claude": ["ANTHROPIC_API_KEY"],
    "codex": ["OPENAI_API_KEY"],
    "gemini": ["GEMINI_API_KEY", "GOOGLE_API_KEY"],
}

_DISCOVER_LABELS: Dict[str, str] = {
    "claude": "Claude",
    "codex": "OpenAI Codex",
    "gemini": "Google Gemini",
    "pi": "Pi",
    "ollama": "Ollama",
    "openwebui": "OpenWebUI",
}


def _discover_blocking() -> List[Dict[str, Any]]:
    """Probe every built-in provider (except the shell stub) in one pass.

    Workspace-independent: overlays the global project providers.json (if
    any) and the global .env on top of the built-in defaults, then checks
    each provider the same way its runtime ``available()`` would — CLI on
    PATH / API key present for CLI providers, endpoint probe with docker
    host fallback (and model listing) for HTTP providers.
    """
    import shutil
    from concurrent.futures import ThreadPoolExecutor

    from .config import DEFAULT_PROVIDERS, project_paths
    from .providers import load_provider

    blocks: Dict[str, Any] = copy.deepcopy(DEFAULT_PROVIDERS.get("providers") or {})
    try:
        saved = load_providers_config(project_paths()).get("providers") or {}
        for name, block in saved.items():
            if name in blocks and isinstance(block, dict):
                blocks[name] = {**blocks[name], **block}
    except Exception as _exc:
        logger.debug("providers: could not merge saved config: %s", _exc)
    env: Dict[str, str] = {}
    try:
        env = env_file.read_env()
    except Exception as _exc:
        logger.debug("providers: could not read .env: %s", _exc)

    def _env(*keys: str) -> str:
        for k in keys:
            v = (env.get(k) or os.environ.get(k) or "").strip()
            if v:
                return v
        return ""

    # Env overrides mirror the runtime precedence (ollama.py / openwebui.py
    # prefer env vars over the config block).
    for var, name, field in (
        ("CLK_OLLAMA_ENDPOINT", "ollama", "endpoint"),
        ("CLK_OLLAMA_MODEL", "ollama", "model"),
        ("CLK_OPENWEBUI_ENDPOINT", "openwebui", "endpoint"),
        ("CLK_OPENWEBUI_MODEL", "openwebui", "model"),
        ("CLK_OPENWEBUI_API_KEY", "openwebui", "api_key"),
    ):
        if _env(var):
            blocks.setdefault(name, {})[field] = _env(var)

    def _http_entry(name: str) -> Dict[str, Any]:
        block = blocks.get(name) or {}
        probe = _probe_blocking(name, block.get("endpoint") or "", block.get("api_key") or "")
        models = probe.get("models") or []
        reachable = bool(probe.get("reachable"))
        return {
            "name": name,
            "type": name,
            "kind": "http",
            "label": _DISCOVER_LABELS.get(name, name),
            "available": reachable,
            "endpoint": probe.get("endpoint") or block.get("endpoint"),
            "models": models,
            # OpenWebUI can answer the TCP probe yet refuse the model list
            # until a key is supplied; surface that so the wizard asks.
            "needs_api_key": name == "openwebui" and reachable and not models,
            "api_key_env": None,
            "mode": None,
        }

    def _cli_entry(name: str) -> Dict[str, Any]:
        block = blocks.get(name) or {}
        cli_found = shutil.which(block.get("command") or name) is not None
        key_envs = _DISCOVER_KEY_ENVS.get(name, [])
        key_set = bool(_env(*key_envs)) if key_envs else False
        mode = "cli" if cli_found else ("api" if key_set else None)
        return {
            "name": name,
            "type": name,
            "kind": "cli",
            "label": _DISCOVER_LABELS.get(name, name),
            "available": cli_found or key_set,
            "endpoint": None,
            "models": [],
            "needs_api_key": not cli_found and not key_set and bool(key_envs),
            "api_key_env": key_envs[0] if key_envs else None,
            "cli_found": cli_found,
            "key_set": key_set,
            "mode": mode,
        }

    def _pi_entry() -> Dict[str, Any]:
        block = blocks.get("pi") or {}
        try:
            ok = load_provider("pi", block).available()
        except Exception:
            ok = False
        return {
            "name": "pi",
            "type": "pi",
            "kind": "cli",
            "label": _DISCOVER_LABELS["pi"],
            "available": ok,
            "endpoint": None,
            "models": [],
            "needs_api_key": False,
            "api_key_env": None,
            "cli_found": ok,
            "key_set": bool(block.get("api_key")),
            "mode": None,
        }

    # The two HTTP probes each block on socket timeouts (~1s worst case);
    # run them concurrently so discovery stays snappy.
    with ThreadPoolExecutor(max_workers=2) as pool:
        ollama_f = pool.submit(_http_entry, "ollama")
        owui_f = pool.submit(_http_entry, "openwebui")
        cli_entries = [_cli_entry(n) for n in ("claude", "codex", "gemini")]
        pi_entry = _pi_entry()
        http_entries = [ollama_f.result(), owui_f.result()]

    providers = http_entries + cli_entries + [pi_entry]
    # Available providers first so the wizard's menu leads with what works.
    providers.sort(key=lambda p: (not p["available"], p["name"]))
    return providers


@router.get("/api/providers/discover")
async def discover_providers() -> Dict[str, Any]:
    """Scan for usable providers (guided-mode setup).

    Checks local HTTP servers (Ollama/OpenWebUI, with the docker-host
    fallback) and preconfigured CLI providers (binary on PATH or API key
    in the global ``.env``). The ``shell`` stub is intentionally omitted —
    it never calls an LLM and is exactly the trap guided mode exists to
    avoid. Blocking probes run off the event loop.
    """
    providers = await asyncio.to_thread(_discover_blocking)
    return {"ok": True, "providers": providers}


@router.post("/api/providers/probe")
async def probe_provider(body: ProbeRequest) -> Dict[str, Any]:
    """Probe an HTTP provider endpoint and return its available models.

    Used by the Providers form / .env editor to offer a model dropdown.
    For HTTP providers (ollama/openwebui), ``supported`` is True and
    ``reachable`` is True/False. For provider types that don't expose an
    HTTP model list (claude/codex/gemini/pi/shell) ``supported`` is False
    and ``reachable`` is ``null`` so the UI keeps a free-text box. Never
    raises on a bad endpoint. The blocking ``urllib`` work runs in a
    thread so it never stalls the event loop.
    """
    return await asyncio.to_thread(
        _probe_blocking, (body.type or "").lower(), (body.endpoint or "").strip(), body.api_key or "",
    )


__all__ = ["router"]
