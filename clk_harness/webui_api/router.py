"""Web-UI REST router: shared helpers, config, .env, doctor, and idea.

The shared :data:`router` every other ``webui_api`` module registers
its endpoints on lives here, together with the workspace/path guards
and the pydantic request models.
"""

from __future__ import annotations

import asyncio
import copy
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter
from pydantic import BaseModel, Field

from .. import env_file
from ..config import (
    Paths,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    save_agents_config,
    save_json,
    save_providers_config,
)
from ..log import get_logger
from ..providers import available_providers

logger = get_logger(__name__)

router = APIRouter()

# ---------------------------------------------------------------------------
# Helpers (import lazily from api to avoid a circular import at module load)
# ---------------------------------------------------------------------------

def _api():
    from .. import api  # local import: api imports this module
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
    from ..config import DEFAULT_PROVIDERS
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
    from ..config import DEFAULT_PROVIDERS
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
        from ..config import project_paths
        paths = project_paths()
    cfg = load_providers_config(paths)
    return {"ok": True, "active": cfg.get("active"), "available": available_providers(cfg)}
