"""Shared fixtures for user-perspective end-to-end tests.

These tests drive the harness the way a real user does: by invoking the
``clk`` CLI as a subprocess, by hitting the REST API over HTTP, and by
running ``kickoff.sh``.  Every test gets an isolated working directory
under pytest's ``tmp_path`` so concurrent runs and reruns do not collide.

The shell provider is used by default; it requires no API keys and
always succeeds, so the tests can verify the *plumbing* without needing
network access or paid LLM calls.
"""

from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator, List, Optional

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]


# ---------------------------------------------------------------------------
# Session-wide git config override
# ---------------------------------------------------------------------------
# Some sandbox / CI environments mandate commit signing but lack a working
# signing setup (gpg, sigstore, etc.).  We force-disable signing for every
# subprocess that runs git by pointing GIT_CONFIG_GLOBAL at a temp file we
# control.  This affects ALL tests in this module, including kickoff.sh.
# It is a no-op in environments where signing is already off.

def _ensure_test_gitconfig() -> str:
    cfg_dir = Path("/tmp") / "clk-user-tests-git"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg = cfg_dir / "gitconfig"
    cfg.write_text(
        "[user]\n"
        "  name = CLK User Tests\n"
        "  email = user-tests@clk.invalid\n"
        "[commit]\n"
        "  gpgsign = false\n"
        "[tag]\n"
        "  gpgsign = false\n"
        "[init]\n"
        "  defaultBranch = main\n",
        encoding="utf-8",
    )
    return str(cfg)


_TEST_GITCONFIG = _ensure_test_gitconfig()
os.environ["GIT_CONFIG_GLOBAL"] = _TEST_GITCONFIG


# ---------------------------------------------------------------------------
# Path / subprocess helpers
# ---------------------------------------------------------------------------


def _clk_module_argv() -> List[str]:
    """Argv prefix that invokes the harness as ``python -m clk_harness.cli``."""
    return [sys.executable, "-m", "clk_harness.cli"]


@contextmanager
def chdir(path: Path) -> Iterator[Path]:
    prev = Path.cwd()
    try:
        os.chdir(path)
        yield path
    finally:
        os.chdir(prev)


def run_clk(
    *args: str,
    cwd: Path,
    env: Optional[dict] = None,
    check: bool = False,
    timeout: float = 120.0,
) -> subprocess.CompletedProcess:
    """Run ``python -m clk_harness.cli <args>`` in ``cwd``.

    The harness is invoked as a module rather than via the ``clk`` shell
    script so the same code path works inside any container or venv where
    the package is importable.
    """
    cmd = [*_clk_module_argv(), *args]
    merged_env = os.environ.copy()
    # Disable the background REST API so tests never collide on port 8001.
    merged_env.setdefault("CLK_DISABLE_API", "1")
    if env:
        merged_env.update(env)
    return subprocess.run(
        cmd,
        cwd=str(cwd),
        env=merged_env,
        check=check,
        capture_output=True,
        text=True,
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def clk_project(tmp_path: Path) -> Path:
    """An empty directory ready to be ``clk init``'d.

    The directory contains a fresh git config so the harness can make
    commits without depending on a global user.name / user.email.
    """
    proj = tmp_path / "proj"
    proj.mkdir()
    # Provide a local git identity so commits don't fail in CI / containers
    # that lack a global config.
    subprocess.run(["git", "init", "-q"], cwd=str(proj), check=False)
    subprocess.run(
        ["git", "config", "user.name", "CLK Test"],
        cwd=str(proj), check=False, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "user.email", "test@clk.invalid"],
        cwd=str(proj), check=False, capture_output=True,
    )
    # Some sandboxed CI environments require commit signing but lack a
    # working signing setup; opt out per-repo so commits land.
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=str(proj), check=False, capture_output=True,
    )
    subprocess.run(
        ["git", "config", "tag.gpgsign", "false"],
        cwd=str(proj), check=False, capture_output=True,
    )
    return proj


@pytest.fixture
def initialized_project(clk_project: Path) -> Path:
    """A project that has already been ``clk init``'d."""
    res = run_clk("init", "--name", "ut-project", cwd=clk_project)
    assert res.returncode == 0, f"clk init failed: {res.stderr}"
    return clk_project


@pytest.fixture
def free_port() -> int:
    """Find an OS-assigned free TCP port (closed before returning)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def _wait_for_http(host: str, port: int, *, timeout: float = 30.0) -> bool:
    """Block until ``host:port`` accepts a TCP connection, or timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1.0):
                return True
        except OSError:
            time.sleep(0.1)
    return False


@pytest.fixture
def api_server(tmp_path: Path, free_port: int) -> Iterator[str]:
    """Start ``clk-api`` in a subprocess on a free port.

    Yields the base URL.  The server's workspaces dir is rooted at
    ``tmp_path/workspaces`` so it cannot collide with /workspaces (which
    requires root in many environments).
    """
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()

    env = os.environ.copy()
    env["CLK_WORKSPACES_DIR"] = str(workspaces)
    env["CLK_API_HOST"] = "127.0.0.1"
    env["CLK_API_PORT"] = str(free_port)
    env["CLK_DISABLE_API"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "clk_harness.api"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )

    base_url = f"http://127.0.0.1:{free_port}"
    try:
        if not _wait_for_http("127.0.0.1", free_port, timeout=30.0):
            # Surface server output if it never came up
            try:
                out = proc.stdout.read(8192).decode("utf-8", errors="replace") if proc.stdout else ""
            except Exception:
                out = ""
            proc.terminate()
            pytest.fail(f"clk-api did not start on {base_url}: {out}")
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=5)


@pytest.fixture
def kickoff_sandbox(tmp_path: Path) -> Path:
    """A copy of the kickoff.sh + minimal harness sources in tmp_path.

    Runs kickoff.sh against a writable working copy so the test does not
    pollute the source tree.  Mirrors the layout the user would see when
    they ``git clone`` the repo and run ``./kickoff.sh``.
    """
    sandbox = tmp_path / "repo"
    sandbox.mkdir()
    # Copy just the bits kickoff.sh needs
    for name in ("kickoff.sh", "pyproject.toml", ".env.example"):
        src = REPO_ROOT / name
        if src.exists():
            shutil.copy2(src, sandbox / name)
    for dirname in ("clk_harness", "scripts"):
        src = REPO_ROOT / dirname
        if src.exists():
            shutil.copytree(src, sandbox / dirname)
    # README.md is optional; copy if present.
    readme = REPO_ROOT / "README.md"
    if readme.exists():
        shutil.copy2(readme, sandbox / "README.md")
    # Mark kickoff executable just in case copy stripped the bit.
    (sandbox / "kickoff.sh").chmod(0o755)
    return sandbox
