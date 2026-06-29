"""Workflow parser and runner (Archon-style YAML).

A workflow file looks like:

    name: engineering
    description: Single development cycle.
    stages:
      - id: decompose
        agent: chief
        objective: Decompose the current top-level objective.
      - id: research
        agent: researcher
        objective: Investigate open assumptions.
        depends_on: [decompose]
        validation: "echo OK"
        commit: true
"""

from __future__ import annotations

import re
import shlex
import subprocess
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import Paths
from ..git_ops import (
    add_all,
    commit as git_commit,
    commit_trace,
    has_changes,
    head_sha,
    revert_to,
    snapshot_rollback,
)
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from . import blackboard as _blackboard
from . import response_quality as _response_quality
from . import noop_guard as _noop_guard
from . import deliberation as _deliberation
from . import done_gate as _done_gate
from . import evaluator as _evaluator
from . import charter as _charter
from .agent import AgentRunner, AgentRun
from .telemetry import CycleTelemetry


_ROUND_STATUS_RE = re.compile(r"^\s*ROUND_STATUS\s*:\s*(continue|done|finished)\s*$", re.IGNORECASE | re.MULTILINE)


def _round_status(text: str) -> str:
    """Return 'continue' or 'done'. Default 'done' when no marker found."""
    if not text:
        return "done"
    matches = _ROUND_STATUS_RE.findall(text)
    if not matches:
        return "done"
    last = matches[-1].lower()
    return "continue" if last == "continue" else "done"


try:
    import yaml  # type: ignore
except Exception:
    # PyYAML is optional. The mini-YAML loader below covers the workflow
    # subset CLK uses, so we silently fall back rather than spraying a
    # warning across stderr (which would also corrupt the TUI).
    yaml = None


def is_provider_failure(error: str) -> bool:
    """Return True for failures a downstream agent cannot fix."""
    msg = (error or "").lower()
    patterns = [
        "no endpoints available",
        "guardrail restrictions",
        "data policy",
        "api key",
        "cli not found",
        "not found",
        "authentication",
        "unauthorized",
        "forbidden",
        "rate limit",
        "quota",
        "operation was aborted",
        "timeout after",
        "no output for",
    ]
    return any(p in msg for p in patterns)


def _mini_yaml_loads(text: str) -> Dict[str, Any]:
    """Minimal YAML loader for the workflow subset used by CLK.

    Supports:
      * top-level scalar keys (``key: value``)
      * a single ``stages:`` key whose value is a list of dicts
      * each list item begins with ``- key: value`` then ``key: value`` lines
      * inline lists like ``[a, b]`` and booleans (``true``/``false``)
      * quoted scalar values

    This is *not* a general YAML parser - it handles exactly what the
    bundled workflows use. Keeping it local avoids a hard dependency on
    PyYAML when ``ensurepip`` is unavailable.
    """

    def parse_scalar(raw: str) -> Any:
        s = raw.strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [parse_scalar(p) for p in _split_csv(inner)]
        low = s.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "~"):
            return None
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s

    def _split_csv(s: str) -> List[str]:
        out: List[str] = []
        buf = ""
        depth = 0
        in_quote: Optional[str] = None
        for ch in s:
            if in_quote:
                buf += ch
                if ch == in_quote:
                    in_quote = None
                continue
            if ch in ("'", '"'):
                in_quote = ch
                buf += ch
                continue
            if ch == "[":
                depth += 1
                buf += ch
                continue
            if ch == "]":
                depth -= 1
                buf += ch
                continue
            if ch == "," and depth == 0:
                out.append(buf.strip())
                buf = ""
                continue
            buf += ch
        if buf.strip():
            out.append(buf.strip())
        return out

    def continuation(start: int, base_indent: int) -> Tuple[str, int]:
        parts: List[str] = []
        j = start
        while j < len(lines):
            line = lines[j]
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent <= base_indent:
                break
            if stripped.startswith("- "):
                break
            if ":" in stripped:
                break
            parts.append(stripped)
            j += 1
        return " ".join(parts).strip(), j

    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
    result: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(" "):  # unexpected at top level, skip
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val:
            result[key] = parse_scalar(val)
            i += 1
            extra, ni = continuation(i, 0)
            if extra and isinstance(result[key], str):
                result[key] = f"{result[key]} {extra}".strip()
                i = ni
            continue
        # value on subsequent indented lines
        i += 1
        if i < len(lines) and lines[i].lstrip().startswith("- "):
            items: List[Any] = []
            cur: Optional[Dict[str, Any]] = None
            while i < len(lines) and lines[i].startswith(" "):
                sub = lines[i]
                stripped = sub.lstrip()
                if stripped.startswith("- "):
                    if cur is not None:
                        items.append(cur)
                    cur = {}
                    rest = stripped[2:]
                    if ":" in rest:
                        k2, _, v2 = rest.partition(":")
                        k2 = k2.strip()
                        cur[k2] = parse_scalar(v2)
                        i += 1
                        extra, ni = continuation(i, len(sub) - len(stripped))
                        if extra and isinstance(cur[k2], str):
                            cur[k2] = f"{cur[k2]} {extra}".strip()
                            i = ni
                        continue
                else:
                    if cur is None:
                        cur = {}
                    if ":" in stripped:
                        k2, _, v2 = stripped.partition(":")
                        k2 = k2.strip()
                        cur[k2] = parse_scalar(v2)
                        i += 1
                        extra, ni = continuation(i, len(sub) - len(stripped))
                        if extra and isinstance(cur[k2], str):
                            cur[k2] = f"{cur[k2]} {extra}".strip()
                            i = ni
                        continue
                i += 1
            if cur is not None:
                items.append(cur)
            result[key] = items
        else:
            # nested mapping not used by our workflow format; collect raw
            buf: List[str] = []
            while i < len(lines) and lines[i].startswith(" "):
                buf.append(lines[i])
                i += 1
            result[key] = "\n".join(buf)
    return result


@dataclass
class WorkflowStage:
    id: str
    agent: str
    objective: str
    depends_on: List[str] = field(default_factory=list)
    validation: Optional[str] = None
    commit: bool = True
    # Blackboard contract: ``inputs`` selectors filter the blackboard
    # digest spliced into the worker's prompt; ``outputs`` are contract
    # keys the worker promises to satisfy via POST blocks. Missing
    # outputs surface as a warning (not a hard failure) post-run so
    # downstream stages can still attempt their work.
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    # Phase tag. ``review`` makes this a chief-review stage that auto-
    # digests upstream post-output and asks the chief to decide
    # CONTINUE / REDIRECT / ABORT. Unknown phases are passed through to
    # the worker prompt as-is for downstream behaviors to interpret.
    phase: str = ""
    # When > 1, the stage runs in turn-based rounds: after each round
    # the runner refreshes the worker's prompt with any new blackboard
    # posts (including those from sibling parallel workers) and re-
    # dispatches. The worker stops the loop early by emitting
    # ``ROUND_STATUS: done`` (default), or requests another round with
    # ``ROUND_STATUS: continue``.
    rounds: int = 1
    # Tag for sensitive stages. When true, the runner runs an extra
    # chief checkpoint after the stage completes (CONTINUE / REDIRECT /
    # ABORT) AND uses meta-prompt drafting on dispatch when configured.
    careful: bool = False
    # Critic-judge inner refinement loop. When present, after the
    # worker's first response the harness dispatches the named critic
    # agent to score the response 0..1; if below
    # ``accept_threshold`` the worker is re-dispatched with the critic's
    # feedback, up to ``max_rounds`` total worker dispatches. ``None``
    # means "use the default policy from clk.config.json::robustness".
    refine: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    name: str
    description: str
    stages: List[WorkflowStage]


