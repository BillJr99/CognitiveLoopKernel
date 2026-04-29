"""Claude Code provider.

Drives the ``claude`` CLI in non-interactive print mode. The CLI is
detected via ``which`` so the rest of the harness can degrade
gracefully when it is not installed.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import traceback
from pathlib import Path

from .base import AgentProvider, AgentRequest, AgentResponse


class ClaudeProvider(AgentProvider):
    type_name = "claude"

    def _cmd(self) -> str:
        return self.config.get("command") or "claude"

    def available(self) -> bool:
        return shutil.which(self._cmd()) is not None

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            return AgentResponse(ok=True, text=f"[claude] dry-run agent={req.agent}", raw={"dry_run": True})
        if not self.available():
            return AgentResponse(ok=False, error=f"claude CLI not found ({self._cmd()})")

        cmd = [self._cmd(), *(self.config.get("args") or ["--print"])]
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
                    error=r.stderr.strip() or f"claude exited rc={r.returncode}",
                )
            return AgentResponse(ok=True, text=r.stdout, raw={"stderr": r.stderr})
        except subprocess.TimeoutExpired as exc:
            print(f"[providers.claude.invoke] timeout: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s")
        except Exception as exc:
            print(f"[providers.claude.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))
