"""OpenAI Codex CLI provider."""

from __future__ import annotations

import shutil
import subprocess
import sys
import traceback

from .base import AgentProvider, AgentRequest, AgentResponse


class CodexProvider(AgentProvider):
    type_name = "codex"

    def _cmd(self) -> str:
        return self.config.get("command") or "codex"

    def available(self) -> bool:
        return shutil.which(self._cmd()) is not None

    def invoke(self, req: AgentRequest) -> AgentResponse:
        if req.dry_run:
            return AgentResponse(ok=True, text=f"[codex] dry-run agent={req.agent}", raw={"dry_run": True})
        if not self.available():
            return AgentResponse(ok=False, error=f"codex CLI not found ({self._cmd()})")

        args = list(self.config.get("args") or ["exec"])
        cmd = [self._cmd(), *args, req.prompt]
        try:
            r = subprocess.run(
                cmd,
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
                    error=r.stderr.strip() or f"codex exited rc={r.returncode}",
                )
            return AgentResponse(ok=True, text=r.stdout, raw={"stderr": r.stderr})
        except subprocess.TimeoutExpired as exc:
            print(f"[providers.codex.invoke] timeout: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=f"timeout after {req.timeout_s}s")
        except Exception as exc:
            print(f"[providers.codex.invoke] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return AgentResponse(ok=False, error=str(exc))
