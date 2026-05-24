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
import sys
import time
import traceback
import urllib.error
import urllib.request
import uuid
from typing import Any, Dict, List, Tuple

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
        endpoint = self._endpoint()
        from ._endpoint_fallback import maybe_docker_host_fallback, probe_endpoint
        if probe_endpoint(endpoint):
            return True
        swapped = maybe_docker_host_fallback(endpoint, label="openwebui")
        if swapped:
            self.config["endpoint"] = swapped
            return True
        return False

    def invoke(self, req: AgentRequest) -> AgentResponse:
        progress = req.on_progress or (lambda kind, msg: None)
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

        # Recent OpenWebUI builds require chat_id in the payload; missing it
        # surfaces as HTTP 400 with "'NoneType' object has no attribute
        # 'startswith'". See https://github.com/greeves89/owui_coding_proxy/commit/1fceee8
        body: Dict[str, Any] = {
            "model": self._model(),
            "messages": [
                {"role": "system", "content": req.system or "You are a helpful agent."},
                {"role": "user", "content": req.prompt},
            ],
            "stream": False,
            "chat_id": str(uuid.uuid4()),
        }
        url = self._endpoint() + "/api/chat/completions"

        # Retry once on transient/opaque upstream failures: 5xx, plus 4xx
        # whose body matches a known opaque-proxy pattern (e.g. OpenWebUI's
        # "Provider returned error", which hides the real OpenRouter reason).
        attempts = 2
        last_err: AgentResponse = AgentResponse(ok=False, error="no attempts made")
        for attempt in range(1, attempts + 1):
            outcome = self._do_post(url, body, req.timeout_s, progress, attempt, attempts)
            if outcome.ok:
                return outcome
            last_err = outcome
            if attempt < attempts and outcome.raw and outcome.raw.get("retryable"):
                delay = outcome.raw.get("retry_after_s") or 1.5
                progress("http_retry", f"attempt {attempt}/{attempts} failed; sleeping {delay}s before retry")
                try:
                    time.sleep(float(delay))
                except Exception:
                    pass
                continue
            break
        return last_err

    def _do_post(
        self,
        url: str,
        body: Dict[str, Any],
        timeout_s: float,
        progress,
        attempt: int,
        attempts: int,
    ) -> AgentResponse:
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
            sys_len = len(body["messages"][0].get("content") or "")
            usr_len = len(body["messages"][1].get("content") or "")
            progress(
                "http_request",
                (
                    f"POST {url} model={body['model']} timeout_s={timeout_s} "
                    f"attempt={attempt}/{attempts} bytes={len(data)} "
                    f"sys_chars={sys_len} user_chars={usr_len}"
                ),
            )
            with urllib.request.urlopen(request, timeout=timeout_s) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
            progress("http_response", f"rc=200 attempt={attempt}/{attempts}")
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
                usage = estimate_tokens("", text)
                usage["source"] = "openwebui-estimate"
            return AgentResponse(ok=True, text=text, raw=payload, usage=usage)
        except urllib.error.HTTPError as exc:
            detail, headers_str = _read_error_detail(exc)
            retryable, reason = _classify_retryable(exc.code, detail)
            retry_after = _retry_after_seconds(exc)
            print(
                f"[providers.openwebui.invoke] HTTP {exc.code} {exc.reason} "
                f"url={url} model={body.get('model')} attempt={attempt}/{attempts} "
                f"retryable={retryable}{' (' + reason + ')' if reason else ''} "
                f"retry_after_s={retry_after} headers=[{headers_str}] detail={detail}",
                file=sys.stderr,
            )
            traceback.print_exc()
            progress(
                "http_response",
                (
                    f"rc={exc.code} reason={exc.reason} attempt={attempt}/{attempts} "
                    f"retryable={retryable}{' (' + reason + ')' if reason else ''} "
                    f"retry_after_s={retry_after} headers={headers_str} detail={detail}"
                ),
            )
            return AgentResponse(
                ok=False,
                error=f"openwebui HTTP {exc.code}: {exc.reason} {detail}".strip(),
                raw={"retryable": retryable, "retry_after_s": retry_after, "status": exc.code},
            )
        except urllib.error.URLError as exc:
            print(f"[providers.openwebui.invoke] URL error: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"unreachable: {exc.reason} attempt={attempt}/{attempts}")
            return AgentResponse(
                ok=False,
                error=f"openwebui unreachable: {exc.reason}",
                raw={"retryable": True},
            )
        except Exception as exc:
            print(f"[providers.openwebui.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            progress("http_error", f"{exc} attempt={attempt}/{attempts}")
            return AgentResponse(ok=False, error=str(exc))


# OpenWebUI's OpenAI-compat proxy hides upstream provider errors behind
# generic strings. Match the known patterns so we can retry once.
_OPAQUE_PROXY_PATTERNS = (
    "provider returned error",
    "upstream error",
    "no endpoints available",
)


def _classify_retryable(status: int, detail: str) -> Tuple[bool, str]:
    if status >= 500:
        return True, f"{status} server error"
    if status == 429:
        return True, "rate limited"
    lowered = (detail or "").lower()
    for pat in _OPAQUE_PROXY_PATTERNS:
        if pat in lowered:
            return True, f"opaque upstream: {pat}"
    return False, ""


def _retry_after_seconds(exc: urllib.error.HTTPError) -> float:
    try:
        ra = exc.headers.get("Retry-After") if exc.headers else None
        if ra:
            return float(ra)
    except Exception:
        pass
    return 0.0


def _read_error_detail(exc: urllib.error.HTTPError) -> Tuple[str, str]:
    try:
        detail = exc.read().decode("utf-8", errors="replace")[:2000]
    except Exception:
        detail = ""
    headers_str = ""
    try:
        if exc.headers is not None:
            interesting = ("Content-Type", "X-Request-Id", "Retry-After",
                           "X-Ratelimit-Remaining", "X-Ratelimit-Reset",
                           "X-Openrouter-Request-Id")
            parts = []
            for h in interesting:
                v = exc.headers.get(h)
                if v:
                    parts.append(f"{h}={v}")
            headers_str = ";".join(parts)
    except Exception:
        pass
    return detail, headers_str
