"""Background launcher for the REST API.

Used by the CLI to start uvicorn on a daemon thread before dispatching the
requested sub-command, so the REST API auto-starts during normal CLI/TUI use.

uvicorn is launched as a subprocess (``python -m uvicorn``) rather than being
imported directly, so the REST API works as long as uvicorn is installed in
the active Python environment — no ``pip install .`` required.
"""

from __future__ import annotations

import logging
import os
import subprocess
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)

_DEFAULT_HOST = "0.0.0.0"
_DEFAULT_PORT = 8001
_APP = "clk_harness.api:app"

# Module-level reference so an atexit / signal handler can terminate it.
_api_proc: Optional[subprocess.Popen] = None


def _truthy(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def api_disabled_by_env() -> bool:
    """Return True if the user opted out via ``CLK_DISABLE_API``."""
    return _truthy(os.environ.get("CLK_DISABLE_API"))


def _get_host() -> str:
    return os.environ.get("CLK_API_HOST", _DEFAULT_HOST)


def _get_port() -> int:
    try:
        return int(os.environ.get("CLK_API_PORT", str(_DEFAULT_PORT)))
    except ValueError:
        return _DEFAULT_PORT


def start_api_in_background(
    *,
    disable: bool = False,
    log_stream=None,
) -> Optional[threading.Thread]:
    """Start the REST API on a background daemon thread.

    Parameters
    ----------
    disable:
        If True, skip startup entirely (caller's ``--no-api`` flag wins).
    log_stream:
        Optional file-like stream for user-facing messages (defaults to
        ``sys.stderr``).  Same target the rest of the CLI uses.

    Returns
    -------
    The daemon thread if started, otherwise None.  Failures are logged and
    swallowed — the CLI must keep running even when the API cannot start.
    """
    global _api_proc

    out = log_stream if log_stream is not None else sys.stderr

    if disable or api_disabled_by_env():
        return None

    host = _get_host()
    port = _get_port()
    log_level = os.environ.get("CLK_API_LOG_LEVEL", "warning")

    cmd = [
        sys.executable, "-m", "uvicorn",
        _APP,
        "--host", host,
        "--port", str(port),
        "--log-level", log_level,
        "--no-access-log",
    ]

    def _run() -> None:
        global _api_proc
        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            _api_proc = proc
            _, stderr_bytes = proc.communicate()
            if proc.returncode not in (0, -15, -2):  # 0=clean, -15=SIGTERM, -2=SIGINT
                msg = (stderr_bytes or b"").decode(errors="replace").strip()
                logger.warning("REST API process exited rc=%s: %s", proc.returncode, msg)
                print(f"[clk] REST API stopped (rc={proc.returncode}): {msg}", file=out)
        except FileNotFoundError:
            print(
                "[clk] REST API disabled: uvicorn not found. "
                "Run `pip install -r requirements.txt` to install dependencies. "
                "Continuing without the API.",
                file=out,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("REST API server thread exited: %s", exc)
            print(f"[clk] REST API server stopped: {exc}", file=out)

    thread = threading.Thread(
        target=_run,
        name="clk-api",
        daemon=True,
    )
    thread.start()

    print(f"[clk] REST API starting on http://{host}:{port}", file=out)
    if host == "0.0.0.0":
        print(
            "[clk] WARNING: REST API is bound to 0.0.0.0 (all interfaces) "
            "and has NO authentication. Intended for isolated sandbox / "
            "container use. Set CLK_API_HOST=127.0.0.1 to restrict to "
            "loopback, or pass --no-api / CLK_DISABLE_API=1 to disable.",
            file=out,
        )
    return thread
