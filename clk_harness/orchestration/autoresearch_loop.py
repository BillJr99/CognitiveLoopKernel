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
from typing import List, Optional

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes
from ..utils.logging_utils import log, log_exception
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

    def _step(self, idx: int, *, dry_run: bool) -> Experiment:
        started = datetime.now().isoformat(timespec="seconds")
        survey = self.runner.run(
            "ralph",
            f"Autoresearch step #{idx}: survey state and propose next experiment.",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )
        question_lines = (survey.response.text or "").strip().splitlines()
        question = next(
            (l for l in question_lines if l.strip().startswith(("Q:", "Question:", "Hypothesis:"))),
            f"Open question #{idx}",
        )

        analyst = self.runner.run(
            "analyst",
            f"Investigate: {question}",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )
        critic = self.runner.run(
            "critic",
            f"Critique findings on: {question}",
            extra={"iteration": idx, "loop": "autoresearch"},
            dry_run=dry_run,
        )

        finding_preview = (analyst.response.text or "")[:400]
        committed = False
        if not dry_run and has_changes(self.paths.root) and analyst.response.ok:
            if add_all(self.paths.root):
                committed = git_commit(
                    self.paths.root,
                    agent="autoresearch",
                    objective=question,
                    files_changed=analyst.files_written,
                    validation=f"critic ok={critic.response.ok}",
                    next_step="select next question",
                    body_extra=finding_preview,
                )
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
