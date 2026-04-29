"""Lightweight scheduler.

CLK does not run as a daemon, but the loops accept a callable schedule
hook. The scheduler is a thin wrapper around ``time.sleep`` and a
``stop_after_seconds`` budget.
"""

from __future__ import annotations

import sys
import time
import traceback
from dataclasses import dataclass
from typing import Callable, Optional

from ..utils.logging_utils import log, log_exception


@dataclass
class Scheduler:
    interval_s: float = 0.0
    budget_s: Optional[float] = None

    def loop(self, fn: Callable[[int], bool], *, max_iterations: int = 0) -> int:
        """Run ``fn(idx)`` until it returns False or limits are reached.

        Returns the number of iterations actually executed.
        """
        start = time.monotonic()
        i = 0
        while True:
            i += 1
            try:
                cont = fn(i)
            except Exception as exc:
                log_exception("orchestration.scheduler.loop", exc)
                cont = False
            if not cont:
                break
            if max_iterations and i >= max_iterations:
                break
            if self.budget_s is not None and (time.monotonic() - start) >= self.budget_s:
                log("scheduler: budget exhausted")
                break
            if self.interval_s > 0:
                time.sleep(self.interval_s)
        return i
