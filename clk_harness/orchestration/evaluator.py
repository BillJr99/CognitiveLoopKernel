"""Validation runner.

Runs a list of shell commands and returns a structured result. Used by
the loops to gate "did this iteration improve the system?".
"""

from __future__ import annotations

import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from ..utils.logging_utils import log, log_exception


@dataclass
class CheckResult:
    command: str
    ok: bool
    rc: int
    output: str


@dataclass
class EvalResult:
    ok: bool
    checks: List[CheckResult] = field(default_factory=list)

    def summary(self) -> str:
        rows = []
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            rows.append(f"{mark} rc={c.rc} :: {c.command}")
        return "\n".join(rows) or "(no checks)"


class Evaluator:
    """Run a list of shell commands as validation gates."""

    def __init__(self, root: Path, default_checks: Optional[List[str]] = None, timeout: int = 180) -> None:
        self.root = root
        self.default_checks = list(default_checks or [])
        self.timeout = timeout

    def run(self, checks: Optional[List[str]] = None) -> EvalResult:
        cmds = list(checks if checks is not None else self.default_checks)
        results: List[CheckResult] = []
        all_ok = True
        for cmd in cmds:
            try:
                r = subprocess.run(
                    cmd,
                    shell=True,
                    cwd=str(self.root),
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                )
                ok = r.returncode == 0
                results.append(
                    CheckResult(
                        command=cmd,
                        ok=ok,
                        rc=r.returncode,
                        output=((r.stdout or "") + (r.stderr or "")).strip(),
                    )
                )
                if not ok:
                    all_ok = False
            except subprocess.TimeoutExpired as exc:
                log_exception("orchestration.evaluator.run.timeout", exc)
                results.append(CheckResult(command=cmd, ok=False, rc=-1, output=f"timeout: {exc}"))
                all_ok = False
            except Exception as exc:
                log_exception("orchestration.evaluator.run", exc)
                results.append(CheckResult(command=cmd, ok=False, rc=-1, output=str(exc)))
                all_ok = False
        return EvalResult(ok=all_ok, checks=results)
