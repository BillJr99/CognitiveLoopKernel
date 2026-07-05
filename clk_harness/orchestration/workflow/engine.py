"""Workflow runner engine: the supervise-cycle driver and stage dispatch."""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

from ...config import Paths
from ...git_ops import head_sha, revert_to, snapshot_rollback
from ...log import get_logger, log_exception
from ...utils.activity_log import log_event
from .. import deliberation as _deliberation
from .. import noop_guard as _noop_guard
from .. import response_quality as _response_quality
from ..agent import AgentRun, AgentRunner
from ..telemetry import CycleTelemetry
from .recovery import RecoveryMixin
from .review import ReviewMixin
from .stages import StageResult, Workflow, WorkflowStage, _round_status, load_workflow
from .validation import ValidationMixin

logger = get_logger(__name__)


class WorkflowRunner(ReviewMixin, RecoveryMixin, ValidationMixin):
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

    def _telemetry_stdout(self) -> bool:
        cfg = (self.runner.clk_cfg.get("mission") or {})
        return bool(cfg.get("telemetry_stdout", True))

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
                logger.info(f"workflow {workflow.name}: stop granted, ending supervise loop")
                stopped_done = True
                break

            cancel_file = self.paths.state / "cancel_requested.txt"
            if cancel_file.exists():
                try:
                    cancel_file.unlink()
                except Exception as _exc:
                    logger.debug("could not remove cancel marker %s: %s", cancel_file, _exc)
                logger.info(f"workflow {workflow.name}: graceful cancel requested; stopping after cycle {cycle - 1}")
                break

            if cycle > 1:
                logger.info(f"workflow {workflow.name}: supervise cycle {cycle}/{self.max_supervise_cycles}")
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
                why = (
                    "agents reported PROGRESS: no"
                    if (material and self_reported_stall)
                    else "no commits or file writes"
                )
                logger.info(
                    f"workflow {workflow.name}: cycle {cycle} made no progress — {why} "
                    f"({no_progress}/{self.max_consecutive_no_progress})" if no_progress >= 2 else "INFO",
                )
                if no_progress >= self.max_consecutive_no_progress:
                    if self.stall_rescue_enabled and not rescue_attempted and not dry_run:
                        rescue_attempted = True
                        no_progress = 0
                        self._dispatch_stall_rescue(workflow, cycle, cycle_results)
                        if self._stop_requested(workflow):
                            logger.info(f"workflow {workflow.name}: stop granted during stall rescue")
                            stopped_done = True
                            break
                        continue
                    logger.error(
                        f"workflow {workflow.name}: stopping after {no_progress} consecutive "
                        "no-progress cycles (set supervise.max_consecutive_no_progress to change)",
                    )
                    log_event(self.paths, "workflow_stalled", workflow=workflow.name,
                              no_progress_cycles=no_progress,
                              rescue_attempted=rescue_attempted)
                    break
            else:
                no_progress = 0

            if any(
                self._is_provider_failure((r.run.response.error or "")) for r in cycle_results if not r.run.response.ok
            ):
                logger.error(
                    f"workflow {workflow.name}: stopping supervise cycles after provider failure",
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
            logger.warning(
                f"workflow {workflow.name}: supervise cycle limit reached "
                f"({self.max_supervise_cycles}); type /run to continue or set "
                "supervise.max_cycles in clk.config.json",
            )
        return all_results

    def _run_once(self, workflow: Workflow, *, dry_run: Optional[bool] = None, cycle: int = 1) -> List[StageResult]:
        """Single pass through the workflow; stages with no inter-dependencies run in parallel."""
        logger.info(f"workflow start: {workflow.name} ({len(workflow.stages)} stages)")
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
                logger.info(
                    f"workflow {workflow.name}: parallel batch "
                    f"[{', '.join(s.id for s in ready)}]"
                )
            else:
                logger.info(f"stage {ready[0].id} -> agent {ready[0].agent}")

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
                        logger.warning(
                            f"workflow {workflow.name}: stage {sr.stage.id} retryable error "
                            f"(attempt {st}/{self.max_stage_retries}): {error_msg!r}; "
                            f"backing off {wait:.0f}s",
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
                            except Exception as _exc:
                                logger.debug("observer progress failed: %s", _exc)
                        dispatched.discard(sr.stage.id)
                        time.sleep(wait)
                        continue  # retry: don't add to results/completed
                    logger.error(
                        f"workflow {workflow.name}: aborting after provider failure "
                        f"in stage {sr.stage.id} (retries exhausted): {error_msg}",
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

        logger.info(f"workflow done: {workflow.name}")
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
            logger.warning(
                f"workflow {workflow.name}: stage {stage.id} did not satisfy "
                f"declared outputs: {unmet_outputs}",
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
                logger.warning(f"stage {stage.id}: validation failed; rolling back to {pre_stage_sha[:8]}")
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
                    logger.error(
                        f"stage {stage.id}: rollback to {pre_stage_sha[:8]} FAILED "
                        f"(HEAD is {(post_sha or 'unknown')[:8]}); workspace may "
                        "contain unvalidated changes",
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
                logger.warning(
                    f"stage {stage.id}: validation failed; keeping work in place "
                    "(validation.rollback_on_failure)",
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
            except Exception as _exc:
                logger.debug("telemetry record_stage failed: %s", _exc)

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

    # -- helpers ---------------------------------------------------------

    def _unmet_deps(self, stage: WorkflowStage, completed: Dict[str, bool]) -> List[str]:
        return [d for d in stage.depends_on if not completed.get(d)]

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
        logger.info(
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
        logger.info(
            f"workflow {workflow_name}: refreshed; "
            f"{len(processed)} processed, {len(pending)} pending"
        )
        return merged, new_mtime
