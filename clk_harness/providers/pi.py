"""Pi terminal harness provider.

Pi exposes an extensible CLI; we simply pipe the prompt to ``pi`` if it
is on PATH, falling back to ``.clk/tools/pi/bin/pi`` if cloned locally.
"""

from __future__ import annotations

import os
import shutil
import sys
import traceback
from pathlib import Path

from .base import AgentProvider, AgentRequest, AgentResponse, estimate_tokens, run_streaming

def _is_system_pi(path: str) -> bool:
    """Return True if path points to the system pi calculator (not pi.dev).

    On Debian/Ubuntu /usr/bin/pi computes digits of π and has nothing to do
    with the pi.dev terminal harness. Exclude it so we don't falsely report
    the pi.dev CLI as available.
    """
    import subprocess as _sp
    # Known system calculator paths
    if path in ("/usr/bin/pi", "/bin/pi"):
        return True
    # Heuristic: the system calculator accepts no flags and prints only digits.
    # The pi.dev CLI should respond to --version or --help without just digits.
    try:
        out = _sp.run([path, "--version"], capture_output=True, text=True, timeout=3)
        combined = (out.stdout + out.stderr).strip()
        # If the only output is a short all-digit string it's the calculator.
        if combined and combined.replace(".", "").isdigit() and len(combined) < 20:
            return True
    except Exception:
        pass
    return False


# Explicit overrides for providers whose env var doesn't follow the
# {NAME.upper()}_API_KEY convention.  Empty today — add entries here
# only if a future provider breaks the pattern.
_KEY_TYPE_ENV_OVERRIDES: dict = {}


def _env_var_for_key_type(key_type: str) -> str:
    """Return the env var name for a given key_type.

    Follows the convention ``{KEY_TYPE.upper()}_API_KEY`` unless an
    explicit override is registered in ``_KEY_TYPE_ENV_OVERRIDES``.
    Examples: openrouter -> OPENROUTER_API_KEY, mistral -> MISTRAL_API_KEY.
    """
    return _KEY_TYPE_ENV_OVERRIDES.get(key_type) or f"{key_type.upper()}_API_KEY"

# Maps abstract capability names to pi CLI flags.
_CAP_MAP: dict = {
    "no-tools":          ["--no-tools"],
    "no-builtin-tools":  ["--no-builtin-tools"],
    "thinking-off":      ["--thinking", "off"],
    "thinking-low":      ["--thinking", "low"],
    "thinking-medium":   ["--thinking", "medium"],
    "thinking-high":     ["--thinking", "high"],
    "thinking-xhigh":    ["--thinking", "xhigh"],
}


class PiProvider(AgentProvider):
    type_name = "pi"

    def capabilities_to_args(self, capabilities: list) -> list:
        result: list = []
        for cap in capabilities:
            extra = _CAP_MAP.get((cap or "").lower().strip())
            if extra:
                result.extend(extra)
        return result

    def _resolve_cmd(self, workdir: Path | None) -> str | None:
        configured = self.config.get("command") or "pi"
        found = shutil.which(configured)
        if found and not _is_system_pi(found):
            return configured
        if workdir is not None:
            local = workdir / ".clk" / "tools" / "pi" / "bin" / "pi"
            if local.exists() and os.access(local, os.X_OK):
                return str(local)
        return None

    def available(self) -> bool:
        return self._resolve_cmd(None) is not None or (
            (Path.cwd() / ".clk" / "tools" / "pi" / "bin" / "pi").exists()
        )

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            usage = estimate_tokens(req.prompt, "")
            usage["source"] = "pi-dry"
            return AgentResponse(
                ok=True,
                text=f"[pi] dry-run agent={req.agent}",
                raw={"dry_run": True},
                usage=usage,
            )
        cmd_path = self._resolve_cmd(req.workdir)
        if not cmd_path:
            return AgentResponse(ok=False, error="pi CLI not found locally or on PATH")
        args = list(self.config.get("args") or ["--print"])
        model = (self.config.get("model") or "").strip()
        if model:
            args = ["--model", model] + args
        cap_args = self.capabilities_to_args(req.capabilities or [])
        cmd = [cmd_path, *args, *cap_args]

        extra_env: dict = {}
        api_key = (self.config.get("api_key") or "").strip()
        if api_key:
            key_type = (self.config.get("key_type") or "openrouter").strip().lower()
            extra_env[_env_var_for_key_type(key_type)] = api_key

        try:
            rc, stdout, stderr = run_streaming(
                cmd,
                stdin_text=req.prompt,
                timeout_s=req.timeout_s,
                no_output_timeout_s=req.no_output_timeout_s,
                cwd=req.workdir,
                on_progress=req.on_progress,
                extra_env=extra_env,
            )
        except Exception as exc:
            print(f"[providers.pi.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))

        text = stdout or ""
        usage = estimate_tokens(req.prompt, text)
        usage["source"] = "pi-estimate"
        if rc == -1:
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s", usage=usage)
        if rc == -3:
            return AgentResponse(ok=False, error=f"no output for {req.no_output_timeout_s}s", usage=usage)
        if rc != 0:
            return AgentResponse(
                ok=False,
                text=text,
                error=(stderr or "").strip() or f"pi exited rc={rc}",
                usage=usage,
            )
        return AgentResponse(ok=True, text=text, raw={"stderr": stderr}, usage=usage)
