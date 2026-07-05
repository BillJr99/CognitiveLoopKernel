"""Validation runner.

Runs a list of shell commands and returns a structured result. Used by
the loops to gate "did this iteration improve the system?".
"""

from __future__ import annotations

import glob
import json
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ..utils.logging_utils import log_exception


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
    # True when the only gate available was a weak smoke (e.g. compileall)
    # because no real test command could be derived. The done-gate uses this
    # to adaptively relax its tests-green requirement for projects that have
    # no meaningful test suite (docs / research / content).
    weak: bool = False

    def summary(self) -> str:
        rows = []
        for c in self.checks:
            mark = "PASS" if c.ok else "FAIL"
            rows.append(f"{mark} rc={c.rc} :: {c.command}")
        out = "\n".join(rows) or "(no checks)"
        if self.weak:
            out += "\n(weak gate: no real test command derived)"
        return out


def derive_validation(root: Path) -> Tuple[List[str], bool]:
    """Best-effort default validation command(s) inferred from project shape.

    Returns ``(commands, weak)``. ``weak`` is True when the only thing we
    could find is a smoke check (no real test suite), so callers can surface
    ``eval=weak`` and the done-gate can adaptively relax tests-green.

    Detection order: pytest (tests/ or test_*.py) -> npm test (package.json
    with a real test script) -> pytest fallback (pyproject/setup) -> python
    compileall smoke -> always-pass weak sentinel.
    """
    root = Path(root)
    try:
        has_tests_dir = (root / "tests").is_dir()
        py_test_files = bool(
            glob.glob(str(root / "**" / "test_*.py"), recursive=True)
            or glob.glob(str(root / "**" / "*_test.py"), recursive=True)
        )
        if has_tests_dir or py_test_files:
            return (["python -m pytest -q"], False)

        pkg = root / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                test_script = ((data.get("scripts") or {}).get("test") or "").strip()
                if test_script and "no test specified" not in test_script:
                    return (["npm test --silent"], False)
            except Exception as exc:
                log_exception("orchestration.evaluator.derive_validation.pkg", exc)

        if (root / "pyproject.toml").is_file() or (root / "setup.py").is_file():
            return (["python -m pytest -q"], False)

        # No real test command — fall back to a weak smoke gate.
        if glob.glob(str(root / "**" / "*.py"), recursive=True):
            return (["python -m compileall -q ."], True)
    except Exception as exc:
        log_exception("orchestration.evaluator.derive_validation", exc)

    # Nothing better: an always-pass sentinel, explicitly weak so callers and
    # the done-gate know this was not a real evaluation.
    return (["test -d ."], True)


class Evaluator:
    """Run a list of shell commands as validation gates."""

    def __init__(
        self,
        root: Path,
        default_checks: Optional[List[str]] = None,
        timeout: int = 180,
        *,
        auto_derive: bool = False,
        derived_command: Optional[str] = None,
    ) -> None:
        self.root = root
        self.default_checks = list(default_checks or [])
        self.timeout = timeout
        self.auto_derive = auto_derive
        self.derived_command = derived_command

    def run(self, checks: Optional[List[str]] = None) -> EvalResult:
        cmds = list(checks if checks is not None else self.default_checks)
        weak = False
        # FM4: never vacuously pass. When no explicit checks were configured,
        # derive a real validation command from the project shape instead of
        # treating "no checks" as success.
        if not cmds and self.auto_derive:
            if self.derived_command:
                cmds = [self.derived_command]
            else:
                cmds, weak = derive_validation(self.root)
        if not cmds:
            # No evaluation available and auto-derive disabled: report a
            # failed (not silently-passing) gate so the loop keeps working.
            return EvalResult(ok=False, checks=[], weak=True)
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
        return EvalResult(ok=all_ok, checks=results, weak=weak)
