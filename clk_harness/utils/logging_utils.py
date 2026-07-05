"""Logging helpers for CLK (compatibility shim).

The implementation now lives in :mod:`clk_harness.log`, which builds the
same ``<iso-ts> [LEVEL] message`` output on stdlib :mod:`logging` and adds
per-module loggers via :func:`clk_harness.log.get_logger`. This module
re-exports the legacy helpers so existing imports keep working:

- :func:`log` / :func:`log_exception` — one-shot diagnostics
- :func:`init_log_file` / :func:`close_log` / :func:`current_log_path` —
  per-run log files inside ``.clk/logs/``
"""

from __future__ import annotations

from ..log import (  # noqa: F401  (re-exported legacy API)
    close_log,
    current_log_path,
    get_logger,
    init_log_file,
    log,
    log_exception,
)

__all__ = [
    "close_log",
    "current_log_path",
    "get_logger",
    "init_log_file",
    "log",
    "log_exception",
]
