"""Logging helpers for CLK.

Writes both to stderr and to per-run log files inside ``.clk/logs/``.
Every caught exception in callers should pass through :func:`log_exception`,
which prefixes the location and prints a traceback.
"""

from __future__ import annotations

import datetime as _dt
import sys
import traceback
from pathlib import Path
from typing import Optional, TextIO

_LOG_FH: Optional[TextIO] = None
_LOG_PATH: Optional[Path] = None


def _ts() -> str:
    return _dt.datetime.now().isoformat(timespec="seconds")


def init_log_file(log_dir: Path, name: str = "clk") -> Path:
    """Open a log file in ``log_dir`` and return its path.

    Subsequent calls reopen a new file. The previous handle is closed.
    """
    global _LOG_FH, _LOG_PATH
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"{name}-{_dt.datetime.now().strftime('%Y%m%d-%H%M%S')}.log"
        if _LOG_FH is not None:
            try:
                _LOG_FH.close()
            except Exception as exc:
                print(f"[logging_utils.init_log_file] failed to close previous log: {exc}", file=sys.stderr)
                traceback.print_exc()
        _LOG_FH = path.open("a", encoding="utf-8")
        _LOG_PATH = path
        return path
    except Exception as exc:
        print(f"[logging_utils.init_log_file] failed: {exc}", file=sys.stderr)
        traceback.print_exc()
        return log_dir / "clk-fallback.log"


def current_log_path() -> Optional[Path]:
    return _LOG_PATH


def log(msg: str, level: str = "INFO") -> None:
    line = f"{_ts()} [{level}] {msg}"
    print(line, file=sys.stderr)
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(line + "\n")
            _LOG_FH.flush()
        except Exception as exc:
            print(f"[logging_utils.log] failed to write: {exc}", file=sys.stderr)
            traceback.print_exc()


def log_exception(where: str, exc: BaseException) -> None:
    """Standard exception logger. Call from every ``except`` block."""
    print(f"{_ts()} [ERROR] [{where}] {exc.__class__.__name__}: {exc}", file=sys.stderr)
    traceback.print_exc()
    if _LOG_FH is not None:
        try:
            _LOG_FH.write(f"{_ts()} [ERROR] [{where}] {exc.__class__.__name__}: {exc}\n")
            traceback.print_exc(file=_LOG_FH)
            _LOG_FH.flush()
        except Exception as inner:
            print(f"[logging_utils.log_exception] failed to write: {inner}", file=sys.stderr)
            traceback.print_exc()


def close_log() -> None:
    global _LOG_FH
    if _LOG_FH is not None:
        try:
            _LOG_FH.close()
        except Exception as exc:
            print(f"[logging_utils.close_log] failed: {exc}", file=sys.stderr)
            traceback.print_exc()
        finally:
            _LOG_FH = None
