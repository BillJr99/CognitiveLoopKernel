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
import os
from typing import Any, Callable, Dict, List, Optional

from pydantic import BaseModel

from . import env_file
from .config import load_providers_config
from .log import get_logger

# The REST surface now lives in the ``clk_harness.webui_api`` package.
# Importing it registers every endpoint module on the shared router; the
# explicit re-exports below preserve this module's original public surface
# for existing importers (tests included).
from .webui_api import events as _events  # noqa: F401  (registers the activity endpoints)
from .webui_api.files import (  # noqa: F401
    _SHA_RE as _SHA_RE,
)
from .webui_api.files import (
    _require_sha as _require_sha,
)
from .webui_api.router import (
    _HIDDEN_DIRS as _HIDDEN_DIRS,
)
from .webui_api.router import (
    _MAX_FILE_BYTES as _MAX_FILE_BYTES,
)
from .webui_api.router import (
    _MAX_FILES as _MAX_FILES,
)
from .webui_api.router import (
    _SECRET_PROVIDER_FIELDS as _SECRET_PROVIDER_FIELDS,
)
from .webui_api.router import (  # noqa: F401, I001
    AgentsUpdate as AgentsUpdate,
)
from .webui_api.router import (
    ClkConfigUpdate as ClkConfigUpdate,
)
from .webui_api.router import (
    EnvUpdate as EnvUpdate,
)
from .webui_api.router import (
    FileWrite as FileWrite,
)
from .webui_api.router import (
    IdeaUpdate as IdeaUpdate,
)
from .webui_api.router import (
    ProvidersUpdate as ProvidersUpdate,
)
from .webui_api.router import (
    _activity_path as _activity_path,
)
from .webui_api.router import (
    _api as _api,
)
from .webui_api.router import (
    _is_probably_binary as _is_probably_binary,
)
from .webui_api.router import (
    _mask_provider_block as _mask_provider_block,
)
from .webui_api.router import (
    _read_idea as _read_idea,
)
from .webui_api.router import (
    _require_workspace as _require_workspace,
)
from .webui_api.router import (
    _safe_unlink as _safe_unlink,
)
from .webui_api.router import (
    _safe_ws_file as _safe_ws_file,
)
from .webui_api.router import (
    _unmask_provider_block as _unmask_provider_block,
)
from .webui_api.router import (
    _ws_paths as _ws_paths,
)
from .webui_api.router import (
    router as router,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Provider probe + discovery (guided mode)
#
# These stay in this module (rather than moving into ``webui_api``) because
# the test suite patches ``webui_router._probe_blocking`` and the discovery
# path below must resolve that patched module-global at call time.
# ---------------------------------------------------------------------------


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
