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
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from .agent import AgentRunner, AgentRun


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
        stopped_for_provider_failure = False
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
            and not (self.paths.state / "done.md").exists()
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
                batch = [self._run_stage(ready[0], workflow, cycle_context, dry_run)]
            else:
                with ThreadPoolExecutor(max_workers=len(ready)) as pool:
                    fmap = {
                        pool.submit(self._run_stage, s, workflow, cycle_context, dry_run): s
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
    ) -> StageResult:
        """Run a single stage and return its result."""
        run = self.runner.run(
            stage.agent,
            stage.objective,
            extra={"stage_id": stage.id, "workflow": workflow.name, "cycle_context": cycle_context},
            dry_run=dry_run,
        )
        ok = run.response.ok
        if dry_run:
            v_ok, v_out = True, "(dry-run: validation skipped)"
        else:
            v_ok, v_out = self._validate(stage)
        committed = False
        if run.committed:
            committed = True
        elif ok and v_ok and stage.commit and not dry_run:
            committed = self._commit(workflow, stage, run, v_out)
        failure_reason = ""
        if not ok:
            failure_reason = (run.response.error or "agent_failed")[:200]
        elif not v_ok:
            failure_reason = f"validation_failed: {v_out[:200]}" if v_out else "validation_failed"
        return StageResult(
            stage=stage,
            run=run,
            validated=v_ok,
            validation_output=v_out,
            committed=committed,
            failure_reason=failure_reason,
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
        if not stage.validation:
            return True, ""
        try:
            log_event(
                self.paths,
                "shell_command_start",
                agent=stage.agent,
                action="validation",
                stage_id=stage.id,
                cmd=stage.validation,
                cwd=str(self.paths.root),
                timeout_s=120,
            )
            r = subprocess.run(
                stage.validation,
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
                cmd=stage.validation,
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
                cmd=stage.validation,
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
