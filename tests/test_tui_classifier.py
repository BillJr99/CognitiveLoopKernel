"""Tests for the TUI error classifier and the hint-bar mapping.

We don't drive the curses front-end here — we test the pure-Python
classifier that the hint bar and the worker both consume so the user
sees consistent error → action mappings.
"""

from clk_harness.tui import classify_error


def test_classify_not_installed():
    kind, _, cmd = classify_error("claude cli not found")
    assert kind == "not_installed"
    assert cmd == "/install"


def test_classify_rate_limit():
    kind, _, cmd = classify_error("HTTP 429: rate limit exceeded")
    assert kind == "rate_limit"
    assert cmd == "/provider"


def test_classify_timeout():
    kind, _, cmd = classify_error("timeout after 60s")
    assert kind == "timeout"
    assert cmd == "/abort"


def test_classify_auth():
    kind, _, cmd = classify_error("401 unauthorized: invalid api key")
    assert kind == "auth"
    assert cmd == "/configure"


def test_classify_policy():
    kind, _, cmd = classify_error("no endpoints available matching guardrail restrictions")
    assert kind == "policy"
    assert cmd == "/provider"


def test_classify_other():
    kind, _, cmd = classify_error("some unexpected internal hiccup")
    assert kind == "other"
    assert cmd == ""


def test_classify_handles_empty_or_none():
    assert classify_error("")[0] == "other"
    assert classify_error(None)[0] == "other"


def test_classify_quota_routes_to_rate_limit():
    # quota errors share the rate-limit resolution.
    kind, _, _ = classify_error("monthly quota exceeded")
    assert kind == "rate_limit"


def test_classifier_resolution_is_nonempty_for_known_kinds():
    for err in (
        "cli not found",
        "rate limit",
        "timeout after 30s",
        "api key invalid",
        "no endpoints available",
    ):
        kind, resolution, cmd = classify_error(err)
        assert resolution, f"empty resolution for: {err!r}"
        assert kind != "other"
