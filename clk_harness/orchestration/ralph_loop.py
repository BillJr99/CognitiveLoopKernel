"""Ralph / gnhf-style iterative loop.

Each iteration:
  1. fresh-context Ralph agent reads ``.clk/state/*`` and the latest commits
  2. picks one measurable improvement
  3. dispatches an engineer + qa pass
  4. validation runs
  5. if validation passes, the iteration is committed
  6. otherwise the working tree is reset to the pre-iteration HEAD

When ``robustness.plateau_window`` consecutive iterations stop showing
improvement, the loop escalates (forces consensus fan-out on the next
plan/engineer/qa via ``careful=true`` in extra), then reframes (asks the
chief to re-author the workflow), and finally terminates gracefully
rather than burning the full iteration budget. Same protocol applies
to regression — a failing iteration that follows a passing run
triggers a critic dispatch before the next plan.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..git_ops import (
    add_all,
    commit as git_commit,
    has_changes,
    head_sha,
    revert_to,
)
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from . import response_quality as _response_quality
from .agent import AgentRunner
from .evaluator import Evaluator, EvalResult


@dataclass
class IterationOutcome:
    index: int
    started_at: str
    finished_at: str
    objective: str
    improved: bool
    committed: bool
    eval_summary: str
    sha_before: Optional[str]
    sha_after: Optional[str]


class RalphLoop:
    def __init__(
        self,
        paths: Paths,
        runner: AgentRunner,
        evaluator: Evaluator,
        *,
        max_iterations: int = 20,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.evaluator = evaluator
        self.max_iterations = max_iterations

    def _observer_log(self, line: str) -> None:
        """Mirror a status line to both the log file and the TUI status pane."""
        log(line)
        obs = getattr(self.runner, "observer", None)
        if obs is not None:
            try:
                obs.log(line)
            except Exception:
                pass

    def run(self, *, dry_run: bool = False) -> List[IterationOutcome]:
        outcomes: List[IterationOutcome] = []
        plateau_streak = 0
        for i in range(1, self.max_iterations + 1):
            adaptive_extra = self._adaptive_extra(outcomes)
            outcome = self._iterate(i, dry_run=dry_run, adaptive_extra=adaptive_extra)
            outcomes.append(outcome)
            self._record(outcome)
            if self._is_done():
                log("ralph: completion criteria met; stopping")
                break
            # Plateau / regression detection runs only when we have
            # enough history; on the first few iterations we let the
            # loop warm up.
            verdict = self._progress_verdict(outcomes)
            if verdict == "plateau":
                plateau_streak += 1
                self._handle_plateau(i, plateau_streak, dry_run=dry_run)
                if self._should_terminate_for_plateau(plateau_streak):
                    self._write_plateau_done(i, plateau_streak)
                    break
            elif verdict == "regressing":
                plateau_streak = 0
                self._handle_regression(i, dry_run=dry_run)
            else:
                plateau_streak = 0
        return outcomes

    # -- adaptive helpers ------------------------------------------------

    def _robustness_cfg(self) -> Dict[str, Any]:
        return dict(self.runner.clk_cfg.get("robustness") or {})

    def _plateau_window(self) -> int:
        try:
            return max(2, int(self._robustness_cfg().get("plateau_window") or 3))
        except (TypeError, ValueError):
            return 3

    def _plateau_action(self) -> str:
        action = str(self._robustness_cfg().get("plateau_action") or "escalate_then_reframe").lower()
        return action

    def _progress_verdict(self, outcomes: List[IterationOutcome]) -> str:
        """Return one of ``"improving" | "plateau" | "regressing"``."""
        window = self._plateau_window()
        if len(outcomes) < window:
            return "improving"
        recent = outcomes[-window:]
        improvements = sum(1 for o in recent if o.improved)
        if improvements == 0:
            return "plateau"
        # Regression: last iteration failed after at least one prior
        # iteration in the window passed.
        if not recent[-1].improved and any(o.improved for o in recent[:-1]):
            return "regressing"
        return "improving"

    def _adaptive_extra(self, outcomes: List[IterationOutcome]) -> Dict[str, Any]:
        """Extra fields injected into the next iteration's dispatches.

        Right now this is just the ``careful`` flag: when we're stuck,
        force consensus fan-out (the wrapper in
        :class:`AgentRunner.run` treats ``careful=true`` as a trigger
        for ``auto_consensus`` and ``auto_refine``).
        """
        if not outcomes or self._progress_verdict(outcomes) == "improving":
            return {}
        return {"careful": True, "loop_adaptive": True}

    def _handle_plateau(self, idx: int, streak: int, *, dry_run: bool) -> None:
        action = self._plateau_action()
        log_event(
            self.paths,
            "ralph_plateau_detected",
            agent="ralph",
            iteration=idx,
            streak=streak,
            action=action,
        )
        log(
            f"ralph: plateau detected at iteration {idx} (streak={streak}); "
            f"action={action}",
            level="WARN",
        )
        if action in ("escalate_only", "escalate_then_reframe"):
            # Escalation is already wired via _adaptive_extra setting
            # careful=true on the next iteration; just record the intent.
            log_event(self.paths, "ralph_plateau_escalate", agent="ralph", iteration=idx)
            self._observer_log(
                f"ralph #{idx} :: plateau escalate :: enabling consensus fan-out (streak={streak})"
            )
        if action in ("reframe_only", "escalate_then_reframe") and streak >= 2 and not dry_run:
            try:
                self._observer_log(
                    f"ralph #{idx} :: plateau reframe :: dispatching chief to re-cast workflow "
                    f"(streak={streak})"
                )
                self.runner.run(
                    "chief",
                    (
                        f"Plateau dispatch — Ralph has run {streak} consecutive "
                        "iterations without measurable improvement. Re-cast roles "
                        "or re-author the engineering workflow with a "
                        "PROPOSE_WORKFLOW so the next iterations attempt a "
                        "qualitatively different approach (new metric, new "
                        "experiment family). Avoid another marginal tweak."
                    ),
                    extra={
                        "phase": "recovery",
                        "loop": "ralph",
                        "iteration": idx,
                        "plateau_streak": streak,
                    },
                    dry_run=dry_run,
                )
            except Exception as exc:
                log_exception("orchestration.ralph_loop._handle_plateau.reframe", exc)

    def _handle_regression(self, idx: int, *, dry_run: bool) -> None:
        log_event(self.paths, "ralph_regression_detected", agent="ralph", iteration=idx)
        log(f"ralph: regression detected at iteration {idx}", level="WARN")
        self._observer_log(f"ralph #{idx} :: regression detected :: dispatching critic")
        if dry_run:
            return
        # Ask the critic to look at what just broke before we plan the
        # next iteration. The critic posts to the blackboard and
        # subsequent ralph runs will see it via $blackboard_digest.
        try:
            self.runner.run(
                "critic",
                (
                    f"Ralph regression check — the iteration before #{idx + 1} "
                    "regressed (it ran but did not improve the metric, after a "
                    "prior passing run). Read the latest commit and the "
                    "evaluator output, identify what broke, and post a brief "
                    "POST: critique block so the next ralph iteration avoids "
                    "the same trap."
                ),
                extra={"phase": "recovery", "loop": "ralph", "iteration": idx},
                dry_run=dry_run,
            )
        except Exception as exc:
            log_exception("orchestration.ralph_loop._handle_regression", exc)

    def _should_terminate_for_plateau(self, streak: int) -> bool:
        action = self._plateau_action()
        # Off → never terminate from plateau detection.
        if action in ("off", "false", "0", ""):
            return False
        # After the escalate + reframe attempts plus two more chances to
        # break out, give up gracefully.
        return streak >= max(2, self._plateau_window()) + 2

    def _write_plateau_done(self, idx: int, streak: int) -> None:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            (self.paths.state / "done.md").write_text(
                f"# Ralph plateau termination\n\n"
                f"Stopped at iteration {idx} after {streak} consecutive "
                "iterations without measurable improvement.\n"
                "Escalation + reframe attempts did not break the plateau, "
                "so the loop terminated gracefully rather than burning the "
                "remaining iteration budget.\n",
                encoding="utf-8",
            )
            log_event(
                self.paths,
                "ralph_plateau_terminated",
                agent="ralph",
                iteration=idx,
                streak=streak,
            )
        except Exception as exc:
            log_exception("orchestration.ralph_loop._write_plateau_done", exc)

    def _iterate(
        self,
        idx: int,
        *,
        dry_run: bool,
        adaptive_extra: Optional[Dict[str, Any]] = None,
    ) -> IterationOutcome:
        started = datetime.now().isoformat(timespec="seconds")
        before = head_sha(self.paths.root)
        objective = f"Ralph iteration #{idx}: select and execute one measurable improvement."
        base_extra: Dict[str, Any] = {"iteration": idx, "loop": "ralph"}
        base_extra.update(adaptive_extra or {})

        # 1. Plan with Ralph
        self._observer_log(f"ralph #{idx} :: plan :: dispatching ralph")
        plan = self.runner.run(
            "ralph",
            objective,
            extra=base_extra,
            dry_run=dry_run,
        )

        # Quality guard: a Ralph that returned empty / malformed
        # planner output can't drive a productive iteration, so we
        # short-circuit rather than re-using whatever stray line happens
        # to be at index 0.
        plan_quality = _response_quality.score(
            plan.response.text,
            min_chars=int((self._robustness_cfg().get("min_response_chars") or 40)),
        )
        eng_obj_lines = (plan.response.text or "").strip().splitlines()
        if not plan_quality.ok and not plan_quality.recoverable:
            eng_obj_text = ""
        else:
            eng_obj_text = eng_obj_lines[0] if eng_obj_lines else ""
        if not eng_obj_text:
            log_event(
                self.paths,
                "ralph_iteration_skipped_low_quality",
                agent="ralph",
                iteration=idx,
                plan_quality=plan_quality.summary(),
                flags=list(plan_quality.flags),
            )
            log(
                f"ralph #{idx}: skipping — planner returned no usable objective",
                level="WARN",
            )
            finished = datetime.now().isoformat(timespec="seconds")
            return IterationOutcome(
                index=idx,
                started_at=started,
                finished_at=finished,
                objective="(planner produced no objective; iteration skipped)",
                improved=False,
                committed=False,
                eval_summary="skipped: planner low quality",
                sha_before=before,
                sha_after=before,
            )

        # 2. Engineer one slice
        engineer_extra = dict(base_extra)
        engineer_extra["from"] = "ralph"
        self._observer_log(f"ralph #{idx} :: engineer :: {eng_obj_text[:60]}")
        engineer = self.runner.run(
            "engineer",
            eng_obj_text,
            extra=engineer_extra,
            dry_run=dry_run,
        )

        # 3. QA pass
        self._observer_log(f"ralph #{idx} :: qa :: dispatching audit pass")
        qa = self.runner.run(
            "qa",
            f"Audit changes from iteration #{idx} and validate.",
            extra=base_extra,
            dry_run=dry_run,
        )

        # 4. Validate via configured checks
        eval_result = self.evaluator.run()

        # 5. Commit or revert
        committed = False
        if not dry_run:
            if eval_result.ok and engineer.response.ok and has_changes(self.paths.root):
                if add_all(self.paths.root):
                    committed = git_commit(
                        self.paths.root,
                        agent="ralph",
                        objective=eng_obj_text,
                        files_changed=engineer.files_written,
                        validation=eval_result.summary(),
                        next_step="select next improvement",
                        body_extra=f"iteration {idx}; qa ok={qa.response.ok}",
                    )
            elif not eval_result.ok and before:
                log(f"ralph #{idx}: validation failed; reverting to {before[:8]}", level="WARN")
                revert_to(self.paths.root, before)

        finished = datetime.now().isoformat(timespec="seconds")
        return IterationOutcome(
            index=idx,
            started_at=started,
            finished_at=finished,
            objective=eng_obj_text,
            improved=eval_result.ok and engineer.response.ok,
            committed=committed,
            eval_summary=eval_result.summary(),
            sha_before=before,
            sha_after=head_sha(self.paths.root),
        )

    def _is_done(self) -> bool:
        return (self.paths.state / "done.md").exists()

    def _record(self, outcome: IterationOutcome) -> None:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            with (self.paths.state / "experiments.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(outcome.__dict__) + "\n")
            # Write PROGRESS.md to the project root so agents can read and
            # append to it (agents cannot write .clk/state/ via ACTIONs).
            progress = self.paths.root / "PROGRESS.md"
            line = (
                f"- iter {outcome.index} @ {outcome.finished_at} "
                f"improved={outcome.improved} committed={outcome.committed} :: {outcome.objective}\n"
            )
            with progress.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            log_exception("orchestration.ralph_loop._record", exc)
