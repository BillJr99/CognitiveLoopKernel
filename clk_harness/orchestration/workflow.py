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

import shlex
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes
from ..utils.logging_utils import log, log_exception
from .agent import AgentRunner, AgentRun


try:
    import yaml  # type: ignore
except Exception:
    # PyYAML is optional. The mini-YAML loader below covers the workflow
    # subset CLK uses, so we silently fall back rather than spraying a
    # warning across stderr (which would also corrupt the TUI).
    yaml = None


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
                        cur[k2.strip()] = parse_scalar(v2)
                else:
                    if cur is None:
                        cur = {}
                    if ":" in stripped:
                        k2, _, v2 = stripped.partition(":")
                        cur[k2.strip()] = parse_scalar(v2)
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
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
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
            data = _mini_yaml_loads(text)
    else:
        try:
            data = _mini_yaml_loads(text)
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.fallback", exc)
            raise

    stages: List[WorkflowStage] = []
    for raw in data.get("stages") or []:
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

    # Default cap on chief recovery dispatches per stage. A stage that
    # still has unmet deps after this many recovery passes gets a final
    # WARN and is skipped, so we never loop forever on a stuck workflow.
    # Overridable via clk.config.json::recovery::max_per_stage.
    DEFAULT_MAX_RECOVERY_PER_STAGE = 3

    @property
    def max_recovery_per_stage(self) -> int:
        cfg = (self.runner.clk_cfg.get("recovery") or {})
        return int(cfg.get("max_per_stage") or self.DEFAULT_MAX_RECOVERY_PER_STAGE)

    DEFAULT_MAX_SUPERVISE_CYCLES = 5

    @property
    def max_supervise_cycles(self) -> int:
        cfg = (self.runner.clk_cfg.get("supervise") or {})
        return int(cfg.get("max_cycles") or self.DEFAULT_MAX_SUPERVISE_CYCLES)

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
        for cycle in range(1, self.max_supervise_cycles + 1):
            if (self.paths.state / "done.md").exists():
                log(f"workflow {workflow.name}: done.md present, stopping supervise loop")
                break
            if cycle > 1:
                log(f"workflow {workflow.name}: supervise cycle {cycle}/{self.max_supervise_cycles}")
            try:
                refreshed = load_workflow(self.paths.workflows / f"{workflow.name}.yaml")
            except Exception:
                refreshed = workflow
            cycle_results = self._run_once(refreshed, dry_run=dry_run, cycle=cycle)
            all_results.extend(cycle_results)
            if dry_run:
                break
        if not (self.paths.state / "done.md").exists() and self.max_supervise_cycles > 1:
            log(
                f"workflow {workflow.name}: supervise cycle limit reached "
                f"({self.max_supervise_cycles}); type /run to continue or set "
                "supervise.max_cycles in clk.config.json",
                level="WARN",
            )
        return all_results

    def _run_once(self, workflow: Workflow, *, dry_run: Optional[bool] = None, cycle: int = 1) -> List[StageResult]:
        """Single pass through the workflow; called once per supervise cycle."""
        log(f"workflow start: {workflow.name} ({len(workflow.stages)} stages)")
        results: List[StageResult] = []
        completed: Dict[str, bool] = {}
        # stage_id -> StageResult for richer "why did dep fail" context
        result_by_id: Dict[str, StageResult] = {}
        wf_path = self.paths.workflows / f"{workflow.name}.yaml"
        wf_mtime = wf_path.stat().st_mtime if wf_path.exists() else 0.0

        stages = list(workflow.stages)
        recovery_count: Dict[str, int] = {}
        i = 0
        while i < len(stages):
            stage = stages[i]
            unmet = self._unmet_deps(stage, completed)
            if unmet:
                tries = recovery_count.get(stage.id, 0)
                if tries >= self.max_recovery_per_stage or dry_run:
                    self._log_skip(stage, unmet, result_by_id)
                    completed[stage.id] = False
                    i += 1
                    continue
                recovery_count[stage.id] = tries + 1
                self._dispatch_recovery(workflow, stage, unmet, result_by_id, dry_run=dry_run)
                # Recovery may have rewritten the workflow with new
                # stages and/or relaxed dependencies. Re-read from disk
                # and replace the un-processed tail of the queue.
                stages, wf_mtime = self._maybe_refresh_workflow(
                    workflow.name, wf_path, wf_mtime, stages, i
                )
                # Don't advance i - whatever stage now sits at i is the
                # next thing to attempt (chief's new stage, or the same
                # stage with its deps now relaxed).
                continue
            log(f"stage {stage.id} -> agent {stage.agent}")
            run = self.runner.run(
                stage.agent,
                stage.objective,
                extra={"stage_id": stage.id, "workflow": workflow.name},
                dry_run=dry_run,
            )
            ok = run.response.ok
            if dry_run:
                v_ok, v_out = True, "(dry-run: validation skipped)"
            else:
                v_ok, v_out = self._validate(stage)
            committed = False
            if run.committed:
                # AgentRunner already created a per-action-batch commit;
                # don't double-commit the same diff.
                committed = True
            elif ok and v_ok and stage.commit and not dry_run:
                committed = self._commit(workflow, stage, run, v_out)
            failure_reason = ""
            if not ok:
                failure_reason = (run.response.error or "agent_failed")[:200]
            elif not v_ok:
                failure_reason = f"validation_failed: {v_out[:200]}" if v_out else "validation_failed"
            sr = StageResult(
                stage=stage,
                run=run,
                validated=v_ok,
                validation_output=v_out,
                committed=committed,
                failure_reason=failure_reason,
            )
            results.append(sr)
            result_by_id[stage.id] = sr
            completed[stage.id] = ok and v_ok
            i += 1
            stages, wf_mtime = self._maybe_refresh_workflow(
                workflow.name, wf_path, wf_mtime, stages, i
            )
        log(f"workflow done: {workflow.name}")
        return results

    # -- helpers ---------------------------------------------------------

    def _unmet_deps(self, stage: WorkflowStage, completed: Dict[str, bool]) -> List[str]:
        return [d for d in stage.depends_on if not completed.get(d)]

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
            "  (c) PROPOSE_ROLE for a specialist that can do (b), then dispatch them.\n"
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
            },
            dry_run=dry_run,
        )

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
        if not stage.validation:
            return True, ""
        try:
            r = subprocess.run(
                stage.validation,
                shell=True,
                cwd=str(self.paths.root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, output.strip()
        except Exception as exc:
            log_exception("orchestration.workflow._validate", exc)
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
