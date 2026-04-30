"""Google Gemini CLI provider.

Drives the ``gemini`` CLI in non-interactive mode. The CLI is detected
via ``which`` so the rest of the harness can degrade gracefully when it
is not installed. Prompts are passed on stdin; configure ``args`` in
``providers.json`` to add flags (e.g. ``--model``) without code changes.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import traceback
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens, run_streaming


_DEFAULT_API_BASE = "https://generativelanguage.googleapis.com/v1beta"
_DEFAULT_API_MODEL = "gemini-2.0-flash"


def _try_extract_json_usage(text: str) -> Dict[str, Any]:
    """If ``text`` is a JSON envelope with a usage field, parse it.

    Different Gemini CLI versions emit different JSON shapes; we accept
    any structure that looks plausibly OpenAI-compatible.
    """
    if not text or not text.strip().startswith("{"):
        return {}
    try:
        env = json.loads(text.strip())
    except json.JSONDecodeError:
        return {}
    usage = (env.get("usage") if isinstance(env, dict) else None) or {}
    if not isinstance(usage, dict):
        return {}
    in_tok = int(usage.get("prompt_token_count")
                 or usage.get("input_tokens")
                 or usage.get("prompt_tokens") or 0)
    out_tok = int(usage.get("candidates_token_count")
                  or usage.get("output_tokens")
                  or usage.get("completion_tokens") or 0)
    if in_tok or out_tok:
        return {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": in_tok + out_tok,
            "source": "gemini-json",
        }
    return {}


class GeminiProvider(AgentProvider):
    type_name = "gemini"

    def _mode(self) -> str:
        return (self.config.get("mode") or "cli").lower()

    def _cmd(self) -> str:
        return self.config.get("command") or "gemini"

    def _api_key(self) -> str:
        return (
            self.config.get("api_key")
            or os.environ.get("GEMINI_API_KEY")
            or os.environ.get("GOOGLE_API_KEY")
            or ""
        )

    def available(self) -> bool:
        if self._mode() == "api":
            return bool(self._api_key())
        return shutil.which(self._cmd()) is not None

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "gemini-dry"
            return AgentResponse(
                ok=True,
                text=f"[gemini] dry-run agent={req.agent} mode={self._mode()}",
                raw={"dry_run": True},
                usage=usage,
            )
        if self._mode() == "api":
            return self._invoke_api(req)
        if not self.available():
            return AgentResponse(ok=False, error=f"gemini CLI not found ({self._cmd()})")

        # Default args are intentionally minimal so this works with the
        # Google `gemini` CLI as it ships. Users with a different binary
        # (or who want a specific model / output format) override args
        # in providers.json.
        cmd = [self._cmd(), *(self.config.get("args") or [])]
        try:
            rc, stdout, stderr = run_streaming(
                cmd,
                stdin_text=req.prompt,
                timeout_s=req.timeout_s,
                no_output_timeout_s=req.no_output_timeout_s,
                cwd=req.workdir,
                on_progress=req.on_progress,
            )
        except Exception as exc:
            print(f"[providers.gemini.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))

        text = stdout or ""
        usage = _try_extract_json_usage(text)
        if not usage:
            usage = estimate_tokens(req.prompt, text)
            usage["source"] = "gemini-estimate"
        if rc == -1:
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s", usage=usage)
        if rc == -3:
            return AgentResponse(ok=False, error=f"no output for {req.no_output_timeout_s}s", usage=usage)
        if rc != 0:
            return AgentResponse(
                ok=False,
                text=text,
                error=(stderr or "").strip() or f"gemini exited rc={rc}",
                usage=usage,
            )
        return AgentResponse(ok=True, text=text, raw={"stderr": stderr}, usage=usage)

    def _invoke_api(self, req: AgentRequest) -> AgentResponse:
        """Direct call to the Gemini generateContent endpoint. No subprocess."""
        api_key = self._api_key()
        if not api_key:
            return AgentResponse(
                ok=False,
                error="gemini api mode: no API key (set GEMINI_API_KEY / GOOGLE_API_KEY or providers.gemini.api_key)",
            )
        progress = req.on_progress or (lambda kind, msg: None)
        base = (self.config.get("endpoint") or _DEFAULT_API_BASE).rstrip("/")
        model = self.config.get("model") or _DEFAULT_API_MODEL
        endpoint = f"{base}/models/{urllib.parse.quote(model)}:generateContent?key={urllib.parse.quote(api_key)}"
        body = {
            "contents": [{"parts": [{"text": req.prompt}]}],
        }
        if req.system:
            body["systemInstruction"] = {"parts": [{"text": req.system}]}
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
            method="POST",
        )
        progress("http_request", f"POST gemini model={model} timeout_s={req.timeout_s}")
        try:
            with urllib.request.urlopen(request, timeout=req.timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            progress("http_response", f"rc={exc.code} reason={exc.reason} detail={detail}")
            return AgentResponse(ok=False, error=f"gemini api HTTP {exc.code}: {exc.reason} {detail}".strip())
        except urllib.error.URLError as exc:
            progress("http_error", f"unreachable: {exc.reason}")
            return AgentResponse(ok=False, error=f"gemini api unreachable: {exc.reason}")
        except Exception as exc:
            print(f"[providers.gemini._invoke_api] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"{exc}")
            return AgentResponse(ok=False, error=str(exc))
        progress("http_response", "rc=200")
        # candidates -> [{ content: { parts: [{text: ...}] } }]
        text_parts = []
        for cand in (payload.get("candidates") or []):
            content = (cand or {}).get("content") or {}
            for part in (content.get("parts") or []):
                if isinstance(part, dict) and part.get("text"):
                    text_parts.append(part["text"])
        text = "".join(text_parts)
        u = payload.get("usageMetadata") or {}
        in_tok = int(u.get("promptTokenCount") or 0)
        out_tok = int(u.get("candidatesTokenCount") or 0)
        usage = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "total_tokens": int(u.get("totalTokenCount") or (in_tok + out_tok)),
            "source": "gemini-api",
        }
        return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
