"""Claude Code provider.

Drives the ``claude`` CLI in non-interactive print mode. We request
``--output-format json`` so the harness can extract real token usage
from the CLI's response envelope. If the JSON envelope is unavailable
(older CLI, or non-zero exit) we fall back to plain stdout and an
estimated token count.
"""

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


_DEFAULT_API_ENDPOINT = "https://api.anthropic.com/v1/messages"
_DEFAULT_API_VERSION = "2023-06-01"
_DEFAULT_API_MODEL = "claude-sonnet-4-5"


def _extract_usage(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Pull token counts out of a Claude CLI JSON envelope.

    The Claude CLI exposes usage under ``usage`` (newer builds) or
    nested in ``message.usage`` (older builds). We accept both shapes.
    """
    usage_obj: Dict[str, Any] = {}
    cand = envelope.get("usage") if isinstance(envelope, dict) else None
    if isinstance(cand, dict):
        usage_obj = cand
    else:
        msg = envelope.get("message") if isinstance(envelope, dict) else None
        if isinstance(msg, dict) and isinstance(msg.get("usage"), dict):
            usage_obj = msg["usage"]
    if not usage_obj:
        return {}
    in_tok = int(usage_obj.get("input_tokens") or usage_obj.get("prompt_tokens") or 0)
    out_tok = int(usage_obj.get("output_tokens") or usage_obj.get("completion_tokens") or 0)
    cache_create = int(usage_obj.get("cache_creation_input_tokens") or 0)
    cache_read = int(usage_obj.get("cache_read_input_tokens") or 0)
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "cache_creation_input_tokens": cache_create,
        "cache_read_input_tokens": cache_read,
        "total_tokens": in_tok + out_tok + cache_create + cache_read,
        "source": "claude-json",
    }


# Maps abstract capability names to claude CLI flags.
# claude CLI does not expose a --no-tools flag, so tool-related capabilities
# are intentionally omitted here (they are pi-specific for now).
_CAP_MAP: dict = {
    "thinking-off":    ["--thinking", "off"],
    "thinking-low":    ["--thinking", "low"],
    "thinking-medium": ["--thinking", "medium"],
    "thinking-high":   ["--thinking", "high"],
}


class ClaudeProvider(AgentProvider):
    type_name = "claude"

    def capabilities_to_args(self, capabilities: list) -> list:
        result: list = []
        for cap in capabilities:
            extra = _CAP_MAP.get((cap or "").lower().strip())
            if extra:
                result.extend(extra)
        return result

    def _mode(self) -> str:
        # "cli" = spawn the local Claude CLI subprocess.
        # "api" = call the Anthropic HTTP API directly (no CLI required;
        # uses the api_key from config or ANTHROPIC_API_KEY env var).
        return (self.config.get("mode") or "cli").lower()

    def _cmd(self) -> str:
        return self.config.get("command") or "claude"

    def _api_key(self) -> str:
        return self.config.get("api_key") or os.environ.get("ANTHROPIC_API_KEY") or ""

    def available(self) -> bool:
        if self._mode() == "api":
            return bool(self._api_key())
        return shutil.which(self._cmd()) is not None

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "claude-dry"
            return AgentResponse(
                ok=True,
                text=f"[claude] dry-run agent={req.agent} mode={self._mode()}",
                raw={"dry_run": True},
                usage=usage,
            )
        if self._mode() == "api":
            return self._invoke_api(req)
        if not self.available():
            return AgentResponse(ok=False, error=f"claude CLI not found ({self._cmd()})")

        # Default to plain --print for maximum compatibility (older
        # Claude CLI builds reject --output-format and hang waiting on
        # stdin). Users who want real token counts can opt-in via
        # providers.json:  "args": ["--print", "--output-format", "json"]
        # The parser below will try JSON first regardless of how args
        # were configured, so opt-in works automatically.
        default_args = ["--print"]
        cap_args = self.capabilities_to_args(req.capabilities or [])
        cmd = [self._cmd(), *(self.config.get("args") or default_args), *cap_args]
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
            print(f"[providers.claude.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))

        if rc == -1:
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s")
        if rc == -3:
            return AgentResponse(ok=False, error=f"no output for {req.no_output_timeout_s}s")
        if rc != 0:
            usage = estimate_tokens(req.prompt, stdout or "")
            usage["source"] = "claude-estimate"
            return AgentResponse(
                ok=False,
                text=stdout,
                error=(stderr or "").strip() or f"claude exited rc={rc}",
                usage=usage,
            )

        text = stdout or ""
        envelope: Dict[str, Any] = {}
        usage: Dict[str, Any] = {}
        stripped = text.strip()
        if stripped.startswith("{"):
            try:
                envelope = json.loads(stripped)
                if isinstance(envelope, dict):
                    if isinstance(envelope.get("result"), str):
                        text = envelope["result"]
                    usage = _extract_usage(envelope)
            except json.JSONDecodeError:
                envelope = {}
        if not usage:
            usage = estimate_tokens(req.prompt, text)
            usage["source"] = "claude-estimate"
        return AgentResponse(
            ok=True,
            text=text,
            raw={"stderr": stderr, "envelope": envelope},
            usage=usage,
        )

    def _invoke_api(self, req: AgentRequest) -> AgentResponse:
        """Direct call to the Anthropic Messages API. No subprocess.

        Used when ``mode`` is set to ``"api"`` (typically via
        CLK_AUTH_MODE=apikey at kickoff). The key is read from the
        provider config or the ANTHROPIC_API_KEY env var.
        """
        api_key = self._api_key()
        if not api_key:
            return AgentResponse(
                ok=False,
                error="claude api mode: no API key (set ANTHROPIC_API_KEY or providers.claude.api_key)",
            )
        progress = req.on_progress or (lambda kind, msg: None)
        endpoint = self.config.get("endpoint") or _DEFAULT_API_ENDPOINT
        model = self.config.get("model") or _DEFAULT_API_MODEL
        max_tokens = int(self.config.get("max_tokens") or 4096)
        body = {
            "model": model,
            "max_tokens": max_tokens,
            "messages": [{"role": "user", "content": req.prompt}],
        }
        if req.system:
            body["system"] = req.system
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(
            endpoint,
            data=data,
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "x-api-key": api_key,
                "anthropic-version": self.config.get("api_version") or _DEFAULT_API_VERSION,
            },
            method="POST",
        )
        progress("http_request", f"POST {endpoint} model={model} timeout_s={req.timeout_s}")
        try:
            # timeout=0 means non-blocking in Python socket semantics; use
            # None (blocking, no limit) when the harness has no hard timeout set.
            _timeout = req.timeout_s if req.timeout_s and req.timeout_s > 0 else None
            with urllib.request.urlopen(request, timeout=_timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:
                detail = ""
            progress("http_response", f"rc={exc.code} reason={exc.reason} detail={detail}")
            return AgentResponse(
                ok=False,
                error=f"claude api HTTP {exc.code}: {exc.reason} {detail}".strip(),
            )
        except urllib.error.URLError as exc:
            progress("http_error", f"unreachable: {exc.reason}")
            return AgentResponse(ok=False, error=f"claude api unreachable: {exc.reason}")
        except Exception as exc:
            print(f"[providers.claude._invoke_api] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"{exc}")
            return AgentResponse(ok=False, error=str(exc))
        progress("http_response", "rc=200")
        # Concatenate text blocks from the response content array.
        text_parts = []
        for block in (payload.get("content") or []):
            if isinstance(block, dict) and block.get("type") == "text":
                text_parts.append(block.get("text") or "")
        text = "".join(text_parts)
        usage_obj = payload.get("usage") or {}
        in_tok = int(usage_obj.get("input_tokens") or 0)
        out_tok = int(usage_obj.get("output_tokens") or 0)
        cache_create = int(usage_obj.get("cache_creation_input_tokens") or 0)
        cache_read = int(usage_obj.get("cache_read_input_tokens") or 0)
        usage = {
            "input_tokens": in_tok,
            "output_tokens": out_tok,
            "cache_creation_input_tokens": cache_create,
            "cache_read_input_tokens": cache_read,
            "total_tokens": in_tok + out_tok + cache_create + cache_read,
            "source": "claude-api",
        }
        return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
