"""OpenAI Codex CLI provider."""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
import urllib.error
import urllib.request
from typing import Any, Dict

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens, run_streaming

_DEFAULT_API_ENDPOINT = "https://api.openai.com/v1/chat/completions"
_DEFAULT_API_MODEL = "gpt-4o-mini"


class CodexProvider(AgentProvider):
    type_name = "codex"

    def _mode(self) -> str:
        return (self.config.get("mode") or "cli").lower()

    def _cmd(self) -> str:
        return self.config.get("command") or "codex"

    def _api_key(self) -> str:
        return self.config.get("api_key") or os.environ.get("OPENAI_API_KEY") or ""

    def available(self) -> bool:
        if self._mode() == "api":
            return bool(self._api_key())
        return shutil.which(self._cmd()) is not None

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "codex-dry"
            return AgentResponse(
                ok=True,
                text=f"[codex] dry-run agent={req.agent} mode={self._mode()}",
                raw={"dry_run": True},
                usage=usage,
            )
        if self._mode() == "api":
            return self._invoke_api(req)
        if not self.available():
            return AgentResponse(ok=False, error=f"codex CLI not found ({self._cmd()})")

        # codex exec takes the prompt as a positional arg, not stdin.
        args = list(self.config.get("args") or ["exec"])
        cmd = [self._cmd(), *args, req.prompt]
        try:
            rc, stdout, stderr = run_streaming(
                cmd,
                stdin_text=None,
                timeout_s=req.timeout_s,
                no_output_timeout_s=req.no_output_timeout_s,
                cwd=req.workdir,
                on_progress=req.on_progress,
            )
        except Exception as exc:
            print(f"[providers.codex.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))

        text = stdout or ""
        usage = estimate_tokens(req.prompt, text)
        usage["source"] = "codex-estimate"
        if rc == -1:
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s", usage=usage)
        if rc == -3:
            return AgentResponse(ok=False, error=f"no output for {req.no_output_timeout_s}s", usage=usage)
        if rc != 0:
            return AgentResponse(
                ok=False,
                text=text,
                error=(stderr or "").strip() or f"codex exited rc={rc}",
                usage=usage,
            )
        return AgentResponse(ok=True, text=text, raw={"stderr": stderr}, usage=usage)

    def _invoke_api(self, req: AgentRequest) -> AgentResponse:
        """Direct OpenAI Chat Completions call. No subprocess."""
        api_key = self._api_key()
        if not api_key:
            return AgentResponse(
                ok=False,
                error="codex api mode: no API key (set OPENAI_API_KEY or providers.codex.api_key)",
            )
        progress = req.on_progress or (lambda kind, msg: None)
        endpoint = self.config.get("endpoint") or _DEFAULT_API_ENDPOINT
        model = self.config.get("model") or _DEFAULT_API_MODEL
        body: Dict[str, Any] = {
            "model": model,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system:
            body["messages"].insert(0, {"role": "system", "content": req.system})
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "Authorization": f"Bearer {api_key}",
            },
            method="POST",
        )
        progress("http_request", f"POST {endpoint} model={model} timeout_s={req.timeout_s}")
        try:
            with urllib.request.urlopen(request, timeout=req.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            progress("http_response", f"rc={exc.code} reason={exc.reason} detail={detail}")
            return AgentResponse(ok=False, error=f"codex api HTTP {exc.code}: {exc.reason} {detail}".strip())
        except urllib.error.URLError as exc:
            progress("http_error", f"unreachable: {exc.reason}")
            return AgentResponse(ok=False, error=f"codex api unreachable: {exc.reason}")
        except Exception as exc:
            print(f"[providers.codex._invoke_api] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"{exc}")
            return AgentResponse(ok=False, error=str(exc))
        progress("http_response", "rc=200")
        choices = payload.get("choices") or []
        text = ""
        if choices:
            text = ((choices[0] or {}).get("message") or {}).get("content") or ""
        u = payload.get("usage") or {}
        in_tok = int(u.get("prompt_tokens") or 0)
        out_tok = int(u.get("completion_tokens") or 0)
        usage = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": int(u.get("total_tokens") or (in_tok + out_tok)),
            "source": "codex-api",
        }
        return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
