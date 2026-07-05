"""Recovery behaviors for the workflow runner.

Provider-failure classification, chief recovery dispatches for unmet
dependencies and unsatisfied output contracts, the one-shot stall
rescue, and the validation-failure rollback policy.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Dict, List, Optional

from ...log import get_logger, log_exception
from ...utils.activity_log import log_event

if TYPE_CHECKING:
    from ...config import Paths
    from ..agent import AgentRunner
    from .stages import StageResult, Workflow, WorkflowStage

logger = get_logger(__name__)


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


class RecoveryMixin:
    """Recovery / rescue / rollback methods mixed into ``WorkflowRunner``."""

    paths: "Paths"
    runner: "AgentRunner"

    # Default cap on chief recovery dispatches per stage. A stage that
    # still has unmet deps after this many recovery passes gets a final
    # WARN and is skipped, so we never loop forever on a stuck workflow.
    # Overridable via clk.config.json::recovery::max_per_stage.
    DEFAULT_MAX_RECOVERY_PER_STAGE = 3

    @property
    def max_recovery_per_stage(self) -> int:
        cfg = (self.runner.clk_cfg.get("recovery") or {})
        return int(cfg.get("max_per_stage") or self.DEFAULT_MAX_RECOVERY_PER_STAGE)

    @property
    def stall_rescue_enabled(self) -> bool:
        """When True, hitting the no-progress cap dispatches the chief once
        in *rescue mode* (restructure the plan or declare done) before the
        loop gives up. Overridable via clk.config.json::supervise::stall_rescue.
        """
        cfg = (self.runner.clk_cfg.get("supervise") or {})
        val = cfg.get("stall_rescue", True)
        return str(val).lower() not in ("false", "0", "off", "no")

    def _should_rollback(self, stage: "WorkflowStage") -> bool:
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
        stage: "WorkflowStage",
        unmet: List[str],
        result_by_id: Dict[str, "StageResult"],
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
        logger.warning(
            f"stage {stage.id} skipped after recovery limit: " + "; ".join(details),
        )

    def _dispatch_recovery(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        unmet: List[str],
        result_by_id: Dict[str, "StageResult"],
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
        logger.info(f"workflow {workflow.name}: dispatching chief recovery for stage {stage.id}")
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
        workflow: "Workflow",
        stage: "WorkflowStage",
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
        logger.info(
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
        workflow: "Workflow",
        cycle: int,
        cycle_results: List["StageResult"],
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
        logger.warning(f"workflow {workflow.name}: dispatching chief stall rescue (cycle {cycle})")
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
