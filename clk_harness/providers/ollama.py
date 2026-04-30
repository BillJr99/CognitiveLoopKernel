"""Ollama HTTP provider.

Uses Python's stdlib ``urllib`` so the harness has no hard dependency
on ``requests``. If the Ollama server is unreachable the provider
reports unavailable.
"""

from __future__ import annotations

import json
import socket
import sys
import traceback
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens


class OllamaProvider(AgentProvider):
    type_name = "ollama"

    def _endpoint(self) -> str:
        return (self.config.get("endpoint") or "http://localhost:11434").rstrip("/")

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
