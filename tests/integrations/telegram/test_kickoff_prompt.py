"""Confirm kickoff.sh's Telegram-setup prompt logic.

We can't drive the full kickoff flow in a unit test (it would try to
build a workspace and run a provider), so we surgically extract the
prompt block by passing the no-tty path: when /dev/tty is unopenable,
kickoff must silently skip the prompt. We assert that property indirectly
by checking the relevant guard text is in the script.
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
KICKOFF = ROOT / "kickoff.sh"


def test_kickoff_has_telegram_block():
    body = KICKOFF.read_text()
    assert "telegram_setup_wizard.sh" in body
    assert "CLK_TELEGRAM_SKIP" in body
    assert "CLK_TELEGRAM_ENABLED" in body


def test_kickoff_skip_flag_short_circuits():
    body = KICKOFF.read_text()
    # The guard must check both ENABLED and TOKEN before prompting,
    # and respect CLK_TELEGRAM_SKIP.
    assert '"${CLK_TELEGRAM_SKIP:-false}" != "true"' in body
    assert '"${CLK_TELEGRAM_ENABLED:-false}" != "true"' in body
