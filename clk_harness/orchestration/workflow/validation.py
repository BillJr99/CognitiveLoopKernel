"""Validation and done-gate integration for the workflow runner.

Stage validation commands, the outputs contract check, the stage
commit, and the done-gate (FM2) stop logic.
"""

from __future__ import annotations

import subprocess
from typing import TYPE_CHECKING, List, Optional

from ...git_ops import add_all, commit_trace, has_changes
from ...git_ops import commit as git_commit
from ...log import get_logger, log_exception
from ...utils.activity_log import log_event
from .. import blackboard as _blackboard
from .. import charter as _charter
from .. import done_gate as _done_gate
from .. import evaluator as _evaluator
from .. import noop_guard as _noop_guard

if TYPE_CHECKING:
    from ...config import Paths
    from ..agent import AgentRun, AgentRunner
    from ..telemetry import CycleTelemetry
    from .stages import Workflow, WorkflowStage

logger = get_logger(__name__)


class ValidationMixin:
    """Validation / done-gate / commit methods mixed into ``WorkflowRunner``."""

    paths: "Paths"
    runner: "AgentRunner"
    telemetry: Optional["CycleTelemetry"]

    # -- done gate (FM2) ---------------------------------------------------

    def _done_gate_enabled(self) -> bool:
        cfg = (self.runner.clk_cfg.get("done_gate") or {})
        return bool(cfg.get("enabled", True))

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

    def _stop_requested(self, workflow: "Workflow") -> bool:
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
            except Exception as _exc:
                logger.debug("telemetry record_done_gate failed: %s", _exc)
        if verdict.passed:
            self._grant_done(verdict)
            return True
        # Reject: downgrade the request so a later cycle can re-earn it.
        try:
            done_md.rename(state / "done_requested.md")
        except Exception:
            try:
                done_md.unlink()
            except Exception as _exc:
                logger.debug("could not clear done request %s: %s", done_md, _exc)
        logger.warning(
            f"workflow {workflow.name}: ACTION:done REJECTED by done-gate — "
            f"unmet: {', '.join(verdict.failures) or '?'}",
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
        except Exception as _exc:
            logger.debug("done-gate trace commit failed: %s", _exc)

    # -- outputs contract / validation / commit --------------------------

    def _check_outputs_contract(self, stage: "WorkflowStage") -> List[str]:
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

    def _validate(self, stage: "WorkflowStage") -> tuple[bool, str]:
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
        workflow: "Workflow",
        stage: "WorkflowStage",
        run: "AgentRun",
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
