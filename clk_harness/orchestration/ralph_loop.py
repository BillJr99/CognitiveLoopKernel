"""Ralph / gnhf-style iterative loop.

Each iteration:
  1. fresh-context Ralph agent reads ``.clk/state/*`` and the latest commits
  2. picks one measurable improvement
  3. dispatches an engineer + qa pass
  4. validation runs
  5. if validation passes, the iteration is committed
  6. otherwise the working tree is reset to the pre-iteration HEAD
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
from ..git_ops import (
    add_all,
    commit as git_commit,
    has_changes,
    head_sha,
    revert_to,
)
from ..utils.logging_utils import log, log_exception
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

    def run(self, *, dry_run: bool = False) -> List[IterationOutcome]:
        outcomes: List[IterationOutcome] = []
        for i in range(1, self.max_iterations + 1):
            outcome = self._iterate(i, dry_run=dry_run)
            outcomes.append(outcome)
            self._record(outcome)
            if self._is_done():
                log("ralph: completion criteria met; stopping")
                break
        return outcomes

    def _iterate(self, idx: int, *, dry_run: bool) -> IterationOutcome:
        started = datetime.now().isoformat(timespec="seconds")
        before = head_sha(self.paths.root)
        objective = f"Ralph iteration #{idx}: select and execute one measurable improvement."

        # 1. Plan with Ralph
        plan = self.runner.run(
            "ralph",
            objective,
            extra={"iteration": idx, "loop": "ralph"},
            dry_run=dry_run,
        )

        # 2. Engineer one slice
        eng_obj = (plan.response.text or "").strip().splitlines()[:1]
        eng_obj_text = eng_obj[0] if eng_obj else "Implement the next improvement"
        engineer = self.runner.run(
            "engineer",
            eng_obj_text,
            extra={"iteration": idx, "loop": "ralph", "from": "ralph"},
            dry_run=dry_run,
        )

        # 3. QA pass
        qa = self.runner.run(
            "qa",
            f"Audit changes from iteration #{idx} and validate.",
            extra={"iteration": idx, "loop": "ralph"},
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
            progress = self.paths.state / "progress.md"
            line = (
                f"- iter {outcome.index} @ {outcome.finished_at} "
                f"improved={outcome.improved} committed={outcome.committed} :: {outcome.objective}\n"
            )
            with progress.open("a", encoding="utf-8") as fh:
                fh.write(line)
        except Exception as exc:
            log_exception("orchestration.ralph_loop._record", exc)
