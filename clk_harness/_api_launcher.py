"""Background launcher for the REST API.

Used by the CLI to start uvicorn on a daemon thread before dispatching the
requested sub-command, so the REST API auto-starts during normal CLI/TUI use.

Imports of ``fastapi`` / ``uvicorn`` / ``clk_harness.api`` are deferred to
``start_api_in_background()`` so that a missing install does not crash the CLI.
Install all dependencies with ``pip install -r requirements.txt``.
"""

from __future__ import annotations

import logging
import os
import sys
import threading
from typing import Optional

logger = logging.getLogger(__name__)


def _truthy(value: Optional[str]) -> bool:
    if not value:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def api_disabled_by_env() -> bool:
    """Return True if the user opted out via ``CLK_DISABLE_API``."""
    return _truthy(os.environ.get("CLK_DISABLE_API"))


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
    out = log_stream if log_stream is not None else sys.stderr

    if disable or api_disabled_by_env():
        return None

    # Lazy import so missing dependencies do not crash the CLI.
    try:
        import uvicorn

        from clk_harness.api import app, get_bind_host, get_bind_port
    except ImportError as exc:
        print(
            f"[clk] REST API disabled: optional dependencies missing ({exc}). "
            f"Active interpreter: {sys.executable}. "
            f"Install with: {sys.executable} -m pip install -r requirements.txt. "
            "Continuing without the API.",
            file=out,
        )
        return None

    host = get_bind_host()
    port = get_bind_port()

    def _run() -> None:
        try:
            config = uvicorn.Config(
                app,
                host=host,
                port=port,
                log_level=os.environ.get("CLK_API_LOG_LEVEL", "warning"),
                access_log=False,
            )
            server = uvicorn.Server(config)
            server.run()
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
            "container use. Unset CLK_API_HOST (or set it to 127.0.0.1) to "
            "restrict to loopback, or pass --no-api / CLK_DISABLE_API=1 to disable.",
            file=out,
        )
    return thread
