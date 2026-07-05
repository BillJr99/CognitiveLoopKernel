"""Drives scripts/telegram_setup_wizard.sh against a stubbed Telegram API.

We start a tiny in-process HTTP server that mimics api.telegram.org's
/getMe and /getUpdates endpoints, then run the wizard with curl / urllib
pointed at it (by overriding the URL via a wrapper script). Since
hard-coding the host is awkward, we instead set PATH to include a stub
"curl" that serves canned responses.
"""

from __future__ import annotations

import os
import stat
import subprocess
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[3]
WIZARD = PROJECT_ROOT / "scripts" / "telegram_setup_wizard.sh"


@pytest.fixture
def fake_curl(tmp_path: Path) -> Path:
    """PATH dir with a stub `curl` that returns canned Telegram API JSON."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    stub = bin_dir / "curl"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "# Strip flags, grab the URL (the last argument).\n"
        "url=\"${@: -1}\"\n"
        'case "$url" in\n'
        "  *getMe*) echo '{\"ok\":true,\"result\":{\"id\":1,\"username\":\"clk_test_bot\"}}';;\n"
        "  *getUpdates*) echo '{\"ok\":true,\"result\":[{\"update_id\":1,"
        "\"message\":{\"from\":{\"id\":42,\"username\":\"alice\"},\"text\":\"hi\"}}]}';;\n"
        "  *) echo '{}';;\n"
        "esac\n"
    )
    st = stub.stat()
    stub.chmod(st.st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return bin_dir


def test_wizard_writes_env(tmp_path: Path, fake_curl: Path):
    if not WIZARD.exists():
        pytest.skip("wizard script not present")
    env_file = tmp_path / ".env"
    env_file.write_text("")

    env = os.environ.copy()
    env["PATH"] = f"{fake_curl}:{env['PATH']}"
    env["CLK_ENV_FILE"] = str(env_file)
    env["CLK_TELEGRAM_BOT_TOKEN"] = "999:TESTTOKEN"  # pre-seed so the
    # 'Use existing token' branch fires; the stubbed curl validates it.
    env["CLK_TELEGRAM_SETUP_NONINTERACTIVE"] = "1"
    env["CLK_TELEGRAM_NO_TTY"] = "1"
    # Inputs:
    # 1. "Use existing token (masked)? [Y/n]:" -> Y
    # 2. After getUpdates, manual prompt only shown if no IDs found; our
    #    stub returns one, so no further input is needed.
    stdin = "Y\n"

    proc = subprocess.run(
        ["bash", str(WIZARD)],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, f"stderr={proc.stderr}\nstdout={proc.stdout}"

    body = env_file.read_text()
    assert "CLK_TELEGRAM_BOT_TOKEN=999:TESTTOKEN" in body
    assert "CLK_TELEGRAM_ENABLED=true" in body
    assert "CLK_TELEGRAM_ALLOWED_USERS=" in body
    # Captured user ID from stub getUpdates
    assert "42" in body


def test_wizard_idempotent_append(tmp_path: Path, fake_curl: Path):
    if not WIZARD.exists():
        pytest.skip("wizard script not present")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "EXISTING=keepme\nCLK_TELEGRAM_ALLOWED_USERS=7\n"
    )

    env = os.environ.copy()
    env["PATH"] = f"{fake_curl}:{env['PATH']}"
    env["CLK_ENV_FILE"] = str(env_file)
    env["CLK_TELEGRAM_BOT_TOKEN"] = "999:TESTTOKEN"
    env["CLK_TELEGRAM_SETUP_NONINTERACTIVE"] = "1"
    env["CLK_TELEGRAM_NO_TTY"] = "1"

    proc = subprocess.run(
        ["bash", str(WIZARD)],
        input="Y\n",
        capture_output=True,
        text=True,
        env=env,
        timeout=20,
    )
    assert proc.returncode == 0, proc.stderr

    body = env_file.read_text()
    # Unrelated keys preserved
    assert "EXISTING=keepme" in body
    # Allowlist now contains both 7 (existing) and 42 (newly captured)
    assert "CLK_TELEGRAM_ALLOWED_USERS=7,42" in body
