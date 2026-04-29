"""OpenWebUI HTTP provider.

OpenWebUI exposes an OpenAI-compatible API. We support two endpoints:

  * GET  ``/api/models``               -> list models, used by kickoff
  * POST ``/api/chat/completions``     -> chat completion

Configuration in ``.clk/config/providers.json``::

    "openwebui": {
        "type": "openwebui",
        "endpoint": "https://chat.example.com",
        "api_key": "<bearer token>",
        "model": "llama3.1:8b"
    }

The provider uses Python's stdlib ``urllib`` so the harness has no hard
dependency on ``requests``.
"""

from __future__ import annotations

import json
import socket
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict, List
from urllib.parse import urlparse

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens


def _auth_header(api_key: str) -> Dict[str, str]:
    if not api_key:
        return {}
    return {"Authorization": f"Bearer {api_key}"}


def list_models(endpoint: str, api_key: str, *, timeout_s: float = 5.0) -> List[str]:
    """Fetch the model list from an OpenWebUI server.

    Returns an empty list on any failure (network, auth, parse) so the
    kickoff script can fall back to manual entry without crashing.
    """
    url = endpoint.rstrip("/") + "/api/models"
    req = urllib.request.Request(
        url,
        headers={"Accept": "application/json", **_auth_header(api_key)},
        method="GET",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    out: List[str] = []
    # OpenAI-style: {"data": [{"id": "..."}]} ; OpenWebUI also returns
    # this shape. Accept either to be lenient.
    candidates = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(candidates, list):
        candidates = payload.get("models") if isinstance(payload, dict) else []
    if isinstance(candidates, list):
        for entry in candidates:
            if isinstance(entry, dict):
                mid = entry.get("id") or entry.get("name")
                if mid:
                    out.append(str(mid))
            elif isinstance(entry, str):
                out.append(entry)
    return out


class OpenWebUIProvider(AgentProvider):
    type_name = "openwebui"

    def _endpoint(self) -> str:
        return (self.config.get("endpoint") or "http://localhost:8080").rstrip("/")

    def _api_key(self) -> str:
        return self.config.get("api_key") or ""

    def _model(self) -> str:
        return self.config.get("model") or "llama3.1"

    def available(self) -> bool:
        try:
            url = urlparse(self._endpoint())
            host = url.hostname or "localhost"
            port = url.port or (443 if url.scheme == "https" else 80)
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except Exception:
            return False

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "openwebui-dry"
            return AgentResponse(
                ok=True,
                text=f"[openwebui] dry-run agent={req.agent}",
                raw={"dry_run": True},
                usage=usage,
            )
        if not self.available():
            return AgentResponse(
                ok=False,
                error=f"openwebui endpoint unreachable: {self._endpoint()}",
            )

        body: Dict[str, Any] = {
            "model": self._model(),
            "messages": [
                {"role": "system", "content": req.system or "You are a helpful agent."},
                {"role": "user", "content": req.prompt},
            ],
            "stream": False,
        }
        url = self._endpoint() + "/api/chat/completions"
        data = json.dumps(body).encode("utf-8")
        try:
            request = urllib.request.Request(
                url,
                data=data,
                headers={
                    "Content-Type": "application/json",
                    "Accept": "application/json",
                    **_auth_header(self._api_key()),
                },
                method="POST",
            )
            with urllib.request.urlopen(request, timeout=req.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            choices = payload.get("choices") or []
            if choices:
                msg = (choices[0] or {}).get("message") or {}
                text = msg.get("content") or ""
            else:
                text = payload.get("response") or ""
            usage_obj = payload.get("usage") or {}
            in_tok = int(usage_obj.get("prompt_tokens") or 0)
            out_tok = int(usage_obj.get("completion_tokens") or 0)
            tot_tok = int(usage_obj.get("total_tokens") or (in_tok + out_tok))
            if in_tok or out_tok:
                usage = {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "total_tokens": tot_tok,
                    "source": "openwebui-api",
                }
            else:
                usage = estimate_tokens(req.prompt, text)
                usage["source"] = "openwebui-estimate"
            return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
        except urllib.error.HTTPError as exc:
            print(f"[providers.openwebui.invoke] HTTP error: {exc}", file=sys.stderr)
            traceback.print_exc()
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            return AgentResponse(
                ok=False,
                error=f"openwebui HTTP {exc.code}: {exc.reason} {detail}".strip(),
            )
        except urllib.error.URLError as exc:
            print(f"[providers.openwebui.invoke] URL error: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=f"openwebui unreachable: {exc.reason}")
        except Exception as exc:
            print(f"[providers.openwebui.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))
