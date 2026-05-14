"""Console-script shim for the ``clk-api`` entry point.

This module is intentionally kept free of any FastAPI / uvicorn imports so
that the ``clk-api`` command can produce a friendly error message when the
``[api]`` optional-dependency group has not been installed, rather than
crashing with an ``ImportError`` traceback before ``main()`` is even called.

The real application lives in ``clk_harness.api``; this shim merely guards
the import and delegates to that module's ``main()``.
"""

from __future__ import annotations


def main() -> None:  # pragma: no cover
    """Entry point for the ``clk-api`` console script."""
    try:
        from clk_harness.api import main as _api_main
    except ImportError:
        import sys
        print(
            "Error: REST API dependencies are not installed.\n"
            "Install them with:\n"
            "    pip install 'clk-harness[api]'",
            file=sys.stderr,
        )
        raise SystemExit(1)
    _api_main()
