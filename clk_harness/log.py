"""Structured logging for CLK, built on stdlib :mod:`logging`.

This module is the single sink for harness diagnostics. It preserves the
established user-visible line format::

    2026-07-05T18:32:29 [INFO] workflow done: engineering

on **stderr**, and mirrors every record into the per-run log file under
``.clk/logs/`` opened by :func:`init_log_file` (historically managed by
``clk_harness.utils.logging_utils``, which now delegates here).

Usage for new code::

    from clk_harness.log import get_logger

    logger = get_logger(__name__)
    logger.info("workflow done: %s", name)

The legacy helpers (:func:`log`, :func:`log_exception`, :func:`init_log_file`,
:func:`close_log`, :func:`current_log_path`) keep their exact signatures and
output so existing callers and the TUI's log-file tailing keep working.

The default level is INFO; set ``CLK_LOG_LEVEL=DEBUG`` to surface the
diagnostic breadcrumbs attached to swallowed exceptions.
"""

from __future__ import annotations

import datetime as _dt
import logging
import os
import sys
import traceback
from pathlib import Path
from typing import Optional

_ROOT_NAME = "clk"

# Historic tags: the harness always printed "[WARN]", not "[WARNING]".
_LEVEL_TAGS = {"WARNING": "WARN"}
_LEVEL_NUMBERS = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARN": logging.WARNING,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}

_FILE_HANDLER: Optional[logging.FileHandler] = None
_LOG_PATH: Optional[Path] = None


class _ClkFormatter(logging.Formatter):
    """``<iso-ts> [LEVEL] message`` — the harness's historic line format."""

    def format(self, record: logging.LogRecord) -> str:
        ts = _dt.datetime.fromtimestamp(record.created).isoformat(timespec="seconds")
        level = _LEVEL_TAGS.get(record.levelname, record.levelname)
        line = f"{ts} [{level}] {record.getMessage()}"
        if record.exc_info:
            line = f"{line}\n{self.formatException(record.exc_info)}"
        return line


def _env_level() -> int:
    raw = (os.environ.get("CLK_LOG_LEVEL") or "INFO").strip().upper()
    return _LEVEL_NUMBERS.get(raw, logging.INFO)


def _root() -> logging.Logger:
    """Return the configured ``clk`` root logger (configured once)."""
    logger = logging.getLogger(_ROOT_NAME)
    if not getattr(logger, "_clk_configured", False):
        logger.setLevel(_env_level())
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(_ClkFormatter())
        logger.addHandler(handler)
        # Don't double-print through the stdlib root logger.
        logger.propagate = False
        logger._clk_configured = True  # type: ignore[attr-defined]
    return logger


def get_logger(name: str = "") -> logging.Logger:
    """Return a per-module logger below the ``clk`` root.

    Pass ``__name__``; a leading ``clk_harness`` package prefix is folded
    into the ``clk`` root so records inherit its handlers and level.
    """
    _root()
    if not name:
        return logging.getLogger(_ROOT_NAME)
    if name == "clk_harness" or name.startswith("clk_harness."):
        name = _ROOT_NAME + name[len("clk_harness"):]
    elif name != _ROOT_NAME and not name.startswith(_ROOT_NAME + "."):
        name = f"{_ROOT_NAME}.{name}"
    return logging.getLogger(name)


def init_log_file(log_dir: Path, name: str = "clk") -> Path:
    """Open a log file in ``log_dir`` and mirror all records into it.

    Subsequent calls open a new file; the previous handler is closed.
    """
    global _FILE_HANDLER, _LOG_PATH
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        root = _root()
        if _FILE_HANDLER is not None:
            try:
                root.removeHandler(_FILE_HANDLER)
                _FILE_HANDLER.close()
            except Exception as exc:
                print(f"[clk.log.init_log_file] failed to close previous log: {exc}", file=sys.stderr)
                traceback.print_exc()
        handler = logging.FileHandler(path, mode="a", encoding="utf-8", delay=False)
        handler.setFormatter(_ClkFormatter())
        root.addHandler(handler)
        _FILE_HANDLER = handler
        _LOG_PATH = path
        return path
    except Exception as exc:
        print(f"[clk.log.init_log_file] failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return log_dir / "clk-fallback.log"


def current_log_path() -> Optional[Path]:
    return _LOG_PATH


def log(msg: str, level: str = "INFO") -> None:
    """Legacy one-shot logger: ``<ts> [LEVEL] msg`` to stderr + log file."""
    _root().log(_LEVEL_NUMBERS.get(level.upper(), logging.INFO), "%s", msg)


def log_exception(where: str, exc: BaseException) -> None:
    """Standard exception logger. Call from every ``except`` block."""
    _root().error("[%s] %s: %s", where, exc.__class__.__name__, exc, exc_info=exc)


def close_log() -> None:
    global _FILE_HANDLER
    if _FILE_HANDLER is not None:
        try:
            _root().removeHandler(_FILE_HANDLER)
            _FILE_HANDLER.close()
        except Exception as exc:
            print(f"[clk.log.close_log] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
        finally:
            _FILE_HANDLER = None
