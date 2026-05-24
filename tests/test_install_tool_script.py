"""Structural tests for scripts/install_tool.sh and scripts/lib_env.sh.

These don't exercise the install commands themselves (which would
require network and a writable global npm/apt) — they verify the
public surface, sourceability, and the deterministic helper functions
that the wizard and TUI depend on.
"""

import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LIB_ENV = ROOT / "scripts" / "lib_env.sh"
INSTALL_TOOL = ROOT / "scripts" / "install_tool.sh"


def _sh(snippet: str, env_extra: dict | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["BASH_ENV"] = ""
    if env_extra:
        env.update(env_extra)
    return subprocess.run(
        ["bash", "-c", snippet],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )


def test_lib_env_sourceable():
    r = _sh(f". '{LIB_ENV}' && echo ok")
    assert r.returncode == 0, r.stderr
    assert "ok" in r.stdout


def test_lib_env_set_inserts_and_rotates(tmp_path):
    env_file = tmp_path / ".env"
    r = _sh(f". '{LIB_ENV}' && env_set '{env_file}' FOO bar && env_set '{env_file}' BAR baz")
    assert r.returncode == 0, r.stderr
    body = env_file.read_text(encoding="utf-8")
    assert "FOO=bar" in body
    assert "BAR=baz" in body
    # Second write should have rotated a .bak (containing only FOO=bar).
    bak = tmp_path / ".env.bak"
    assert bak.exists()
    assert "FOO=bar" in bak.read_text(encoding="utf-8")


def test_lib_env_set_replaces_existing_key(tmp_path):
    env_file = tmp_path / ".env"
    r = _sh(f". '{LIB_ENV}' && env_set '{env_file}' FOO one && env_set '{env_file}' FOO two")
    assert r.returncode == 0, r.stderr
    body = env_file.read_text(encoding="utf-8")
    assert "FOO=two" in body
    assert "FOO=one" not in body


def test_lib_env_get_returns_default_when_missing(tmp_path):
    env_file = tmp_path / ".env"
    r = _sh(f". '{LIB_ENV}' && env_get '{env_file}' MISSING fallback")
    assert r.returncode == 0
    assert r.stdout.strip() == "fallback"


def test_lib_env_restore_swaps_bak_back(tmp_path):
    env_file = tmp_path / ".env"
    r = _sh(
        f". '{LIB_ENV}' && env_set '{env_file}' FOO one "
        f"&& env_set '{env_file}' FOO two && env_restore '{env_file}'"
    )
    assert r.returncode == 0, r.stderr
    assert "FOO=one" in env_file.read_text(encoding="utf-8")


def test_install_tool_script_executable_and_lints():
    # `bash -n` parses without executing.
    r = subprocess.run(
        ["bash", "-n", str(INSTALL_TOOL)],
        capture_output=True, text=True, check=False,
    )
    assert r.returncode == 0, r.stderr


def test_install_tool_check_handles_known_tool(tmp_path):
    # `shell` is always available — check_tool should return 0.
    r = _sh(f"{INSTALL_TOOL} check shell")
    assert r.returncode == 0


def test_install_tool_check_handles_missing_tool():
    # An obviously fake tool should report missing (rc != 0).
    r = _sh(f"{INSTALL_TOOL} check totally-not-a-real-tool-xyz")
    assert r.returncode != 0


def test_install_tool_install_print_only_prints_command(tmp_path):
    # --print-only never executes — just confirms the recipe surfaces a
    # candidate command without prompting.
    fake_tool_marker = tmp_path / "fake_marker"
    # We use the `tmux` recipe because it's deterministic across platforms
    # and the script's --print-only branch returns rc=1 (user-declined).
    r = subprocess.run(
        ["bash", str(INSTALL_TOOL), "install", "tmux", "--print-only"],
        capture_output=True, text=True, check=False,
    )
    # Should mention either install command or the docs link.
    combined = r.stdout + r.stderr
    assert ("tmux" in combined) or ("apt" in combined) or ("brew" in combined) or ("github" in combined.lower())


def test_install_tool_configure_rejects_unknown_tool():
    r = subprocess.run(
        ["bash", str(INSTALL_TOOL), "configure", "totally-fake-tool"],
        capture_output=True, text=True, check=False,
    )
    # Should return a non-zero "no configure recipe" code without prompting.
    assert r.returncode != 0
