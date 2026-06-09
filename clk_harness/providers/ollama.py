"""Ollama HTTP provider.

Uses Python's stdlib ``urllib`` so the harness has no hard dependency
on ``requests``. If the Ollama server is unreachable the provider
reports unavailable.
"""

from __future__ import annotations

import json
import os
import sys
import traceback
import urllib.error
import urllib.request

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens
from ._endpoint_fallback import maybe_docker_host_fallback, probe_endpoint


def list_models(endpoint: str, *, timeout_s: float = 5.0) -> list:
    """Fetch installed model names from an Ollama server (``GET /api/tags``).

    Returns an empty list on any failure (network, parse) so callers can
    fall back to manual entry. Applies the container→host fallback so a
    ``localhost`` endpoint still resolves from inside Docker.
    """
    base = (endpoint or "http://localhost:11434").rstrip("/")
    if not probe_endpoint(base):
        swapped = maybe_docker_host_fallback(base, label="ollama")
        if swapped:
            base = swapped
    url = base + "/api/tags"
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"}, method="GET")
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except Exception:
        return []
    out = []
    for entry in (payload.get("models") if isinstance(payload, dict) else []) or []:
        if isinstance(entry, dict):
            name = entry.get("name") or entry.get("model")
            if name:
                out.append(str(name))
    return out


class OllamaProvider(AgentProvider):
    type_name = "ollama"

    def _endpoint(self) -> str:
        # .env (CLK_OLLAMA_ENDPOINT) wins when set so the global config can
        # drive the connection without editing providers.json.
        return (
            os.environ.get("CLK_OLLAMA_ENDPOINT")
            or self.config.get("endpoint")
            or "http://localhost:11434"
        ).rstrip("/")

    def available(self) -> bool:
        endpoint = self._endpoint()
        if probe_endpoint(endpoint):
            return True
        # Container-on-host rescue: if the configured localhost endpoint
        # is dead but host.docker.internal answers, mutate our config so
        # subsequent calls (invoke, list_models, …) use the working URL.
        swapped = maybe_docker_host_fallback(endpoint, label="ollama")
        if swapped:
            self.config["endpoint"] = swapped
            return True
        return False

    def _model(self) -> str:
        return os.environ.get("CLK_OLLAMA_MODEL") or self.config.get("model") or "llama3.1"

    def invoke(self, req: AgentRequest) -> AgentResponse:
        progress = req.on_progress or (lambda kind, msg: None)
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "ollama-dry"
            return AgentResponse(
                ok=True,
                text=f"[ollama] dry-run agent={req.agent}",
                raw={"dry_run": True},
                usage=usage,
            )
        if not self.available():
            return AgentResponse(ok=False, error=f"ollama endpoint unreachable: {self._endpoint()}")

        body = {
            "model": self._model(),
            "prompt": req.prompt,
            "system": req.system or "",
            "stream": False,
        }
        url = self._endpoint() + "/api/generate"
        data = json.dumps(body).encode("utf-8")
        try:
            request = urllib.request.Request(
                url,
                data=data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            progress("http_request", f"POST {url} model={self._model()} timeout_s={req.timeout_s}")
            with urllib.request.urlopen(request, timeout=req.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            progress("http_response", "rc=200")
            text = payload.get("response", "")
            in_tok = int(payload.get("prompt_eval_count") or 0)
            out_tok = int(payload.get("eval_count") or 0)
            if in_tok or out_tok:
                usage = {
                    "input_tokens": in_tok,
                    "output_tokens": out_tok,
                    "total_tokens": in_tok + out_tok,
                    "source": "ollama-api",
                }
            else:
                usage = estimate_tokens(req.prompt, text)
                usage["source"] = "ollama-estimate"
            return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
        except urllib.error.HTTPError as exc:
            print(f"[providers.ollama.invoke] HTTP error: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_response", f"rc={exc.code} reason={exc.reason}")
            return AgentResponse(ok=False, error=f"ollama HTTP {exc.code}: {exc.reason}")
        except urllib.error.URLError as exc:
            print(f"[providers.ollama.invoke] URL error: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"unreachable: {exc.reason}")
            return AgentResponse(ok=False, error=f"ollama unreachable: {exc.reason}")
        except Exception as exc:
            print(f"[providers.ollama.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"{exc}")
            return AgentResponse(ok=False, error=str(exc))
