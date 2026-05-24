"""Confirm kickoff.sh's Telegram-setup wizard integration.

Telegram is configured from the `--setup` wizard rather than from every
kickoff run. We can't drive the full setup flow in a unit test, so we
assert structurally that:
  * the setup wizard asks about Telegram and conditionally invokes the
    standalone telegram_setup_wizard.sh helper,
  * CLK_TELEGRAM_SKIP is persisted to .env based on that answer,
  * pre-existing CLK_TELEGRAM_* values are preserved across re-runs of
    --setup (i.e. referenced from the heredoc).
"""

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
KICKOFF = ROOT / "kickoff.sh"


def test_kickoff_has_telegram_block():
    body = KICKOFF.read_text()
    assert "telegram_setup_wizard.sh" in body
    assert "CLK_TELEGRAM_SKIP" in body
    assert "CLK_TELEGRAM_ENABLED" in body


def test_setup_invokes_wizard_only_on_yes():
    body = KICKOFF.read_text()
    # The wizard is asked about during --setup and invoked only if the
    # user answered y/Y.
    assert 'Set up Telegram bot now?' in body
    assert '"${tg_setup,,}" = "y"' in body
    assert 'CLK_ENV_FILE="$env_file" "$SCRIPT_DIR/scripts/telegram_setup_wizard.sh"' in body


def test_setup_persists_skip_and_existing_values():
    body = KICKOFF.read_text()
    # Declining the prompt writes CLK_TELEGRAM_SKIP=true atomically via
    # env_set (sourced from scripts/lib_env.sh). Pre-existing Telegram
    # values survive a --setup re-run because env_set only touches the
    # specific key it's told about — every other line in .env is
    # preserved verbatim by the awk pass inside lib_env.sh.
    assert 'env_set "$env_file" CLK_TELEGRAM_SKIP "$tg_skip"' in body
    # The wizard sources .env up top so existing values become defaults
    # for any prompt that uses them.
    assert ". \"$env_file\"" in body or '. "$env_file"' in body
    # And the shared env helper is sourced so atomic writes + .bak
    # rotation are in effect.
    assert "scripts/lib_env.sh" in body
