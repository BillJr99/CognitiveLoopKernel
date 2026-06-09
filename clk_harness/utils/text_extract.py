"""Provider-error classification and 'thought' extraction helpers.

These two pure-string helpers were originally defined in
:mod:`clk_harness.tui`, but ``tui.py`` imports :mod:`curses` at module
top, so importing them from there would drag curses into any process
that just wants the text logic (e.g. the REST API / web snapshot
builder, which runs headless under uvicorn).

They live here so both the TUI and the web layer share one
implementation without the curses dependency. ``tui.py`` re-imports
them from this module, so behavior is identical to before.
"""

from __future__ import annotations

from typing import Tuple


def classify_error(error: str) -> Tuple[str, str, str]:
    """Classify a provider error string.

    Returns ``(kind, resolution, suggested_command)`` where:
      * ``kind`` is one of: ``rate_limit``, ``timeout``, ``auth``,
        ``policy``, ``not_installed``, ``other``.
      * ``resolution`` is a short human sentence the user can act on.
      * ``suggested_command`` is the TUI slash command that would fix
        it (or empty when none applies).

    The hint bar uses ``suggested_command`` directly; the log uses
    ``resolution`` as the human prose.
    """
    msg = (error or "").lower()
    if "rate limit" in msg or "quota" in msg:
        return (
            "rate_limit",
            "provider rate/quota failure; back off and retry after the window resets or switch provider",
            "/provider",
        )
    if "timeout" in msg or "no output" in msg or "operation was aborted" in msg:
        return (
            "timeout",
            "provider call stalled/aborted; retries are reissued with backoff, then the cycle stops",
            "/abort",
        )
    if "api key" in msg or "authentication" in msg or "unauthorized" in msg or "forbidden" in msg:
        return (
            "auth",
            "provider auth/config failure; fix credentials via /configure or switch provider",
            "/configure",
        )
    if "no endpoints available" in msg or "guardrail restrictions" in msg or "data policy" in msg:
        return (
            "policy",
            "provider endpoint/policy routing issue; retries are reissued, then switch provider if they fail",
            "/provider",
        )
    if "cli not found" in msg or "not found" in msg:
        return (
            "not_installed",
            "provider executable/config missing; install via /install or switch provider",
            "/install",
        )
    return (
        "other",
        "provider failure; workflow recovery is aborted until the provider is fixed or changed",
        "",
    )


def extract_thought(text: str) -> str:
    """Pull a single 'thinking' line out of an agent's response.

    Scans for common markers (Q:, Hypothesis:, Decision:, PROPOSE_ROLE:,
    PROPOSE_WORKFLOW:, Risk:, Next:) and returns the first match. Used
    as the rotating ``thought`` view in the agent cards and the web
    dashboard.
    """
    if not text:
        return ""
    markers = (
        "Q:",
        "Question:",
        "Hypothesis:",
        "Decision:",
        "Risk:",
        "Risks:",
        "Next:",
        "PROPOSE_ROLE:",
        "PROPOSE_WORKFLOW:",
    )
    for line in text.splitlines():
        s = line.strip()
        for m in markers:
            if s.startswith(m):
                return s[:240]
    return ""


__all__ = ["classify_error", "extract_thought"]
