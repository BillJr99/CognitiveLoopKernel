"""Smoke-test the bot's main() entry point: --check-config paths.

The full Telegram Application is not started here; we exercise the
config-validation branches that don't touch the network.
"""

from __future__ import annotations

from clk_harness.integrations.telegram import bot as bot_mod


def test_check_config_missing_token(monkeypatch, capsys):
    monkeypatch.delenv("CLK_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLK_TELEGRAM_ALLOWED_USERS", raising=False)
    rc = bot_mod.main(["--check-config"])
    assert rc == 2


def test_check_config_missing_allowlist(monkeypatch):
    monkeypatch.setenv("CLK_TELEGRAM_BOT_TOKEN", "999:TEST")
    monkeypatch.delenv("CLK_TELEGRAM_ALLOWED_USERS", raising=False)
    rc = bot_mod.main(["--check-config"])
    assert rc == 3


def test_check_config_ok(monkeypatch, capsys):
    monkeypatch.setenv("CLK_TELEGRAM_BOT_TOKEN", "999:TEST")
    monkeypatch.setenv("CLK_TELEGRAM_ALLOWED_USERS", "1,2,3")
    rc = bot_mod.main(["--check-config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "3 user" in out


def test_main_requires_token(monkeypatch, capsys):
    monkeypatch.delenv("CLK_TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("CLK_TELEGRAM_ALLOWED_USERS", raising=False)
    rc = bot_mod.main([])
    assert rc == 2


def test_main_requires_allowlist(monkeypatch):
    monkeypatch.setenv("CLK_TELEGRAM_BOT_TOKEN", "999:TEST")
    monkeypatch.delenv("CLK_TELEGRAM_ALLOWED_USERS", raising=False)
    rc = bot_mod.main([])
    assert rc == 3
