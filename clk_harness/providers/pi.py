"""Pi terminal harness provider.

Pi exposes an extensible CLI; we simply pipe the prompt to ``pi`` if it
is on PATH, falling back to ``.clk/tools/pi/bin/pi`` if cloned locally.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from .base import AgentProvider, AgentRequest, AgentResponse


class PiProvider(AgentProvider):
    type_name = "pi"

    def _resolve_cmd(self, workdir: Path | None) -> str | None:
        configured = self.config.get("command") or "pi"
        if shutil.which(configured):
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
            return AgentResponse(ok=True, text=f"[pi] dry-run agent={req.agent}", raw={"dry_run": True})
        cmd_path = self._resolve_cmd(req.workdir)
        if not cmd_path:
            return AgentResponse(ok=False, error="pi CLI not found locally or on PATH")
        args = list(self.config.get("args") or [])
        cmd = [cmd_path, *args]
        try:
            r = subprocess.run(
                cmd,
                input=req.prompt,
                cwd=str(req.workdir) if req.workdir else None,
                capture_output=True,
                text=True,
                timeout=req.timeout_s,
                check=False,
            )
            if r.returncode != 0:
                return AgentResponse(
                    ok=False,
                    text=r.stdout,
                    error=r.stderr.strip() or f"pi exited rc={r.returncode}",
                )
            return AgentResponse(ok=True, text=r.stdout, raw={"stderr": r.stderr})
        except subprocess.TimeoutExpired as exc:
            print(f"[providers.pi.invoke] timeout: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s")
        except Exception as exc:
            print(f"[providers.pi.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))
