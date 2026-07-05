"""Per-cycle loop telemetry.

The plan->execute->evaluate->refine->iterate loop is only trustworthy if the
user can *see* it firing. CLK already logs rich events to
``.clk/logs/activity.jsonl``, but there is no per-cycle rollup. This module
provides a small thread-safe counter object that accumulates what happened in
one mission/supervise cycle and renders a single compact line plus a
``loop_cycle_summary`` activity event.

A ``CycleTelemetry`` is created once per cycle and passed down the dispatch path
via ``extra["telemetry"]``. Because parallel workflow stages share one cycle's
telemetry object, every mutating method takes a lock.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..log import get_logger
from ..utils.activity_log import log_event

logger = get_logger(__name__)


@dataclass
class CycleTelemetry:
    """Mutable, thread-safe accumulator for a single loop cycle.

    Counters are incremented from hooks already on the dispatch path
    (action application, commits, refine rounds, consensus runs, quality
    retries, no-op re-dispatches). ``eval`` / ``done_gate`` verdicts and
    ``progress`` are stamped by the loop driver before :meth:`emit`.
    """

    n: int = 0
    max_cycles: int = 0
    phase: str = ""
    workflow: str = ""

    stages_run: int = 0
    stages_ok: int = 0
    actions_applied: int = 0
    files_written: int = 0
    commits: int = 0
    refine_rounds: int = 0
    consensus_runs: int = 0
    quality_retries: int = 0
    noop_redispatches: int = 0
    qa_exchanges: int = 0

    eval_ran: bool = False
    eval_ok: Optional[bool] = None
    eval_failures: int = 0
    eval_weak: bool = False

    done_gate_requested: bool = False
    done_gate_passed: Optional[bool] = None
    done_gate_failures: List[str] = field(default_factory=list)

    progress: Optional[bool] = None
    notes: str = ""

    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    # -- counter helpers (thread-safe) -------------------------------------
    def add_actions(self, n: int = 1) -> None:
        with self._lock:
            self.actions_applied += int(n)

    def add_files(self, n: int = 1) -> None:
        with self._lock:
            self.files_written += int(n)

    def add_commit(self, n: int = 1) -> None:
        with self._lock:
            self.commits += int(n)

    def add_refine_round(self, n: int = 1) -> None:
        with self._lock:
            self.refine_rounds += int(n)

    def add_consensus_run(self, n: int = 1) -> None:
        with self._lock:
            self.consensus_runs += int(n)

    def add_quality_retry(self, n: int = 1) -> None:
        with self._lock:
            self.quality_retries += int(n)

    def add_noop_redispatch(self, n: int = 1) -> None:
        with self._lock:
            self.noop_redispatches += int(n)

    def add_qa_exchange(self, n: int = 1) -> None:
        with self._lock:
            self.qa_exchanges += int(n)

    def record_stage(self, *, ok: bool) -> None:
        with self._lock:
            self.stages_run += 1
            if ok:
                self.stages_ok += 1

    def record_eval(self, eval_result: Any, *, weak: bool = False) -> None:
        """Stamp the cycle's evaluation verdict from an ``EvalResult``."""
        with self._lock:
            self.eval_ran = True
            self.eval_weak = bool(weak)
            try:
                self.eval_ok = bool(eval_result.ok)
                self.eval_failures = sum(1 for c in eval_result.checks if not c.ok)
            except Exception:
                self.eval_ok = None
                self.eval_failures = 0

    def record_done_gate(self, verdict: Any) -> None:
        with self._lock:
            self.done_gate_requested = True
            try:
                self.done_gate_passed = bool(verdict.passed)
                self.done_gate_failures = list(verdict.failures or [])
            except Exception:
                self.done_gate_passed = None
                self.done_gate_failures = []

    # -- rendering ---------------------------------------------------------
    def as_dict(self) -> Dict[str, Any]:
        with self._lock:
            return {
                "n": self.n,
                "max_cycles": self.max_cycles,
                "phase": self.phase,
                "workflow": self.workflow,
                "stages_run": self.stages_run,
                "stages_ok": self.stages_ok,
                "actions_applied": self.actions_applied,
                "files_written": self.files_written,
                "commits": self.commits,
                "refine_rounds": self.refine_rounds,
                "consensus_runs": self.consensus_runs,
                "quality_retries": self.quality_retries,
                "noop_redispatches": self.noop_redispatches,
                "qa_exchanges": self.qa_exchanges,
                "eval_ran": self.eval_ran,
                "eval_ok": self.eval_ok,
                "eval_failures": self.eval_failures,
                "eval_weak": self.eval_weak,
                "done_gate_requested": self.done_gate_requested,
                "done_gate_passed": self.done_gate_passed,
                "done_gate_failures": list(self.done_gate_failures),
                "progress": self.progress,
                "notes": self.notes,
            }

    def render_line(self) -> str:
        """Compact one-line summary for stdout / TUI."""
        d = self.as_dict()
        cap = f"/{d['max_cycles']}" if d["max_cycles"] else ""
        parts: List[str] = [f"cycle {d['n']}{cap}"]
        if d["phase"]:
            parts.append(f"phase {d['phase']}")
        parts.append(f"stages {d['stages_run']} ({d['stages_ok']} ok)")
        parts.append(f"actions {d['actions_applied']}")
        if d["files_written"]:
            parts.append(f"files {d['files_written']}")
        if d["commits"]:
            parts.append(f"commits {d['commits']}")
        if d["refine_rounds"]:
            parts.append(f"refine {d['refine_rounds']}r")
        if d["consensus_runs"]:
            parts.append(f"consensus {d['consensus_runs']}")
        if d["qa_exchanges"]:
            parts.append(f"q&a {d['qa_exchanges']}")
        if d["eval_ran"]:
            if d["eval_ok"]:
                parts.append("eval PASS" + ("(weak)" if d["eval_weak"] else ""))
            else:
                parts.append(f"eval FAIL({d['eval_failures']})")
        if d["done_gate_requested"]:
            if d["done_gate_passed"]:
                parts.append("done-gate PASS")
            else:
                fails = ",".join(d["done_gate_failures"]) or "?"
                parts.append(f"done-gate REJECT({fails})")
        else:
            parts.append("done-gate -")
        if d["quality_retries"]:
            parts.append(f"retries {d['quality_retries']}")
        if d["noop_redispatches"]:
            parts.append(f"noop {d['noop_redispatches']}")
        return " | ".join(parts)

    def emit(self, paths: Optional[Paths], *, to_stdout: bool = True) -> str:
        """Write a ``loop_cycle_summary`` event and return the rendered line.

        ``activity.jsonl`` always receives the event (cheap); stdout printing
        is gated by ``to_stdout`` (config ``mission.telemetry_stdout``).
        """
        line = self.render_line()
        if paths is not None:
            try:
                log_event(paths, "loop_cycle_summary", **self.as_dict())
            except Exception as _exc:
                logger.debug("activity log_event failed: %s", _exc)
        if to_stdout:
            print(line, flush=True)
        return line
