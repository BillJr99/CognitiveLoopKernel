"""Confirm the kickoff setup wizard's Telegram integration.

Telegram is configured from the `--setup` wizard rather than from every
kickoff run. The wizard lives in ``clk_harness/kickoff.py`` (kickoff.sh
is a thin wrapper over `clk kickoff`). We can't drive the full setup
flow in a unit test, so we assert structurally that:
  * the setup wizard asks about Telegram and conditionally invokes the
    standalone telegram_setup_wizard.sh helper,
  * CLK_TELEGRAM_SKIP is persisted to .env based on that answer,
  * pre-existing CLK_TELEGRAM_* values are preserved across re-runs of
    --setup (answers are written through env_file.write_env, which only
    touches the specific key it's told about).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
KICKOFF = ROOT / "clk_harness" / "kickoff.py"


def test_kickoff_has_telegram_block():
    body = KICKOFF.read_text()
    assert "telegram_setup_wizard.sh" in body
    assert "CLK_TELEGRAM_SKIP" in body
    assert "CLK_TELEGRAM_ENABLED" in body


def test_setup_invokes_wizard_only_on_yes():
    body = KICKOFF.read_text()
    # The wizard is asked about during --setup and invoked only if the
    # user answered y/Y.
    assert "Set up Telegram bot now?" in body
    assert 'tg_setup.lower() == "y"' in body
    # The standalone helper is launched with CLK_ENV_FILE pointing at the
    # wizard's env file so both write the same .env.
    assert '"CLK_ENV_FILE": str(env_path)' in body


def test_setup_persists_skip_and_existing_values():
    body = KICKOFF.read_text()
    # Declining the prompt writes CLK_TELEGRAM_SKIP=true atomically via
    # _env_set -> env_file.write_env (tmp + fsync + .bak + os.replace).
    # Pre-existing Telegram values survive a --setup re-run because
    # write_env only touches the specific key it's told about — every
    # other line in .env is preserved verbatim.
    assert '_env_set("CLK_TELEGRAM_SKIP", "true")' in body
    assert '_env_set("CLK_TELEGRAM_SKIP", "false")' in body
    assert "env_file.write_env" in body
    # The wizard loads .env up top so existing values become defaults
    # for any prompt that uses them.
    assert "_load_env_into(os.environ, env_path)" in body