def load_workflow(path: Path) -> Workflow:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.pyyaml", exc)
            try:
                data = _mini_yaml_loads(text)
            except Exception:
                data = {}
    else:
        try:
            data = _mini_yaml_loads(text)
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.fallback", exc)
            raise

    # If parsing produced nothing usable (e.g. the chief wrote a
    # malformed workflow that wedges every subsequent supervise cycle),
    # restore the bundled template for this workflow name so the runner
    # has stages to execute on the next pass.
    if not isinstance(data, dict) or not data.get("stages"):
        try:
            from ..templates.workflows import WORKFLOWS as _BUNDLED_WORKFLOWS
        except Exception:
            _BUNDLED_WORKFLOWS = {}
        fallback = _BUNDLED_WORKFLOWS.get(path.name)
        if fallback:
            try:
                path.write_text(fallback, encoding="utf-8")
            except Exception as exc:
                log_exception("orchestration.workflow.load_workflow.restore", exc)
            if yaml is not None:
                try:
                    data = yaml.safe_load(fallback) or {}
                except Exception:
                    data = _mini_yaml_loads(fallback)
            else:
                data = _mini_yaml_loads(fallback)

    stages: List[WorkflowStage] = []
    for raw in data.get("stages") or []:
        try:
            rounds = int(raw.get("rounds") or 1)
        except (TypeError, ValueError):
            rounds = 1
        refine_raw = raw.get("refine")
        if isinstance(refine_raw, dict):
            refine_cfg: Optional[Dict[str, Any]] = dict(refine_raw)
        elif refine_raw in (True, "true", "yes", 1):
            refine_cfg = {}
        else:
            refine_cfg = None
        stages.append(
            WorkflowStage(
                id=str(raw.get("id") or raw.get("agent") or "stage"),
                agent=str(raw.get("agent") or "engineer"),
                objective=str(raw.get("objective") or ""),
                depends_on=list(raw.get("depends_on") or []),
                validation=raw.get("validation"),
                commit=bool(raw.get("commit", True)),
                inputs=list(raw.get("inputs") or []),
                outputs=list(raw.get("outputs") or []),
                phase=str(raw.get("phase") or "").strip().lower(),
                rounds=max(1, rounds),
                careful=bool(raw.get("careful") or False),
                refine=refine_cfg,
                metadata=dict(raw.get("metadata") or {}),
            )
        )
    return Workflow(
        name=str(data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        stages=stages,
    )


@dataclass
class StageResult:
    stage: WorkflowStage
    run: AgentRun
    validated: bool
    validation_output: str = ""
    committed: bool = False
    failure_reason: str = ""  # filled when ok=False or validated=False


class WorkflowRunner:
    def __init__(self, paths: Paths, runner: AgentRunner) -> None:
        self.paths = paths
        self.runner = runner
        # When set by the MissionRunner, the per-cycle telemetry object is
        # threaded into each stage's dispatch extra so the dispatch-path hooks
        # accumulate into the active cycle. When None, ``run`` creates one per
        # supervise cycle so standalone ``clk run`` is observable too.
        self.telemetry: Optional[CycleTelemetry] = None
        # When True, producing dispatches get the deliberation preamble and
        # the done-gate / phase semantics lean toward unattended autonomy.
        self.mission_mode: bool = False
        # When the MissionRunner drives phases, it owns the outer loop, so it
        # sets this to 1 to make each WorkflowRunner.run() a single pass.
        self.supervise_cycles_override: Optional[int] = None

    # Default cap on chief recovery dispatches per stage. A stage that
    # still has unmet deps after this many recovery passes gets a final
    # WARN and is skipped, so we never loop forever on a stuck workflow.
    # Overridable via clk.config.json::recovery::max_per_stage.
    DEFAULT_MAX_RECOVERY_PER_STAGE = 3

    @property
    def max_recovery_per_stage(self) -> int:
        cfg = (self.runner.clk_cfg.get("recovery") or {})
        return int(cfg.get("max_per_stage") or self.DEFAULT_MAX_RECOVERY_PER_STAGE)

    # Per-stage retry cap for provider errors (separate from recovery_count
    # which handles unmet deps). Uses exponential backoff starting at
    # stage_backoff_s. Overridable via clk.config.json::provider_retry.
    DEFAULT_MAX_STAGE_RETRIES = 2
    DEFAULT_STAGE_BACKOFF_S = 30.0

    @property
    def max_stage_retries(self) -> int:
        cfg = (self.runner.clk_cfg.get("provider_retry") or {})
        return int(cfg.get("stage_max_retries", self.DEFAULT_MAX_STAGE_RETRIES) or self.DEFAULT_MAX_STAGE_RETRIES)

    @property
    def stage_backoff_s(self) -> float:
        cfg = (self.runner.clk_cfg.get("provider_retry") or {})
        return float(cfg.get("stage_backoff_s", self.DEFAULT_STAGE_BACKOFF_S) or self.DEFAULT_STAGE_BACKOFF_S)

    DEFAULT_MAX_SUPERVISE_CYCLES = 20

    @property
    def max_supervise_cycles(self) -> int:
        if self.supervise_cycles_override is not None:
            return int(self.supervise_cycles_override)
        cfg = (self.runner.clk_cfg.get("supervise") or {})
        return int(cfg.get("max_cycles") or self.DEFAULT_MAX_SUPERVISE_CYCLES)

    @property
    def max_consecutive_no_progress(self) -> int:
        cfg = (self.runner.clk_cfg.get("supervise") or {})
        return int(cfg.get("max_consecutive_no_progress") or 8)

    @property
    def stall_rescue_enabled(self) -> bool:
        """When True, hitting the no-progress cap dispatches the chief once
        in *rescue mode* (restructure the plan or declare done) before the
        loop gives up. Overridable via clk.config.json::supervise::stall_rescue.
        """
        cfg = (self.runner.clk_cfg.get("supervise") or {})
        val = cfg.get("stall_rescue", True)
        return str(val).lower() not in ("false", "0", "off", "no")

    def _should_rollback(self, stage: WorkflowStage) -> bool:
        """Whether a failed validation hard-resets the stage's work.

        Policy via clk.config.json::validation::rollback_on_failure:
        ``never`` keeps the work; ``careful`` (default) rolls back only
        stages marked careful=true; ``always`` is the legacy behavior.
        """
        cfg = (self.runner.clk_cfg.get("validation") or {})
        policy = str(cfg.get("rollback_on_failure", "careful")).lower()
        if policy == "always":
            return True
        if policy == "never":
            return False
        return bool(stage.careful)

    # -- done gate (FM2) ---------------------------------------------------

    def _done_gate_enabled(self) -> bool:
        cfg = (self.runner.clk_cfg.get("done_gate") or {})
        return bool(cfg.get("enabled", True))

    def _telemetry_stdout(self) -> bool:
        cfg = (self.runner.clk_cfg.get("mission") or {})
        return bool(cfg.get("telemetry_stdout", True))

    def _evaluate_done_gate(self) -> "_done_gate.DoneGateVerdict":
        """Build a real eval result + charter criteria and run the done gate."""
        val_cfg = (self.runner.clk_cfg.get("validation") or {})
        evaluator = _evaluator.Evaluator(
            root=self.paths.root,
            default_checks=list(self.runner.clk_cfg.get("validation_checks") or []),
            auto_derive=bool(val_cfg.get("auto_derive", True)),
            derived_command=val_cfg.get("derived_command"),
        )
        try:
            eval_result = evaluator.run()
        except Exception as exc:
            log_exception("orchestration.workflow._evaluate_done_gate.eval", exc)
            eval_result = None
        try:
            charter = _charter.load_charter(self.paths)
            extra_criteria = _charter.derive_done_criteria(charter)
        except Exception:
            extra_criteria = []
        return _done_gate.evaluate_done_gate(
            self.paths, self.runner.clk_cfg, eval_result, extra_criteria=extra_criteria,
        )

    def _stop_requested(self, workflow: Workflow) -> bool:
        """Whether the loop may stop now.

        ``done_granted.md`` (written only by the gate) is the authoritative
        stop signal. A bare ``done.md`` is an agent *request*: when the gate
        is enabled it is honored only if every completion criterion passes,
        otherwise it is downgraded so the loop keeps working. When the gate
        is disabled, ``done.md`` stops the loop as it always did.
        """
        state = self.paths.state
        if (state / "done_granted.md").exists():
            return True
        done_md = state / "done.md"
        if not done_md.exists():
            return False
        if not self._done_gate_enabled():
            return True
        verdict = self._evaluate_done_gate()
        if self.telemetry is not None:
            try:
                self.telemetry.record_done_gate(verdict)
            except Exception:
                pass
        if verdict.passed:
            self._grant_done(verdict)
            return True
        # Reject: downgrade the request so a later cycle can re-earn it.
        try:
            done_md.rename(state / "done_requested.md")
        except Exception:
            try:
                done_md.unlink()
            except Exception:
                pass
        log(
            f"workflow {workflow.name}: ACTION:done REJECTED by done-gate — "
            f"unmet: {', '.join(verdict.failures) or '?'}",
            level="WARN",
        )
        log_event(
            self.paths,
            "done_gate_rejected",
            workflow=workflow.name,
            failures=list(verdict.failures),
            checked=verdict.checked,
        )
        return False

    def _grant_done(self, verdict: "_done_gate.DoneGateVerdict") -> None:
        try:
            (self.paths.state / "done_granted.md").write_text(
                "# Mission complete\n\n" + verdict.summary() + "\n",
                encoding="utf-8",
            )
        except Exception as exc:
            log_exception("orchestration.workflow._grant_done", exc)
        log_event(self.paths, "done_gate_granted", checked=verdict.checked)
        try:
            commit_trace(
                self.paths.root,
                kind="done",
                summary="done-gate granted",
                meta={"checked": list(verdict.checked.keys())},
            )
        except Exception:
            pass

    def run(self, workflow: Workflow, *, dry_run: Optional[bool] = None) -> List[StageResult]:
        """Execute the workflow, looping it under chief supervision.

        Three dynamic behaviors:

        1. After each stage we re-check the workflow file's mtime: if a
           PROPOSE_WORKFLOW block rewrote it, new stages are spliced in
           for the remainder of this cycle.

        2. If a stage's deps are unmet (because an earlier stage failed
           or was skipped), we dispatch the chief in *recovery mode* to
           either re-cast the workflow, run a remediation stage, or
           explicitly accept the gap. After recovery we re-check deps
           and retry the stage. Capped at ``MAX_RECOVERY_PER_STAGE``.

        3. When the workflow finishes without ``.clk/state/done.md``
           existing, we loop and run the workflow again (the chief's
           supervise stage may have rewritten it via PROPOSE_WORKFLOW).
           This way no agent is ever truly "done" until the chief signals
           ACTION:done. Capped at ``DEFAULT_MAX_SUPERVISE_CYCLES``.
        """
        all_results: List[StageResult] = []
        stopped_for_provider_failure = False
        stopped_done = False
        no_progress = 0
        rescue_attempted = False
        for cycle in range(1, self.max_supervise_cycles + 1):
            if self._stop_requested(workflow):
                log(f"workflow {workflow.name}: stop granted, ending supervise loop")
                stopped_done = True
                break

            cancel_file = self.paths.state / "cancel_requested.txt"
            if cancel_file.exists():
                try:
                    cancel_file.unlink()
                except Exception:
                    pass
                log(f"workflow {workflow.name}: graceful cancel requested; stopping after cycle {cycle - 1}")
                break

            if cycle > 1:
                log(f"workflow {workflow.name}: supervise cycle {cycle}/{self.max_supervise_cycles}")
            # Per-cycle telemetry: when the MissionRunner owns one it is set on
            # self.telemetry already; otherwise create one for this cycle so
            # standalone `clk run` is observable too (FM5).
            owns_telemetry = self.telemetry is None
            if owns_telemetry:
                self.telemetry = CycleTelemetry(
                    n=cycle, max_cycles=self.max_supervise_cycles, workflow=workflow.name,
                )
            try:
                refreshed = load_workflow(self.paths.workflows / f"{workflow.name}.yaml")
            except Exception:
                refreshed = workflow
            cycle_results = self._run_once(refreshed, dry_run=dry_run, cycle=cycle)
            all_results.extend(cycle_results)

            # Check for no-progress. Two signals combine:
            #   * material progress — a commit or file write happened
            #   * self-report — agents end responses with PROGRESS: yes/no;
            #     when every reporting agent says "no", the cycle counts as
            #     stalled even if files were technically touched (busywork).
            material = any(
                r.committed or bool(r.run.files_written)
                for r in cycle_results
                if r.run.response.ok
            )
            signals = [
                _response_quality.progress_signal(r.run.response.text)
                for r in cycle_results
                if r.run.response.ok
            ]
            explicit = [s for s in signals if s is not None]
            self_reported_stall = bool(explicit) and not any(explicit)
            progress = material and not self_reported_stall
            # Emit the per-cycle telemetry line (FM5). When the MissionRunner
            # owns the telemetry object it records eval/done-gate and emits
            # itself, so only emit here for standalone supervise cycles.
            if owns_telemetry and self.telemetry is not None:
                self.telemetry.progress = progress
                self.telemetry.emit(self.paths, to_stdout=self._telemetry_stdout())
                self.telemetry = None
            if not progress:
                no_progress += 1
                why = "agents reported PROGRESS: no" if (material and self_reported_stall) else "no commits or file writes"
                log(
                    f"workflow {workflow.name}: cycle {cycle} made no progress — {why} "
                    f"({no_progress}/{self.max_consecutive_no_progress})",
                    level="WARN" if no_progress >= 2 else "INFO",
                )
                if no_progress >= self.max_consecutive_no_progress:
                    if self.stall_rescue_enabled and not rescue_attempted and not dry_run:
                        rescue_attempted = True
                        no_progress = 0
                        self._dispatch_stall_rescue(workflow, cycle, cycle_results)
                        if self._stop_requested(workflow):
                            log(f"workflow {workflow.name}: stop granted during stall rescue")
                            stopped_done = True
                            break
                        continue
                    log(
                        f"workflow {workflow.name}: stopping after {no_progress} consecutive "
                        "no-progress cycles (set supervise.max_consecutive_no_progress to change)",
                        level="ERROR",
                    )
                    log_event(self.paths, "workflow_stalled", workflow=workflow.name,
                              no_progress_cycles=no_progress,
                              rescue_attempted=rescue_attempted)
                    break
            else:
                no_progress = 0

            if any(self._is_provider_failure((r.run.response.error or "")) for r in cycle_results if not r.run.response.ok):
                log(
                    f"workflow {workflow.name}: stopping supervise cycles after provider failure",
                    level="ERROR",
                )
                stopped_for_provider_failure = True
                break
            if dry_run:
                break
        if (
            not stopped_for_provider_failure
            and not stopped_done
            and not (self.paths.state / "done_granted.md").exists()
            and self.max_supervise_cycles > 1
        ):
            log(
                f"workflow {workflow.name}: supervise cycle limit reached "
                f"({self.max_supervise_cycles}); type /run to continue or set "
                "supervise.max_cycles in clk.config.json",
                level="WARN",
            )
        return all_results

    def _run_once(self, workflow: Workflow, *, dry_run: Optional[bool] = None, cycle: int = 1) -> List[StageResult]:
        """Single pass through the workflow; stages with no inter-dependencies run in parallel."""
        log(f"workflow start: {workflow.name} ({len(workflow.stages)} stages)")
        results: List[StageResult] = []
        completed: Dict[str, bool] = {}
        result_by_id: Dict[str, StageResult] = {}
        wf_path = self.paths.workflows / f"{workflow.name}.yaml"
        wf_mtime = wf_path.stat().st_mtime if wf_path.exists() else 0.0

        stages = list(workflow.stages)
        recovery_count: Dict[str, int] = {}
        stage_retry_count: Dict[str, int] = {}
        dispatched: Set[str] = set()  # stage ids sent to runner this pass

        max_cycles = self.max_supervise_cycles
        cycle_context = f"Supervise cycle {cycle}/{max_cycles} — {max_cycles - cycle + 1} remaining."

        while True:
            # Stages not yet dispatched and not yet in completed
            pending = [s for s in stages if s.id not in dispatched and s.id not in completed]
            if not pending:
                break

            ready = [s for s in pending if not self._unmet_deps(s, completed)]
            blocked = [s for s in pending if self._unmet_deps(s, completed)]

            if not ready:
                # Every remaining stage has unmet deps — recovery dispatch for the first one
                stage = blocked[0]
                unmet = self._unmet_deps(stage, completed)
                tries = recovery_count.get(stage.id, 0)
                if tries >= self.max_recovery_per_stage or dry_run:
                    self._log_skip(stage, unmet, result_by_id)
                    completed[stage.id] = False
                    dispatched.add(stage.id)
                    continue
                recovery_count[stage.id] = tries + 1
                self._dispatch_recovery(
                    workflow, stage, unmet, result_by_id,
                    dry_run=dry_run, cycle_context=cycle_context,
                )
                stages, wf_mtime = self._refresh_from_dispatched(
                    workflow.name, wf_path, wf_mtime, stages,
                    dispatched | set(completed),
                )
                continue

            # Mark ready stages as dispatched before running so re-entrant
            # refreshes don't double-dispatch them.
            for s in ready:
                dispatched.add(s.id)

            if len(ready) > 1:
                log(
                    f"workflow {workflow.name}: parallel batch "
                    f"[{', '.join(s.id for s in ready)}]"
                )
            else:
                log(f"stage {ready[0].id} -> agent {ready[0].agent}")

            # Run: parallel when multiple independent stages are ready
            if len(ready) == 1 or dry_run:
                batch = [self._run_stage(ready[0], workflow, cycle_context, dry_run, result_by_id)]
            else:
                with ThreadPoolExecutor(max_workers=len(ready)) as pool:
                    fmap = {
                        pool.submit(self._run_stage, s, workflow, cycle_context, dry_run, result_by_id): s
                        for s in ready
                    }
                    batch = [fut.result() for fut in as_completed(fmap)]

            abort = False
            for sr in batch:
                ok = sr.run.response.ok
                if not ok and self._is_provider_failure(sr.run.response.error or ""):
                    error_msg = sr.run.response.error or ""
                    st = stage_retry_count.get(sr.stage.id, 0) + 1
                    stage_retry_count[sr.stage.id] = st
                    if self._is_retryable_stage_error(error_msg) and st <= self.max_stage_retries:
                        wait = self.stage_backoff_s * (2 ** (st - 1))
                        log(
                            f"workflow {workflow.name}: stage {sr.stage.id} retryable error "
                            f"(attempt {st}/{self.max_stage_retries}): {error_msg!r}; "
                            f"backing off {wait:.0f}s",
                            level="WARN",
                        )
                        log_event(
                            self.paths, "workflow_stage_retry",
                            agent=sr.stage.agent, workflow=workflow.name,
                            stage_id=sr.stage.id, attempt=st,
                            max_retries=self.max_stage_retries,
                            backoff_s=wait, error=error_msg,
                        )
                        if self.runner.observer is not None:
                            try:
                                self.runner.observer.progress(
                                    sr.stage.agent, "retry",
                                    f"stage {sr.stage.id} backing off {wait:.0f}s "
                                    f"(attempt {st}/{self.max_stage_retries}): {error_msg}",
                                )
                            except Exception:
                                pass
                        dispatched.discard(sr.stage.id)
                        time.sleep(wait)
                        continue  # retry: don't add to results/completed
                    log(
                        f"workflow {workflow.name}: aborting after provider failure "
                        f"in stage {sr.stage.id} (retries exhausted): {error_msg}",
                        level="ERROR",
                    )
                    log_event(
                        self.paths, "workflow_aborted",
                        agent=sr.stage.agent, workflow=workflow.name,
                        stage_id=sr.stage.id, reason="provider_failure", error=error_msg,
                    )
                    results.append(sr)
                    result_by_id[sr.stage.id] = sr
                    completed[sr.stage.id] = False
                    abort = True
                else:
                    results.append(sr)
                    result_by_id[sr.stage.id] = sr
                    completed[sr.stage.id] = ok and sr.validated

            if abort:
                break

            stages, wf_mtime = self._refresh_from_dispatched(
                workflow.name, wf_path, wf_mtime, stages,
                dispatched | set(completed),
            )

        log(f"workflow done: {workflow.name}")
        return results

    def _run_stage(
        self,
        stage: WorkflowStage,
        workflow: Workflow,
        cycle_context: str,
        dry_run: Optional[bool],
        result_by_id: Optional[Dict[str, "StageResult"]] = None,
    ) -> StageResult:
        """Run a single stage and return its result.

        Handles all the new stage semantics on top of the basic single-
        dispatch path: inputs (blackboard filtering), outputs (contract
        verification), phase=review (chief review prompt synthesis),
        rounds>1 (turn-based re-dispatch with refreshed digest), and
        careful=True (extra chief checkpoint after the run).
        """
        result_by_id = result_by_id or {}

        pre_stage_sha: Optional[str] = head_sha(self.paths.root) if stage.commit and not dry_run else None

        # Build objective: chief-review stages get a synthesized prompt
        # that includes the upstream stages' blackboard posts.
        if stage.phase == "review" and stage.depends_on:
            objective = self._build_review_objective(workflow, stage, result_by_id)
        else:
            objective = stage.objective

        # Optional meta-prompt drafting for sensitive (careful) stages or
        # when meta_prompt.dispatch is "always". The chief is asked to
        # tighten the worker's task prompt; result is cached on disk.
        if (
            not dry_run
            and stage.agent != "chief"
            and stage.phase != "review"
            and self._meta_dispatch_enabled(stage)
        ):
            try:
                drafted = self.runner.meta_draft_dispatch_prompt(
                    agent_name=stage.agent,
                    base_objective=objective,
                    blackboard_inputs=list(stage.inputs),
                    stage_outputs=list(stage.outputs),
                )
                if drafted:
                    objective = drafted
            except Exception as exc:
                log_exception("orchestration.workflow._run_stage.meta_dispatch", exc)

        # Inputs filter the blackboard digest. Review stages auto-include
        # all posts from the stages they depend on.
        bb_inputs = list(stage.inputs)
        if stage.phase == "review" and not bb_inputs:
            bb_inputs = [f"stage:{d}" for d in stage.depends_on]

        base_extra: Dict[str, Any] = {
            "stage_id": stage.id,
            "workflow": workflow.name,
            "cycle_context": cycle_context,
            "blackboard_inputs": bb_inputs,
            "stage_outputs": list(stage.outputs),
            # Carried for the no-op guard (commit=producing) and the telemetry
            # hooks on the dispatch path.
            "commit": bool(stage.commit),
            "telemetry": self.telemetry,
        }
        if stage.phase:
            base_extra["phase"] = stage.phase

        # Deliberation: in mission mode, prepend the self-reflect + ask-peers
        # preamble to producing dispatches so the team "thinks" before acting.
        if (
            not dry_run
            and self.mission_mode
            and stage.phase != "review"
            and _deliberation.enabled(self.runner.clk_cfg)
            and _noop_guard.is_mutation_expected(
                stage.agent, outputs=stage.outputs, commit=stage.commit,
                cfg=self.runner.clk_cfg,
            )
        ):
            preamble = _deliberation.dispatch_preamble(self.runner.clk_cfg)
            if preamble:
                objective = preamble + objective

        stop_when_file = self.paths.state / "stop_when.txt"
        stop_when = stop_when_file.read_text(encoding="utf-8").strip() if stop_when_file.exists() else ""
        if stop_when:
            base_extra["stop_when"] = stop_when

        # Turn-based rounds: keep dispatching until the worker emits
        # ROUND_STATUS: done (or absent), or the round cap is reached.
        rounds_max = max(1, int(stage.rounds or 1))
        run: Optional[AgentRun] = None
        for round_idx in range(1, rounds_max + 1):
            extra = dict(base_extra)
            extra["round"] = round_idx
            extra["rounds_total"] = rounds_max
            if round_idx == 1:
                round_objective = objective
            else:
                round_objective = (
                    f"Round {round_idx}/{rounds_max} of stage `{stage.id}`.\n\n"
                    "Sibling agents may have posted to the blackboard since your last "
                    "round; the digest above has the latest. Continue your work, "
                    "post new findings via POST blocks, and emit `ROUND_STATUS: done` "
                    "in your final round (or `ROUND_STATUS: continue` to request "
                    "another round before the cap).\n\n"
                    f"Original objective:\n{objective}"
                )
            run = self.runner.run(
                stage.agent,
                round_objective,
                extra=extra,
                dry_run=dry_run,
            )
            if rounds_max == 1:
                break
            status = _round_status(run.response.text or "")
            log_event(
                self.paths,
                "workflow_round_complete",
                agent=stage.agent,
                workflow=workflow.name,
                stage_id=stage.id,
                round=round_idx,
                rounds_total=rounds_max,
                round_status=status,
                ok=run.response.ok,
            )
            if status == "done" or not run.response.ok:
                break

        assert run is not None  # the loop runs at least once

        # Critic-judge refinement loop. When the stage opts in
        # (explicit ``refine:`` block or careful=true under the default
        # auto_refine policy), dispatch a critic agent to score the
        # response; if the critic says revise, re-dispatch the worker
        # with the critic's feedback until accept or max_rounds.
        if not dry_run and run.response.ok and self._debate_enabled(stage):
            # Adversarial debate panel takes precedence over the single critic.
            try:
                run = self._debate_loop(workflow, stage, run, cycle_context, dry_run)
            except Exception as exc:
                log_exception("orchestration.workflow._run_stage.debate", exc)
        elif not dry_run and run.response.ok and self._refine_enabled(stage):
            try:
                run = self._refine_loop(workflow, stage, run, cycle_context, dry_run)
            except Exception as exc:
                log_exception("orchestration.workflow._run_stage.refine", exc)

        ok = run.response.ok
        if dry_run:
            v_ok, v_out = True, "(dry-run: validation skipped)"
        else:
            v_ok, v_out = self._validate(stage)

        # Outputs contract: warn when the stage's promised POST keys never
        # landed, then give the chief one recovery pass to fill the gap
        # (re-dispatch the worker, post a substitute, or accept it) so
        # downstream stages don't silently consume missing inputs.
        unmet_outputs = self._check_outputs_contract(stage)
        if unmet_outputs:
            log(
                f"workflow {workflow.name}: stage {stage.id} did not satisfy "
                f"declared outputs: {unmet_outputs}",
                level="WARN",
            )
            log_event(
                self.paths,
                "workflow_outputs_unmet",
                agent=stage.agent,
                workflow=workflow.name,
                stage_id=stage.id,
                expected=list(stage.outputs),
                missing=list(unmet_outputs),
            )
            # Only when the stage otherwise succeeded: a failed response or
            # failed validation already keeps the stage incomplete (and may
            # roll back), so a recovery pass here couldn't unblock anything.
            if (
                not dry_run
                and ok
                and v_ok
                and stage.agent != "chief"
                and self._outputs_recovery_enabled
            ):
                try:
                    self._dispatch_outputs_recovery(
                        workflow, stage, unmet_outputs, cycle_context, dry_run
                    )
                    # Re-check: the chief may have posted the missing keys
                    # (or had the worker do it) during the recovery pass.
                    unmet_outputs = self._check_outputs_contract(stage)
                except Exception as exc:
                    log_exception("orchestration.workflow._run_stage.outputs_recovery", exc)

        committed = False
        if run.committed:
            committed = True
        elif ok and v_ok and stage.commit and not dry_run:
            committed = self._commit(workflow, stage, run, v_out)

        if not v_ok and pre_stage_sha and not dry_run:
            if self._should_rollback(stage):
                log(f"stage {stage.id}: validation failed; rolling back to {pre_stage_sha[:8]}", level="WARN")
                log_event(self.paths, "workflow_stage_rollback",
                          agent=stage.agent, workflow=workflow.name,
                          stage_id=stage.id, sha=pre_stage_sha)
                # Snapshot the about-to-be-discarded work behind a ref so a
                # hard reset never makes it unrecoverable (batch commits
                # would otherwise dangle and eventually be GC'd).
                snapshot_rollback(self.paths.root, stage.id)
                # Verify the rollback actually landed: a silently-failed git
                # reset would leave broken state on disk while the runner
                # believes it recovered.
                rolled_back = revert_to(self.paths.root, pre_stage_sha)
                post_sha = head_sha(self.paths.root) if rolled_back else None
                if rolled_back and post_sha == pre_stage_sha:
                    committed = False
                else:
                    log(
                        f"stage {stage.id}: rollback to {pre_stage_sha[:8]} FAILED "
                        f"(HEAD is {(post_sha or 'unknown')[:8]}); workspace may "
                        "contain unvalidated changes",
                        level="ERROR",
                    )
                    log_event(self.paths, "workflow_rollback_failed",
                              agent=stage.agent, workflow=workflow.name,
                              stage_id=stage.id, expected_sha=pre_stage_sha,
                              actual_sha=post_sha or "")
            else:
                # Default for ordinary stages: keep the work in place. The
                # failure is recorded on the StageResult and the supervise /
                # qa loop repairs forward — a hard reset here would delete
                # batch-committed files from disk (and the Files tab).
                log(
                    f"stage {stage.id}: validation failed; keeping work in place "
                    "(validation.rollback_on_failure)",
                    level="WARN",
                )
                log_event(self.paths, "workflow_rollback_skipped",
                          agent=stage.agent, workflow=workflow.name,
                          stage_id=stage.id, sha=pre_stage_sha)

        failure_reason = ""
        if not ok:
            failure_reason = (run.response.error or "agent_failed")[:200]
        elif not v_ok:
            failure_reason = f"validation_failed: {v_out[:200]}" if v_out else "validation_failed"
        elif unmet_outputs:
            # Soft-fail tag — does not unset stage completion but visible
            # in the result for downstream consumers.
            failure_reason = f"outputs_unmet: {','.join(unmet_outputs)[:160]}"

        result = StageResult(
            stage=stage,
            run=run,
            validated=v_ok,
            validation_output=v_out,
            committed=committed,
            failure_reason=failure_reason,
        )
        if self.telemetry is not None:
            try:
                self.telemetry.record_stage(ok=bool(ok and v_ok))
            except Exception:
                pass

        # Per-stage chief checkpoint for sensitive stages. Cheap, gated,
        # and never blocks: it just keeps the chief in the loop without
        # waiting for the next supervise cycle.
        if (
            ok
            and v_ok
            and not dry_run
            and self._checkpoint_enabled(stage)
            and stage.agent != "chief"  # avoid recursion on review/checkpoint stages
        ):
            try:
                self._dispatch_checkpoint(workflow, stage, result, cycle_context, dry_run)
            except Exception as exc:
                log_exception("orchestration.workflow._run_stage.checkpoint", exc)

        return result

    # -- new helpers (blackboard / review / checkpoint) ------------------

    def _check_outputs_contract(self, stage: WorkflowStage) -> List[str]:
        """Return the list of declared output contract keys not yet posted
        by ``stage.id``. Empty list when the contract is satisfied or not
        declared.
        """
        if not stage.outputs:
            return []
        try:
            posts = _blackboard.list_posts(self.paths)
            return _blackboard.find_outputs_satisfied(
                posts, stage_id=stage.id, expected=stage.outputs
            )
        except Exception as exc:
            log_exception("orchestration.workflow._check_outputs_contract", exc)
            return []

    def _build_review_objective(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        result_by_id: Dict[str, "StageResult"],
    ) -> str:
        """Render the chief-review prompt for ``stage`` using upstream stages'
        actual posts so the chief reads the real artifacts, not the
        worker's self-report.
        """
        try:
            all_posts = _blackboard.list_posts(self.paths)
        except Exception:
            all_posts = []
        upstream_ids = list(stage.depends_on)
        sections: List[str] = [
            f"Review dispatch for workflow `{workflow.name}` stage `{stage.id}`.",
            "",
            "You are reviewing the output of these upstream stages:",
        ]
        for sid in upstream_ids:
            sr = result_by_id.get(sid)
            if sr is None:
                sections.append(f"- `{sid}`: (no result on record)")
                continue
            agent = sr.stage.agent
            ok = sr.run.response.ok
            v_ok = sr.validated
            reason = sr.failure_reason or ""
            sections.append(
                f"- `{sid}` (agent {agent}): ok={ok} validated={v_ok} "
                + (f"reason={reason}" if reason else "no failure")
            )
        sections.append("")
        sections.append("Blackboard posts produced by these stages:")
        any_posts = False
        for p in all_posts:
            if p.stage_id in upstream_ids:
                any_posts = True
                body = (p.body or "").strip()
                if len(body) > 1200:
                    body = body[:1200].rstrip() + " …"
                sections.append(
                    f"\n--- post id={p.id} author={p.author} type={p.post_type} "
                    f"stage={p.stage_id} produces={','.join(p.produces) or '-'} ---"
                )
                sections.append(body or "(empty body)")
        if not any_posts:
            sections.append("(no blackboard posts from upstream — workers may have skipped POST.)")
        sections.append("")
        sections.append(
            "Decide one of:\n"
            "  (a) ACTION:done with REASON — the user's prompt is fully addressed.\n"
            "  (b) PROPOSE_WORKFLOW with a refined next iteration (always include\n"
            "      a final supervise stage so the loop continues).\n"
            "  (c) PROPOSE_CONSENSUS to re-sample a specific decision when the\n"
            "      upstream results disagree or seem unreliable.\n"
            "Also emit a brief POST: review block summarizing what passed, what\n"
            "needs more work, and the chosen path."
        )
        if stage.objective:
            sections.append("")
            sections.append("Review-stage author's objective (from the workflow YAML):")
            sections.append(stage.objective)
        return "\n".join(sections)

    @property
    def _checkpoint_default_per_stage(self) -> bool:
        cfg = (self.runner.clk_cfg.get("review") or {})
        return bool(cfg.get("per_stage", False))

    def _checkpoint_enabled(self, stage: WorkflowStage) -> bool:
        if stage.careful:
            return True
        return self._checkpoint_default_per_stage

    def _meta_dispatch_enabled(self, stage: WorkflowStage) -> bool:
        cfg = (self.runner.clk_cfg.get("meta_prompt") or {})
        mode = str(cfg.get("dispatch") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "always":
            return True
        # default mode "careful_only"
        return bool(stage.careful)

    # -- critic-judge refinement (Layer 3 robustness loop) ---------------

    def _refine_enabled(self, stage: WorkflowStage) -> bool:
        """Decide whether the critic-judge refinement loop should run.

        Explicit ``refine:`` on the stage always wins. Otherwise we
        fall back to ``robustness.auto_refine`` (off | careful_only |
        all). ``chief`` and ``qa`` agents are skipped to avoid the
        critic critiquing its own coalescing output or the validator.
        """
        if stage.agent in ("chief", "qa", "critic"):
            return False
        if stage.refine is not None:
            return True
        cfg = (self.runner.clk_cfg.get("robustness") or {})
        mode = str(cfg.get("auto_refine") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "all":
            return True
        # default mode "careful_only"
        return bool(stage.careful)

    def _refine_loop(
        self,
        workflow: "Workflow",
        stage: WorkflowStage,
        first_run: AgentRun,
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> AgentRun:
        """Run draft → critic → revise until accept or max_rounds.

        Reuses the runner's existing dispatch path for both the critic
        and the revised worker. The critic is dispatched in a ``phase:
        refine_critic`` extra so the wrapper's auto-consensus and
        quality-retry layers don't recurse.

        Returns the final worker run — either the revised one or the
        original when the critic accepts immediately.
        """
        defaults = (self.runner.clk_cfg.get("robustness") or {})
        cfg = dict(stage.refine or {})
        critic_name = str(cfg.get("critic") or "critic")
        try:
            max_rounds = int(cfg.get("max_rounds") or defaults.get("refine_max_rounds") or 3)
        except (TypeError, ValueError):
            max_rounds = 3
        try:
            threshold = float(cfg.get("accept_threshold") or defaults.get("refine_accept_threshold") or 0.8)
        except (TypeError, ValueError):
            threshold = 0.8

        # If the named critic isn't in the roster, fall back to the
        # `critic` baseline; if even that is missing, skip silently.
        agents_cfg = (self.runner.agents_cfg.get("agents") or {})
        if critic_name not in agents_cfg:
            critic_name = "critic" if "critic" in agents_cfg else ""
        if not critic_name:
            return first_run

        current_run = first_run
        for round_idx in range(1, max_rounds + 1):
            if self.telemetry is not None:
                try:
                    self.telemetry.add_refine_round()
                except Exception:
                    pass
            verdict, judge_score, feedback = self._dispatch_critic(
                workflow, stage, current_run, critic_name, round_idx, max_rounds, dry_run,
            )
            log_event(
                self.paths,
                "refine_critic_verdict",
                agent=stage.agent,
                critic=critic_name,
                workflow=workflow.name,
                stage_id=stage.id,
                round=round_idx,
                max_rounds=max_rounds,
                verdict=verdict,
                score=judge_score,
                accept_threshold=threshold,
            )
            self.runner._observer_log(
                f"refine :: {stage.id} :: round {round_idx}/{max_rounds} "
                f"{critic_name}→ verdict={verdict} score={judge_score:.2f}"
            )
            if verdict == "accept" or judge_score >= threshold:
                return current_run
            if round_idx == max_rounds:
                # Out of budget — keep the latest worker output even
                # though the critic isn't satisfied.
                return current_run
            revise_objective = (
                f"Refinement round {round_idx + 1}/{max_rounds} of stage "
                f"`{stage.id}`. The critic (`{critic_name}`) scored your "
                f"previous response {judge_score:.2f}/1.0 and asked for "
                "revisions:\n\n"
                f"{feedback}\n\n"
                "Revise the response so the critic's points are addressed. "
                "Keep what already works; rewrite only what was flagged. "
                "Re-emit POST and ACTION blocks the same way you did the "
                "first time so the harness can record the updated work.\n\n"
                f"Original objective:\n{stage.objective}"
            )
            current_run = self.runner.run(
                stage.agent,
                revise_objective,
                extra={
                    "phase": "refine_worker",
                    "stage_id": stage.id,
                    "workflow": workflow.name,
                    "cycle_context": cycle_context,
                    "blackboard_inputs": list(stage.inputs),
                    "stage_outputs": list(stage.outputs),
                    "refine_round": round_idx + 1,
                    "refine_max_rounds": max_rounds,
                    "telemetry": self.telemetry,
                },
                dry_run=dry_run,
            )
            if not current_run.response.ok:
                return current_run
        return current_run

    _REFINE_VERDICT_RE = re.compile(
        r"^\s*VERDICT\s*:\s*(accept|revise|reject)\b", re.IGNORECASE | re.MULTILINE,
    )
    _REFINE_SCORE_RE = re.compile(
        r"^\s*SCORE\s*:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE | re.MULTILINE,
    )

    def _dispatch_critic(
        self,
        workflow: "Workflow",
        stage: WorkflowStage,
        worker_run: AgentRun,
        critic_name: str,
        round_idx: int,
        max_rounds: int,
        dry_run: Optional[bool],
    ) -> Tuple[str, float, str]:
        """Run one critic pass; return ``(verdict, score, feedback)``.

        ``verdict`` is normalised to ``"accept"`` or ``"revise"``.
        ``score`` is parsed from the critic's ``SCORE: <0..1>`` line and
        defaults to 0.0 (i.e. "revise") when missing.
        ``feedback`` is the critic's full response text, used verbatim
        in the revision objective.
        """
        worker_text = (worker_run.response.text or "").strip()
        if len(worker_text) > 4000:
            worker_text = worker_text[:4000].rstrip() + "\n…(truncated)"
        outputs_text = (
            ", ".join(stage.outputs) if stage.outputs else "(no declared outputs)"
        )
        critic_objective = (
            f"Refinement-loop critic pass for workflow `{workflow.name}` "
            f"stage `{stage.id}` (round {round_idx}/{max_rounds}).\n\n"
            f"Worker: `{stage.agent}`\n"
            f"Worker's objective:\n{stage.objective}\n\n"
            f"Declared output contract keys: {outputs_text}\n\n"
            f"Worker's response:\n---\n{worker_text}\n---\n\n"
            "Score the response 0..1 against the objective and the "
            "declared output contract. List concrete, specific "
            "revisions the worker should make. Be brief — three to six "
            "bullets is plenty. End your response with exactly two "
            "lines:\n"
            "VERDICT: accept   # or `revise` if any item must change\n"
            "SCORE: <0..1>\n"
        )
        critic_run = self.runner.run(
            critic_name,
            critic_objective,
            extra={
                "phase": "refine_critic",
                "stage_id": stage.id,
                "workflow": workflow.name,
                "refine_round": round_idx,
            },
            dry_run=dry_run,
        )
        text = critic_run.response.text or ""
        verdict_m = self._REFINE_VERDICT_RE.search(text)
        verdict = (verdict_m.group(1).lower() if verdict_m else "revise")
        if verdict not in ("accept", "revise"):
            verdict = "revise"
        score_m = self._REFINE_SCORE_RE.search(text)
        try:
            score_val = float(score_m.group(1)) if score_m else 0.0
        except (TypeError, ValueError):
            score_val = 0.0
        score_val = max(0.0, min(1.0, score_val))
        # When the critic accepted but didn't post a score, treat it as
        # a confident pass; when it asked to revise but didn't score,
        # treat as a moderate-low score so the loop continues.
        if score_m is None:
            score_val = 1.0 if verdict == "accept" else 0.4
        return verdict, score_val, text.strip()

    # -- adversarial debate panel (multi-critic refinement) --------------

    _DEBATE_LENS_GUIDANCE: Dict[str, str] = {
        "correctness": "logic errors, wrong outputs, unhandled edge cases, broken contracts or APIs.",
        "security": "injection, unsafe input handling, secret/credential leakage, unsafe shell/file operations.",
        "simplicity": "needless complexity, duplication, dead code, and simpler equivalent designs.",
        "performance": "obvious inefficiency, redundant work, N+1 patterns, unbounded loops or memory.",
        "robustness": "failure modes, missing error handling, flaky assumptions, and race conditions.",
        "tests": "missing or weak tests, untested branches, and assertions that don't actually verify behavior.",
        "ux": "confusing interfaces, poor error messages, and undocumented behavior.",
    }

    def _debate_enabled(self, stage: WorkflowStage) -> bool:
        """Whether the adversarial debate panel should run for this stage.

        Explicit ``refine: {mode: debate}`` always wins; otherwise the
        ``robustness.debate`` policy (off | careful_only | all) decides.
        chief / qa / critic stages are skipped.
        """
        if stage.agent in ("chief", "qa", "critic"):
            return False
        if isinstance(stage.refine, dict) and str(stage.refine.get("mode") or "").lower() == "debate":
            return True
        cfg = (self.runner.clk_cfg.get("robustness") or {})
        mode = str(cfg.get("debate") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "all":
            return True
        return bool(stage.careful)  # careful_only

    def _debate_lenses(self, stage: WorkflowStage) -> List[str]:
        if isinstance(stage.refine, dict) and stage.refine.get("critics"):
            lenses = [str(x).strip().lower() for x in stage.refine["critics"] if str(x).strip()]
        else:
            cfg = (self.runner.clk_cfg.get("robustness") or {})
            lenses = [str(x).strip().lower() for x in (cfg.get("debate_lenses") or []) if str(x).strip()]
        return lenses or ["correctness", "security", "simplicity"]

    def _dispatch_lens_critic(
        self,
        workflow: "Workflow",
        stage: WorkflowStage,
        worker_run: AgentRun,
        critic_name: str,
        lens: str,
        round_idx: int,
        max_rounds: int,
        peer_transcript: str,
        dry_run: Optional[bool],
    ) -> Tuple[str, str, float, str]:
        """One adversarial critic pass for a single lens.

        Returns ``(lens, verdict, score, feedback)``. The critic is told to
        try to *break* the work from its lens and, in later rounds, to engage
        with peers' critiques (reinforce / refute / concede).
        """
        worker_text = (worker_run.response.text or "").strip()
        if len(worker_text) > 3500:
            worker_text = worker_text[:3500].rstrip() + "\n…(truncated)"
        guidance = self._DEBATE_LENS_GUIDANCE.get(
            lens, f"weaknesses from the {lens} perspective."
        )
        peer_block = ""
        if peer_transcript.strip():
            peer_block = (
                "\nYour fellow panelists said (engage with them — reinforce, "
                "refute, or concede explicitly):\n"
                f"{peer_transcript}\n"
            )
        objective = (
            f"ADVERSARIAL DEBATE — you are the **{lens}** critic on a review "
            f"panel for stage `{stage.id}` (round {round_idx}/{max_rounds}).\n\n"
            f"Your lens: hunt for {guidance}\n"
            "Try hard to BREAK this work from your lens. Be specific and "
            "concrete; cite the exact place. Default to skepticism — only "
            "accept if you genuinely cannot find a real problem.\n\n"
            f"Worker `{stage.agent}` objective:\n{stage.objective}\n\n"
            f"Worker's response:\n---\n{worker_text}\n---\n"
            f"{peer_block}\n"
            "Keep it to 2-5 concrete bullets. End with exactly two lines:\n"
            "VERDICT: accept   # or `revise` if any real issue remains\n"
            "SCORE: <0..1>\n"
        )
        critic_run = self.runner.run(
            critic_name,
            objective,
            extra={
                "phase": "refine_critic",
                "stage_id": stage.id,
                "workflow": workflow.name,
                "refine_round": round_idx,
                "debate_lens": lens,
            },
            dry_run=dry_run,
        )
        text = critic_run.response.text or ""
        verdict_m = self._REFINE_VERDICT_RE.search(text)
        verdict = (verdict_m.group(1).lower() if verdict_m else "revise")
        if verdict not in ("accept", "revise"):
            verdict = "revise"
        score_m = self._REFINE_SCORE_RE.search(text)
        try:
            score_val = float(score_m.group(1)) if score_m else (1.0 if verdict == "accept" else 0.4)
        except (TypeError, ValueError):
            score_val = 0.4
        score_val = max(0.0, min(1.0, score_val))
        return lens, verdict, score_val, text.strip()

    def _debate_loop(
        self,
        workflow: "Workflow",
        stage: WorkflowStage,
        first_run: AgentRun,
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> AgentRun:
        """Run an adversarial debate panel: N lens-critics → worker revision.

        Each round fans out one critic per lens in parallel; the worker is
        kept only if a majority of lenses accept (or the mean score clears the
        threshold). Otherwise the combined critiques drive a revision, and the
        next round's critics see the prior panel transcript so they can debate
        each other. Bounded by ``debate_max_rounds``.
        """
        defaults = (self.runner.clk_cfg.get("robustness") or {})
        cfg = dict(stage.refine or {}) if isinstance(stage.refine, dict) else {}
        try:
            max_rounds = int(cfg.get("max_rounds") or defaults.get("debate_max_rounds") or 2)
        except (TypeError, ValueError):
            max_rounds = 2
        try:
            threshold = float(cfg.get("accept_threshold") or defaults.get("refine_accept_threshold") or 0.8)
        except (TypeError, ValueError):
            threshold = 0.8

        agents_cfg = (self.runner.agents_cfg.get("agents") or {})
        critic_name = "critic" if "critic" in agents_cfg else ""
        if not critic_name:
            # No critic in the roster — fall back to the single-critic loop
            # (which itself no-ops when no critic exists).
            return self._refine_loop(workflow, stage, first_run, cycle_context, dry_run)

        lenses = self._debate_lenses(stage)
        max_parallel = max(1, int((self.runner.clk_cfg.get("consensus") or {}).get("max_parallel") or 4))
        current_run = first_run
        peer_transcript = ""

        for round_idx in range(1, max_rounds + 1):
            if self.telemetry is not None:
                try:
                    self.telemetry.add_refine_round()
                except Exception:
                    pass
            verdicts: List[Tuple[str, str, float, str]] = []
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(lenses))) as pool:
                futs = {
                    pool.submit(
                        self._dispatch_lens_critic, workflow, stage, current_run,
                        critic_name, lens, round_idx, max_rounds, peer_transcript, dry_run,
                    ): lens
                    for lens in lenses
                }
                for fut in as_completed(futs):
                    try:
                        verdicts.append(fut.result())
                    except Exception as exc:
                        log_exception("orchestration.workflow._debate_loop.critic", exc)
            if not verdicts:
                return current_run
            revise_votes = sum(1 for (_l, v, _s, _f) in verdicts if v == "revise")
            scores = [s for (_l, _v, s, _f) in verdicts]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            transcript = "\n".join(
                f"[{lens}] verdict={v} score={s:.2f}\n{fb}" for (lens, v, s, fb) in verdicts
            )
            peer_transcript = transcript
            log_event(
                self.paths, "debate_round",
                agent=stage.agent, workflow=workflow.name, stage_id=stage.id,
                round=round_idx, max_rounds=max_rounds,
                lenses=[l for (l, *_r) in verdicts],
                revise_votes=revise_votes, avg_score=round(avg_score, 3),
                accept_threshold=threshold,
            )
            self.runner._observer_log(
                f"debate :: {stage.id} :: round {round_idx}/{max_rounds} "
                f"{len(lenses)} critics, {revise_votes} revise, avg={avg_score:.2f}"
            )
            try:
                _blackboard.post(
                    self.paths, author="critic-panel", body=transcript[:4000],
                    post_type="debate", stage_id=stage.id, workflow=workflow.name,
                    slug_hint=f"debate-{stage.id}-r{round_idx}",
                )
            except Exception as exc:
                log_exception("orchestration.workflow._debate_loop.post", exc)

            # Panel accepts when a majority accept AND the mean clears the bar.
            if revise_votes * 2 <= len(verdicts) and avg_score >= threshold:
                return current_run
            if round_idx == max_rounds:
                return current_run

            revise_objective = (
                f"Debate round {round_idx + 1}/{max_rounds} of stage `{stage.id}`. "
                f"An adversarial review panel ({', '.join(lenses)}) found issues "
                f"(mean score {avg_score:.2f}/1.0). Address every concrete point "
                "below; keep what already works. Re-emit POST and ACTION blocks "
                "the same way so the harness records the updated work.\n\n"
                f"Panel critiques:\n{transcript}\n\n"
                f"Original objective:\n{stage.objective}"
            )
            current_run = self.runner.run(
                stage.agent,
                revise_objective,
                extra={
                    "phase": "refine_worker",
                    "stage_id": stage.id,
                    "workflow": workflow.name,
                    "cycle_context": cycle_context,
                    "blackboard_inputs": list(stage.inputs),
                    "stage_outputs": list(stage.outputs),
                    "refine_round": round_idx + 1,
                    "refine_max_rounds": max_rounds,
                    "telemetry": self.telemetry,
                },
                dry_run=dry_run,
            )
            if not current_run.response.ok:
                return current_run
        return current_run

    def _dispatch_checkpoint(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        result: "StageResult",
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> None:
        """Light-weight chief checkpoint after a sensitive stage.

        Cost-bounded: a small prompt with the stage's posts and a
        request for a CONTINUE / REDIRECT / ABORT verdict. The chief
        emits ACTION:done if the project is finished, or PROPOSE_WORKFLOW
        if the plan should change. Otherwise we just log the verdict and
        let the workflow proceed.
        """
        try:
            posts = _blackboard.list_posts(self.paths)
        except Exception:
            posts = []
        produced = [p for p in posts if p.stage_id == stage.id]
        snapshot = "\n".join(
            f"- {p.id} type={p.post_type} produces={','.join(p.produces) or '-'} "
            f"body_chars={len(p.body or '')}"
            for p in produced[-10:]
        ) or "(no posts from this stage)"
        objective = (
            f"Chief checkpoint after stage `{stage.id}` (agent {stage.agent}, "
            f"workflow `{workflow.name}`).\n\n"
            f"Stage objective:\n{stage.objective}\n\n"
            f"Posts produced by this stage:\n{snapshot}\n\n"
            "Reply with one of:\n"
            "  CHECKPOINT: continue — let the workflow proceed as planned.\n"
            "  CHECKPOINT: redirect — emit PROPOSE_WORKFLOW with a revised plan.\n"
            "  CHECKPOINT: abort — emit ACTION:done if the project is finished.\n"
            "Keep the response short — this is a verification, not a redo."
        )
        log(f"workflow {workflow.name}: checkpoint after stage {stage.id}")
        self.runner.run(
            "chief",
            objective,
            extra={
                "phase": "checkpoint",
                "workflow": workflow.name,
                "stage_id": stage.id,
                "cycle_context": cycle_context,
                "blackboard_inputs": [f"stage:{stage.id}"],
            },
            dry_run=dry_run,
        )

    # -- helpers ---------------------------------------------------------

    def _unmet_deps(self, stage: WorkflowStage, completed: Dict[str, bool]) -> List[str]:
        return [d for d in stage.depends_on if not completed.get(d)]

    def _is_provider_failure(self, error: str) -> bool:
        return is_provider_failure(error)

    def _is_retryable_stage_error(self, error: str) -> bool:
        """Subset of provider failures worth retrying with backoff at stage level."""
        msg = (error or "").lower()
        retryable = [
            "no output for",
            "timeout after",
            "operation was aborted",
            "no endpoints available",
            "guardrail restrictions",
            "data policy",
            "connection reset",
            "temporarily unavailable",
            "try again",
            "rate limit",
            "quota",
            # HTTP 429 rate-limiting and HTTP 404 (OpenRouter: no endpoints temporarily available)
            "http 429",
            "http 404",
        ]
        non_retryable = [
            "api key",
            "authentication",
            "unauthorized",
            "forbidden",
            "cli not found",
        ]
        return any(s in msg for s in retryable) and not any(s in msg for s in non_retryable)

    def _log_skip(
        self,
        stage: WorkflowStage,
        unmet: List[str],
        result_by_id: Dict[str, StageResult],
    ) -> None:
        details: List[str] = []
        for d in unmet:
            sr = result_by_id.get(d)
            if sr is None:
                details.append(f"{d}=never_ran")
            elif sr.failure_reason:
                details.append(f"{d}={sr.failure_reason}")
            else:
                details.append(f"{d}=incomplete")
        log(
            f"stage {stage.id} skipped after recovery limit: " + "; ".join(details),
            level="WARN",
        )

    def _dispatch_recovery(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        unmet: List[str],
        result_by_id: Dict[str, StageResult],
        *,
        dry_run: Optional[bool],
        cycle_context: str = "",
    ) -> None:
        details: List[str] = []
        for d in unmet:
            sr = result_by_id.get(d)
            if sr is None:
                details.append(f"- `{d}`: never ran (probably never reached or removed from workflow)")
            elif sr.failure_reason:
                details.append(f"- `{d}`: {sr.failure_reason}")
            else:
                details.append(f"- `{d}`: incomplete (no failure recorded)")
        objective = (
            f"Recovery dispatch for workflow `{workflow.name}` stage `{stage.id}`.\n\n"
            f"This stage depends on: {stage.depends_on}.\n"
            f"Unmet dependencies (with reasons):\n" + "\n".join(details) + "\n\n"
            "Decide one of:\n"
            "  (a) Re-cast the workflow with PROPOSE_WORKFLOW so the dependency is\n"
            "      no longer required, OR\n"
            "  (b) Emit ACTION blocks that fix the upstream failure (write/edit/run\n"
            "      to satisfy the failed validation), OR\n"
            "  (c) dispatch an existing suitable agent, or PROPOSE_ROLE for a\n"
            "      distinct specialist if no current agent fits (b).\n"
            "Do NOT skip silently. The harness will retry this stage after you respond."
        )
        log(f"workflow {workflow.name}: dispatching chief recovery for stage {stage.id}")
        self.runner.run(
            "chief",
            objective,
            extra={
                "phase": "recovery",
                "workflow": workflow.name,
                "stage_id": stage.id,
                "unmet_deps": ",".join(unmet),
                "cycle_context": cycle_context,
            },
            dry_run=dry_run,
        )

    @property
    def _outputs_recovery_enabled(self) -> bool:
        """Gate for the outputs-contract recovery dispatch. Defaults on;
        disable via clk.config.json::recovery::dispatch_on_unmet_outputs.
        """
        cfg = (self.runner.clk_cfg.get("recovery") or {})
        val = cfg.get("dispatch_on_unmet_outputs", True)
        return str(val).lower() not in ("false", "0", "off", "no")

    def _dispatch_outputs_recovery(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        missing: List[str],
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> None:
        """Chief recovery pass for an unsatisfied outputs contract.

        Runs once per stage execution (the caller re-checks the contract
        afterwards). The chief can re-dispatch the worker via a Q&A-style
        instruction, post the missing keys itself if the information is
        already on the blackboard, or explicitly accept the gap.
        """
        objective = (
            f"Outputs-contract recovery for workflow `{workflow.name}` "
            f"stage `{stage.id}` (agent {stage.agent}).\n\n"
            f"The stage declared it would produce these blackboard keys but "
            f"did not: {', '.join(missing)}.\n"
            f"Stage objective was:\n{stage.objective}\n\n"
            "Downstream stages consume these keys; missing them causes silent "
            "data gaps. Do one of:\n"
            "  (a) Post the missing keys yourself (POST block with PRODUCES:\n"
            "      listing them) if the information already exists on the\n"
            "      blackboard or in the repo, OR\n"
            "  (b) Emit ACTION blocks that produce the artifact the keys\n"
            "      describe, then POST with the keys, OR\n"
            "  (c) Explicitly accept the gap in a POST: review block stating\n"
            "      why downstream stages can proceed without these keys.\n"
            "Do NOT skip silently."
        )
        log(
            f"workflow {workflow.name}: dispatching chief outputs recovery "
            f"for stage {stage.id} (missing: {', '.join(missing)})"
        )
        log_event(
            self.paths, "workflow_outputs_recovery",
            agent=stage.agent, workflow=workflow.name,
            stage_id=stage.id, missing=list(missing),
        )
        self.runner.run(
            "chief",
            objective,
            extra={
                "phase": "recovery",
                "workflow": workflow.name,
                "stage_id": stage.id,
                "cycle_context": cycle_context,
                "blackboard_inputs": [f"stage:{stage.id}"],
            },
            dry_run=dry_run,
        )

    def _dispatch_stall_rescue(
        self,
        workflow: Workflow,
        cycle: int,
        cycle_results: List[StageResult],
    ) -> None:
        """One-shot chief dispatch when the supervise loop stalls.

        Instead of silently giving up after N no-progress cycles, give the
        chief the stall evidence and a chance to (a) declare the project
        done, (b) restructure the plan via PROPOSE_WORKFLOW, or (c) emit
        ACTION blocks that unblock the workers directly. Runs at most once
        per supervise loop (the caller tracks ``rescue_attempted``).
        """
        lines: List[str] = []
        for r in cycle_results[-8:]:
            ok = r.run.response.ok
            reason = r.failure_reason or ("ok" if ok else "failed")
            lines.append(f"- stage `{r.stage.id}` (agent {r.stage.agent}): {reason}")
        objective = (
            f"STALL RESCUE for workflow `{workflow.name}` at supervise cycle {cycle}.\n\n"
            "The loop has made no measurable progress for several consecutive "
            "cycles (no commits, no file writes, or every agent self-reported "
            "`PROGRESS: no`). Last cycle's stages:\n"
            + "\n".join(lines or ["- (no stage results recorded)"])
            + "\n\nDiagnose WHY the loop is stuck, then do exactly one of:\n"
            "  (a) ACTION:done with REASON — if the user's objective is actually\n"
            "      complete and the loop is spinning on nothing.\n"
            "  (b) PROPOSE_WORKFLOW with a restructured plan that removes the\n"
            "      blocked stages and takes a genuinely different approach.\n"
            "  (c) ACTION blocks (write/edit/run) that directly fix the blocker\n"
            "      the workers keep hitting.\n"
            "Do NOT re-propose the same plan that is already stalling. This is "
            "the loop's last chance before the harness stops it."
        )
        log(f"workflow {workflow.name}: dispatching chief stall rescue (cycle {cycle})", level="WARN")
        log_event(self.paths, "workflow_stall_rescue", workflow=workflow.name, cycle=cycle)
        try:
            self.runner.run(
                "chief",
                objective,
                extra={
                    "phase": "recovery",
                    "workflow": workflow.name,
                    "stage_id": "stall_rescue",
                },
            )
        except Exception as exc:
            log_exception("orchestration.workflow._dispatch_stall_rescue", exc)

    def _refresh_from_dispatched(
        self,
        workflow_name: str,
        wf_path: Path,
        prev_mtime: float,
        stages: List[WorkflowStage],
        done_ids: Set[str],
    ) -> Tuple[List[WorkflowStage], float]:
        """Reload the workflow file if rewritten; splice in any new stages
        whose ids haven't been dispatched yet so dynamically-added stages
        are picked up without re-running already-dispatched ones.
        """
        if not wf_path.exists():
            return stages, prev_mtime
        new_mtime = wf_path.stat().st_mtime
        if new_mtime <= prev_mtime:
            return stages, prev_mtime
        try:
            refreshed = load_workflow(wf_path)
        except Exception as exc:
            log_exception("orchestration.workflow._refresh_from_dispatched", exc)
            return stages, prev_mtime
        kept = [s for s in stages if s.id in done_ids]
        new_pending = [s for s in refreshed.stages if s.id not in done_ids]
        merged = kept + new_pending
        log(
            f"workflow {workflow_name}: refreshed; "
            f"{len(kept)} done, {len(new_pending)} pending"
        )
        return merged, new_mtime

    def _maybe_refresh_workflow(
        self,
        workflow_name: str,
        wf_path: Path,
        prev_mtime: float,
        stages: List[WorkflowStage],
        cursor: int,
    ) -> Tuple[List[WorkflowStage], float]:
        """If the workflow file was rewritten, replace the un-processed
        tail of the queue with the refreshed stage list.

        ``cursor`` is the index the runner is about to process next.
        Stages already executed (``stages[:cursor]``) are preserved so
        we never re-run them. Stages from the refreshed YAML whose ids
        appear in the executed prefix are dropped (the agent should use
        a new id like ``foo_retry`` if they want to re-attempt).
        """
        if not wf_path.exists():
            return stages, prev_mtime
        new_mtime = wf_path.stat().st_mtime
        if new_mtime <= prev_mtime:
            return stages, prev_mtime
        try:
            refreshed = load_workflow(wf_path)
        except Exception as exc:
            log_exception("orchestration.workflow._maybe_refresh_workflow", exc)
            return stages, prev_mtime
        processed = stages[:cursor]
        processed_ids = {s.id for s in processed}
        pending = [s for s in refreshed.stages if s.id not in processed_ids]
        merged = processed + pending
        log(
            f"workflow {workflow_name}: refreshed; "
            f"{len(processed)} processed, {len(pending)} pending"
        )
        return merged, new_mtime

    def _validate(self, stage: WorkflowStage) -> tuple[bool, str]:
        cmd = stage.validation
        # FM4: a producing stage with no explicit validation no longer
        # auto-passes — derive a real command from the project shape. Non-
        # producing stages (chief/critic prose) keep the auto-pass.
        if not cmd:
            val_cfg = (self.runner.clk_cfg.get("validation") or {})
            if val_cfg.get("auto_derive", True) and _noop_guard.is_mutation_expected(
                stage.agent, outputs=stage.outputs, commit=stage.commit,
                cfg=self.runner.clk_cfg,
            ):
                if val_cfg.get("derived_command"):
                    cmd = str(val_cfg.get("derived_command"))
                else:
                    derived, _weak = _evaluator.derive_validation(self.paths.root)
                    cmd = derived[0] if derived else None
            if not cmd:
                return True, ""
        try:
            log_event(
                self.paths,
                "shell_command_start",
                agent=stage.agent,
                action="validation",
                stage_id=stage.id,
                cmd=cmd,
                cwd=str(self.paths.root),
                timeout_s=120,
            )
            r = subprocess.run(
                cmd,
                shell=True,
                cwd=str(self.paths.root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (r.stdout or "") + (r.stderr or "")
            log_event(
                self.paths,
                "shell_command_end",
                agent=stage.agent,
                action="validation",
                stage_id=stage.id,
                cmd=cmd,
                ok=r.returncode == 0,
                returncode=r.returncode,
                output=output,
                output_chars=len(output or ""),
            )
            return r.returncode == 0, output.strip()
        except Exception as exc:
            log_exception("orchestration.workflow._validate", exc)
            log_event(
                self.paths,
                "shell_command_end",
                agent=stage.agent,
                action="validation",
                stage_id=stage.id,
                cmd=cmd,
                ok=False,
                error=str(exc),
            )
            return False, str(exc)

    def _commit(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        run: AgentRun,
        validation_output: str,
    ) -> bool:
        if not has_changes(self.paths.root):
            return False
        if not add_all(self.paths.root):
            return False
        return git_commit(
            self.paths.root,
            agent=f"{workflow.name}.{stage.id}",
            objective=stage.objective,
            files_changed=run.files_written,
            validation=stage.validation or "none",
            next_step=f"continue workflow {workflow.name}",
            body_extra=(validation_output or "")[:500],
        )
