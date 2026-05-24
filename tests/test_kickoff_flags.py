"""Tests for kickoff.sh new flags: --list, --clean, --restore.

These don't run the full kickoff (which copies the harness and spawns
the TUI). They exercise just the early-exit flag handlers so we can
verify their contracts without doing a real run.
"""

import os
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
KICKOFF = ROOT / "kickoff.sh"


def _kickoff(*args, cwd: Path, stdin: str | None = None) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    # Force a known HOME so the test never reads the user's real config.
    env["HOME"] = str(cwd)
    return subprocess.run(
        ["bash", str(KICKOFF), *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        input=stdin,
        check=False,
    )


def test_help_exits_zero(tmp_path: Path) -> None:
    proc = _kickoff("--help", cwd=tmp_path)
    assert proc.returncode == 0
    # Help mentions every new flag we added.
    for flag in ("--setup", "--restore", "--list", "--clean"):
        assert flag in proc.stdout


def test_list_prints_helpful_message_when_no_workspace(tmp_path: Path) -> None:
    proc = _kickoff("--list", cwd=tmp_path)
    assert proc.returncode == 0
    assert "no workspace" in proc.stdout.lower()


def test_list_shows_existing_kickoff_dirs(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "kickoff-20260101-120000").mkdir(parents=True)
    (tmp_path / "workspace" / "kickoff-20260201-120000").mkdir(parents=True)
    proc = _kickoff("--list", cwd=tmp_path)
    assert proc.returncode == 0
    assert "kickoff-20260101" in proc.stdout
    assert "kickoff-20260201" in proc.stdout


def test_restore_swaps_env_bak_back(tmp_path: Path) -> None:
    # Working copy of kickoff.sh into the temp dir so .env.bak lives
    # next to it. The script resolves SCRIPT_DIR from BASH_SOURCE so
    # we point bash at the original via path, but the lookup for .env
    # uses SCRIPT_DIR (= the original) — so write .env.bak next to the
    # real kickoff.sh instead.
    env_path = ROOT / ".env"
    env_bak_path = ROOT / ".env.bak"
    # Save any pre-existing files so we restore them at the end.
    env_was = env_path.read_text(encoding="utf-8") if env_path.exists() else None
    env_bak_was = env_bak_path.read_text(encoding="utf-8") if env_bak_path.exists() else None
    try:
        env_bak_path.write_text("KEY=from-bak\n", encoding="utf-8")
        if env_path.exists():
            env_path.unlink()
        proc = _kickoff("--restore", cwd=tmp_path)
        assert proc.returncode == 0, proc.stderr
        # .env should now contain the backup body.
        assert env_path.read_text(encoding="utf-8") == "KEY=from-bak\n"
    finally:
        if env_path.exists():
            env_path.unlink()
        if env_bak_path.exists():
            env_bak_path.unlink()
        if env_was is not None:
            env_path.write_text(env_was, encoding="utf-8")
        if env_bak_was is not None:
            env_bak_path.write_text(env_bak_was, encoding="utf-8")


def test_restore_without_bak_fails_cleanly(tmp_path: Path) -> None:
    env_bak_path = ROOT / ".env.bak"
    if env_bak_path.exists():
        # Don't pollute another developer's checkout.
        pytest.skip(".env.bak exists locally — skip negative test")
    proc = _kickoff("--restore", cwd=tmp_path)
    assert proc.returncode != 0
    assert "no" in proc.stderr.lower() or "no" in proc.stdout.lower()


def test_clean_requires_an_interval(tmp_path: Path) -> None:
    # --clean with no value is a usage error.
    proc = _kickoff("--clean", cwd=tmp_path)
    assert proc.returncode == 2
    assert "requires a value" in proc.stderr or "DURATION" in proc.stderr


def test_clean_rejects_unparseable_durations(tmp_path: Path) -> None:
    (tmp_path / "workspace" / "kickoff-x").mkdir(parents=True)
    proc = _kickoff("--clean", "garbage", cwd=tmp_path)
    assert proc.returncode == 2


def test_clean_dry_message_when_nothing_old_enough(tmp_path: Path) -> None:
    # A fresh kickoff dir won't be "older than 7d".
    (tmp_path / "workspace" / "kickoff-fresh").mkdir(parents=True)
    proc = _kickoff("--clean", "7d", cwd=tmp_path)
    assert proc.returncode == 0
    assert "no kickoff dirs older" in proc.stdout.lower()


def test_clean_with_no_workspace_dir(tmp_path: Path) -> None:
    proc = _kickoff("--clean", "7d", cwd=tmp_path)
    assert proc.returncode == 0
    assert "nothing to clean" in proc.stdout.lower()


def test_clean_lists_targets_and_refuses_without_tty(tmp_path: Path) -> None:
    # Create a workspace and backdate it to be eligible.
    d = tmp_path / "workspace" / "kickoff-stale"
    d.mkdir(parents=True)
    import time as _time
    old = _time.time() - (8 * 86400)
    os.utime(d, (old, old))
    proc = _kickoff("--clean", "7d", cwd=tmp_path)
    # No /dev/tty inside subprocess.run → script should refuse to delete.
    assert proc.returncode == 2
    assert "non-interactive" in (proc.stderr + proc.stdout).lower()
    # And the dir is still there.
    assert d.exists()


def test_unknown_flag_exits_nonzero(tmp_path: Path) -> None:
    proc = _kickoff("--definitely-not-a-flag", cwd=tmp_path)
    assert proc.returncode != 0
