"""End-to-end tests for the Docker image.

These verify the published container actually works as a user would
consume it from GHCR: the image builds from the repo's ``Dockerfile``,
the entry point responds to ``--help``, and a non-interactive kickoff
run with the shell provider produces the expected workspace artefacts.

Skipped automatically when the Docker daemon is unavailable so the rest
of the user_tests suite still runs on hosts without Docker.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
IMAGE_TAG = os.environ.get("CLK_DOCKER_TEST_IMAGE", "clk-e2e-test:latest")
BUILD_TIMEOUT = float(os.environ.get("CLK_DOCKER_BUILD_TIMEOUT", "900"))
RUN_TIMEOUT = float(os.environ.get("CLK_DOCKER_RUN_TIMEOUT", "600"))


def _docker_available() -> bool:
    if shutil.which("docker") is None:
        return False
    try:
        r = subprocess.run(
            ["docker", "info"],
            capture_output=True, text=True, timeout=15,
        )
        return r.returncode == 0
    except (subprocess.TimeoutExpired, OSError):
        return False


pytestmark = pytest.mark.skipif(
    not _docker_available(),
    reason="Docker daemon is not available on this host",
)


@pytest.fixture(scope="module")
def docker_image() -> str:
    """Build the image from the repo's Dockerfile once per test module."""
    res = subprocess.run(
        ["docker", "build", "-t", IMAGE_TAG, "."],
        cwd=str(REPO_ROOT),
        capture_output=True,
        text=True,
        timeout=BUILD_TIMEOUT,
    )
    if res.returncode != 0:
        pytest.fail(
            "docker build failed:\n"
            f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
        )
    return IMAGE_TAG


def _docker_run(image: str, *args: str, timeout: float = RUN_TIMEOUT,
                extra_docker_args: list[str] | None = None
                ) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm"]
    if extra_docker_args:
        cmd.extend(extra_docker_args)
    cmd.append(image)
    cmd.extend(args)
    return subprocess.run(
        cmd, capture_output=True, text=True, timeout=timeout,
    )


def test_image_builds_and_entrypoint_help(docker_image: str) -> None:
    """The entrypoint (kickoff.sh) responds to --help inside the image."""
    res = _docker_run(docker_image, "--help", timeout=60)
    assert res.returncode == 0, (
        f"kickoff.sh --help failed inside container.\n"
        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    combined = (res.stdout + res.stderr).lower()
    assert "usage" in combined
    assert "--provider" in combined


def test_clk_cli_help_inside_container(docker_image: str) -> None:
    """The bundled clk_harness package is importable & the CLI runs."""
    res = _docker_run(
        docker_image,
        "python", "-m", "clk_harness.cli", "--help",
        timeout=60,
        extra_docker_args=["--entrypoint", ""],
    )
    assert res.returncode == 0, (
        f"clk --help failed inside container.\n"
        f"STDOUT:\n{res.stdout}\nSTDERR:\n{res.stderr}"
    )
    assert "usage" in (res.stdout + res.stderr).lower()


def test_kickoff_non_interactive_run(
    docker_image: str, tmp_path: Path,
) -> None:
    """End-to-end: a CLK_NO_TUI kickoff with the shell provider produces
    a workspace directory with the documented artefacts.
    """
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    # The image's default user is root; ensure the bind-mounted host dir
    # is writable from inside the container.
    workspace.chmod(0o777)

    project_name = f"ut-docker-{uuid.uuid4().hex[:8]}"
    res = _docker_run(
        docker_image,
        "An idea for a small CLI tool",
        timeout=RUN_TIMEOUT,
        extra_docker_args=[
            "-v", f"{workspace}:/app/workspace",
            "-e", "CLK_NO_TUI=true",
            "-e", "CLK_PROVIDER=shell",
            "-e", f"CLK_PROJECT_NAME={project_name}",
            "-e", "CLK_MAX_ITERATIONS=1",
            "-e", "CLK_GIT_NAME=CLK Docker Test",
            "-e", "CLK_GIT_EMAIL=docker-test@clk.invalid",
        ],
    )
    assert res.returncode == 0, (
        f"non-interactive kickoff failed.\n"
        f"STDOUT:\n{res.stdout[-4000:]}\nSTDERR:\n{res.stderr[-4000:]}"
    )

    # kickoff.sh writes a self-contained kickoff-* directory under
    # workspace/ containing the .clk state tree and a KICKOFF.md.
    kickoffs = sorted(workspace.glob("kickoff-*"))
    assert kickoffs, (
        f"no kickoff-* dir created under {workspace}; "
        f"contents: {[p.name for p in workspace.iterdir()]}"
    )
    kdir = kickoffs[-1]
    assert (kdir / "KICKOFF.md").is_file()
    assert (kdir / ".clk").is_dir()
