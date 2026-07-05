"""stderr/stdout capture for the TUI.

Routes every write into the dashboard log pane so subprocess and
traceback output cannot reach the real terminal and corrupt the
curses display.
"""

from __future__ import annotations

import re

from ..log import get_logger
from .dashboard import DashboardState  # noqa: F401  (annotation use)

logger = get_logger(__name__)


class _StreamToLog:
    """File-like object that routes writes into the dashboard log pane.

    Replaces ``sys.stderr`` (and optionally ``sys.stdout``) while the TUI
    is active so that ``log()`` calls, ``print()`` statements, and
    ``traceback.print_exc()`` output never reach the terminal directly
    and therefore never corrupt the curses screen.

    Lines are buffered until a ``\\n`` is seen so partial writes from
    ``print(..., end="")`` don't produce noisy fragments. Severity is
    inferred from the ``[LEVEL]`` tag the harness's own logger emits;
    everything else falls back to the default level.
    """

    LEVEL_TAGS = ("[ERROR]", "[WARN]", "[INFO]")
    LOG_PREFIX_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s+\[(ERROR|WARN|INFO)\]\s*(.*)$"
    )

    def __init__(self, state: "DashboardState", default_level: str = "INFO") -> None:
        self.state = state
        self.default_level = default_level
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        out_len = len(s)
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            line = line.rstrip("\r")
            if not line.strip():
                continue
            level = self.default_level
            m = self.LOG_PREFIX_RE.match(line)
            if m:
                level = m.group(1)
                line = m.group(2)
            else:
                for tag in self.LEVEL_TAGS:
                    if tag in line:
                        level = tag.strip("[]")
                        break
            try:
                self.state.add_log(line[:300], level=level)
            except Exception:
                # Last resort: never raise from a stream.write.
                pass
        return out_len

    def flush(self) -> None:
        if self._buf.strip():
            try:
                self.state.add_log(self._buf[:300], level=self.default_level)
            except Exception:
                pass
            self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self):  # raise so subprocess.* doesn't grab us
        raise OSError("StreamToLog has no fileno")

