"""Cognitive Loop Kernel TUI dashboard.

A curses-based front end inspired by gnhf / Archon. Decomposed package;
this ``__init__`` preserves the public surface of the former
``clk_harness/tui.py`` module:

* :mod:`.app` — ``TuiApp`` (the curses layout/render/key loop) and the
  ``run()`` entry point ``clk tui`` uses.
* :mod:`.dashboard` — ``DashboardState`` (thread-safe shared state),
  agent cards, log lines, and the ``DashboardObserver`` adapter.
* :mod:`.commands` — the ``Worker`` thread that executes slash-command
  ``Job``s off the UI thread.
* :mod:`.stream` — the stderr/stdout capture that routes stray writes
  into the log pane instead of corrupting the curses display.
* :mod:`.theme` — presentation helpers (token formatting, word wrap).
"""

from ..utils.text_extract import classify_error, extract_thought
from .app import TuiApp, run
from .commands import Job, Worker
from .dashboard import (
    AgentCard,
    AgentStatus,
    DashboardObserver,
    DashboardState,
    LogLine,
    _extract_thought,
)
from .stream import _StreamToLog
from .theme import _format_tokens, _word_wrap

__all__ = [
    "AgentCard",
    "AgentStatus",
    "DashboardObserver",
    "DashboardState",
    "Job",
    "LogLine",
    "TuiApp",
    "Worker",
    "_StreamToLog",
    "_extract_thought",
    "_format_tokens",
    "_word_wrap",
    "classify_error",
    "extract_thought",
    "run",
]
