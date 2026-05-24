"""Tests for the TUI hint bar — TuiApp._hint_for_state.

We test the pure-state→string mapping without instantiating curses.
The user feels "in control" only if the hint bar surfaces the right
next-step on every meaningful state, so these checks lock in that
contract.
"""

from __future__ import annotations

import pytest

from clk_harness.config import Paths
from clk_harness.tui import DashboardState, TuiApp


@pytest.fixture
def app(tmp_path):
    paths = Paths(root=tmp_path)
    paths.ensure()
    state = DashboardState(["chief"], paths=paths, agents_cfg={"agents": {"chief": {}}})
    state.provider = "claude"
    # We don't need a real Worker for the hint bar — it only reads state.
    return TuiApp(state, worker=None)  # type: ignore[arg-type]


def test_hint_when_no_idea_yet(app):
    app.state.idea = ""
    hint = app._hint_for_state()
    assert "type your idea" in hint
    assert "/help" in hint or "/tutorial" in hint


def test_hint_when_busy(app):
    app.state.set_idea("test")
    app.state.set_phase("workflow:engineering", busy=True)
    hint = app._hint_for_state()
    assert "working" in hint or "agent" in hint
    assert "/abort" in hint


def test_hint_when_stop_requested(app):
    app.state.set_idea("test")
    app.state.set_phase("loop:ralph", busy=False)
    app.state.request_stop()
    hint = app._hint_for_state()
    assert "stop" in hint.lower()


def test_hint_when_provider_not_installed(app):
    app.state.set_idea("test")
    app.state.last_error_kind = "not_installed"
    hint = app._hint_for_state()
    assert "/install" in hint
    assert app.state.provider in hint


def test_hint_when_provider_auth_failed(app):
    app.state.set_idea("test")
    app.state.last_error_kind = "auth"
    hint = app._hint_for_state()
    assert "/configure" in hint


def test_hint_when_provider_rate_limited(app):
    app.state.set_idea("test")
    app.state.last_error_kind = "rate_limit"
    hint = app._hint_for_state()
    assert "rate" in hint.lower() or "limited" in hint.lower()


def test_hint_when_provider_timeout(app):
    app.state.set_idea("test")
    app.state.last_error_kind = "timeout"
    hint = app._hint_for_state()
    assert "/abort" in hint or "stalled" in hint.lower()


def test_hint_when_tutorial_running(app):
    app.state.set_idea("test")
    app.state.in_tutorial = True
    hint = app._hint_for_state()
    assert "tutorial" in hint.lower()


def test_hint_idle_with_idea_suggests_help(app):
    app.state.set_idea("test")
    hint = app._hint_for_state()
    assert "/help" in hint or "follow-up" in hint or "continue" in hint
