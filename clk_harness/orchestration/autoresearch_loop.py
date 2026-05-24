"""Karpathy-style autoresearch loop, driven by the ralph agent.

Repeatedly:
  1. ralph surveys what is known and picks the highest-value open question
  2. analyst investigates the question
  3. critic reviews the finding
  4. record the learning, regardless of pass/fail

Ralph handles both refinement mode (pick one improvement → implement →
validate) and research mode (survey → question → experiment → record).
This loop puts ralph in research mode by framing each iteration as an
autoresearch step.
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
from ..git_ops import add_all, commit as git_commit, has_changes, head_sha, revert_to
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from . import response_quality as _response_quality
from .agent import AgentRunner
from .evaluator import Evaluator


@dataclass
class Experiment:
    index: int
    started_at: str
    finished_at: str
    question: str
    finding: str
    committed: bool


class AutoresearchLoop:
    def __init__(
        self,
        paths: Paths,
        runner: AgentRunner,
        evaluator: Evaluator,
        *,
        max_iterations: int = 10,
    ) -> None:
        self.paths = paths
        self.runner = runner
        self.evaluator = evaluator
        self.max_iterations = max_iterations

    def run(self, *, dry_run: bool = False) -> List[Experiment]:
        out: List[Experiment] = []
        for i in range(1, self.max_iterations + 1):
            exp = self._step(i, dry_run=dry_run)
            out.append(exp)
            self._record(exp)
            if (self.paths.state / "done.md").exists():
                break
        return out

    def _robustness_cfg(self) -> Dict[str, Any]:
        return dict(self.runner.clk_cfg.get("robustness") or {})

    def _observer_log(self, line: str) -> None:
        """Mirror a status line to both the log file and the TUI status pane."""
        log(line)
        obs = getattr(self.runner, "observer", None)
        if obs is not None:
            try:
                obs.log(line)
            except Exception:
                pass

    def _step(self, idx: int, *, dry_run: bool) -> Experiment:
        started = datetime.now().isoformat(timespec="seconds")
        before = head_sha(self.paths.root)
        min_chars = int(self._robustness_cfg().get("min_response_chars") or 40)
        self._observer_log(f"autoresearch #{idx} :: survey :: dispatching ralph")
        survey = self.runner.run(
            "ralph",
            f"Autoresearch step #{idx}: survey state and propose next experiment.",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )
        survey_quality = _response_quality.score(survey.response.text, min_chars=min_chars)
        if not survey_quality.ok and not survey_quality.recoverable:
            log_event(
                self.paths,
                "autoresearch_step_skipped_low_quality",
                agent="ralph",
                iteration=idx,
                survey_quality=survey_quality.summary(),
                flags=list(survey_quality.flags),
            )
            log(
                f"autoresearch #{idx}: skipping — survey returned no usable text",
                level="WARN",
            )
            finished = datetime.now().isoformat(timespec="seconds")
            return Experiment(
                index=idx,
                started_at=started,
                finished_at=finished,
                question="(survey produced no question; step skipped)",
                finding="",
                committed=False,
            )
        question_lines = (survey.response.text or "").strip().splitlines()
        question = next(
            (l for l in question_lines if l.strip().startswith(("Q:", "Question:", "Hypothesis:"))),
            f"Open question #{idx}",
        )

        self._observer_log(f"autoresearch #{idx} :: analyst :: investigating: {question[:60]}")
        analyst = self.runner.run(
            "analyst",
            f"Investigate: {question}",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )
        self._observer_log(f"autoresearch #{idx} :: critic :: reviewing findings")
        critic = self.runner.run(
            "critic",
            f"Critique findings on: {question}",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )

        finding_preview = (analyst.response.text or "")[:400]
        committed = False
        # Evaluator gate: if the analyst's writes broke the build,
        # revert to the pre-step HEAD rather than committing a broken
        # state. Same protocol Ralph already uses.
        if not dry_run and has_changes(self.paths.root) and analyst.response.ok:
            eval_result = self.evaluator.run()
            if eval_result.ok:
                if add_all(self.paths.root):
                    committed = git_commit(
                        self.paths.root,
                        agent="autoresearch",
                        objective=question,
                        files_changed=analyst.files_written,
                        validation=f"critic ok={critic.response.ok}; "
                                   f"eval={eval_result.summary()[:200]}",
                        next_step="select next question",
                        body_extra=finding_preview,
                    )
            elif before:
                log_event(
                    self.paths,
                    "autoresearch_revert",
                    agent="autoresearch",
                    iteration=idx,
                    eval_summary=eval_result.summary()[:400],
                    sha_before=before,
                )
                log(
                    f"autoresearch #{idx}: evaluator failed; reverting to "
                    f"{before[:8]}",
                    level="WARN",
                )
                revert_to(self.paths.root, before)
        finished = datetime.now().isoformat(timespec="seconds")
        return Experiment(
            index=idx,
            started_at=started,
            finished_at=finished,
            question=question,
            finding=finding_preview,
            committed=committed,
        )

    def _record(self, exp: Experiment) -> None:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            with (self.paths.state / "experiments.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(exp.__dict__) + "\n")
        except Exception as exc:
            log_exception("orchestration.autoresearch_loop._record", exc)
