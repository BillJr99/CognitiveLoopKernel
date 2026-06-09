"""End-to-end tests for the web-UI REST surface.

Starts the real ``clk-api`` server in a subprocess (with an isolated
workspaces dir AND an isolated ``CLK_ENV_FILE`` so the test never touches
the developer's real ``.env``), then drives the new web endpoints over
HTTP the way the React SPA does.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator

import httpx
import pytest


def _wait_for_http(port: int, timeout: float = 30.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            httpx.get(f"http://127.0.0.1:{port}/api/healthz", timeout=2)
            return True
        except Exception:
            time.sleep(0.25)
    return False


@pytest.fixture
def webui_server(tmp_path: Path, free_port: int) -> Iterator[str]:
    """A clk-api server with isolated workspaces + .env for web-UI tests."""
    workspaces = tmp_path / "workspaces"
    workspaces.mkdir()
    env_file = tmp_path / ".env"

    env = os.environ.copy()
    env["CLK_WORKSPACES_DIR"] = str(workspaces)
    env["CLK_ENV_FILE"] = str(env_file)
    env["CLK_API_HOST"] = "127.0.0.1"
    env["CLK_API_PORT"] = str(free_port)
    env["CLK_DISABLE_API"] = "1"

    proc = subprocess.Popen(
        [sys.executable, "-m", "clk_harness.api"],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    base = f"http://127.0.0.1:{free_port}"
    try:
        if not _wait_for_http(free_port):
            out = proc.stdout.read(8192).decode(errors="replace") if proc.stdout else ""
            proc.terminate()
            pytest.fail(f"clk-api did not start: {out}")
        yield base
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except Exception:
            proc.kill()


def _new_workspace(base: str, name: str = "e2e") -> dict:
    r = httpx.post(f"{base}/api/workspaces", json={"name": name}, timeout=10)
    assert r.status_code == 201, r.text
    return r.json()


def _run_and_wait(base: str, wid: str, command: str, timeout: float = 90.0) -> dict:
    r = httpx.post(f"{base}/api/research", json={"command": command, "workspace_id": wid}, timeout=10)
    assert r.status_code == 202, r.text
    task_id = r.json()["task_id"]
    deadline = time.time() + timeout
    while time.time() < deadline:
        s = httpx.get(f"{base}/api/research/{task_id}", timeout=10).json()
        if s["status"] in ("done", "failed", "cancelled"):
            return s
        time.sleep(0.5)
    pytest.fail(f"task {task_id} ({command}) did not finish")


def test_env_endpoints_masked_and_schema(webui_server: str) -> None:
    base = webui_server
    # Schema is always available.
    schema = httpx.get(f"{base}/api/env/schema", timeout=10).json()
    assert schema["ok"] is True
    assert any(g["name"] == "API Keys" for g in schema["groups"])

    # Write a secret + a plain var, confirm masking on read-back.
    r = httpx.put(
        f"{base}/api/env",
        json={"values": {"ANTHROPIC_API_KEY": "sk-e2e-secret", "CLK_PROVIDER": "shell"}},
        timeout=10,
    )
    assert r.status_code == 200
    body = httpx.get(f"{base}/api/env", timeout=10).json()
    by_key = {v["key"]: v for v in body["vars"]}
    assert by_key["ANTHROPIC_API_KEY"]["masked"] is True
    assert by_key["ANTHROPIC_API_KEY"]["value"] != "sk-e2e-secret"
    assert by_key["CLK_PROVIDER"]["value"] == "shell"

    # Sentinel preserves the secret while changing a sibling value.
    mask = by_key["ANTHROPIC_API_KEY"]["value"]
    httpx.put(
        f"{base}/api/env",
        json={"values": {"ANTHROPIC_API_KEY": mask, "CLK_PROVIDER": "ollama"}},
        timeout=10,
    )
    # Reveal (enabled? no) -> stays 403; instead confirm via a second GET that
    # the provider changed and the key remains masked (still set).
    body2 = httpx.get(f"{base}/api/env", timeout=10).json()
    by_key2 = {v["key"]: v for v in body2["vars"]}
    assert by_key2["CLK_PROVIDER"]["value"] == "ollama"
    assert by_key2["ANTHROPIC_API_KEY"]["set"] is True
    assert by_key2["ANTHROPIC_API_KEY"]["masked"] is True


def test_config_endpoints(webui_server: str) -> None:
    base = webui_server
    ws = _new_workspace(base, "cfg")
    wid = ws["workspace_id"]

    cfg = httpx.get(f"{base}/api/workspaces/{wid}/config/clk", timeout=10).json()
    assert cfg["ok"] and "default_provider" in cfg["config"]
    cfg["config"]["max_iterations"] = 9
    r = httpx.put(f"{base}/api/workspaces/{wid}/config/clk", json={"config": cfg["config"]}, timeout=10)
    assert r.json()["config"]["max_iterations"] == 9

    prov = httpx.get(f"{base}/api/workspaces/{wid}/config/providers", timeout=10).json()
    assert "providers" in prov and "shell" in prov["providers"]

    doctor = httpx.get(f"{base}/api/workspaces/{wid}/doctor", timeout=10).json()
    assert doctor["ok"] and "findings" in doctor


def test_init_then_activity_and_snapshot(webui_server: str) -> None:
    base = webui_server
    ws = _new_workspace(base, "act")
    wid = ws["workspace_id"]

    status = _run_and_wait(base, wid, "init")
    assert status["status"] == "done", status

    # The init run writes default_agent_created events to activity.jsonl.
    activity = httpx.get(f"{base}/api/workspaces/{wid}/activity", timeout=10).json()
    assert activity["ok"] is True
    kinds = {e["kind"] for e in activity["events"]}
    assert "default_agent_created" in kinds, kinds

    # Each normalized event carries the UI fields the SPA renders.
    sample = next(e for e in activity["events"] if e["kind"] == "default_agent_created")
    for field in ("seq", "ts", "kind", "severity", "category", "summary", "payload"):
        assert field in sample

    snap = httpx.get(f"{base}/api/workspaces/{wid}/snapshot", timeout=10).json()["snapshot"]
    assert "agents" in snap and "totals" in snap
    # chief/qa/ralph baseline roles get cards from default_agent_created.
    assert any(name in snap["agents"] for name in ("chief", "qa", "ralph"))


def test_activity_stream_replays_then_closes(webui_server: str) -> None:
    base = webui_server
    ws = _new_workspace(base, "stream")
    wid = ws["workspace_id"]
    _run_and_wait(base, wid, "init")

    # from=start replays the existing log; read a few frames then disconnect.
    got = []
    with httpx.stream(
        "GET", f"{base}/api/workspaces/{wid}/activity/stream?from=start", timeout=15
    ) as resp:
        assert resp.status_code == 200
        start = time.time()
        for line in resp.iter_lines():
            if line.startswith("data:"):
                got.append(json.loads(line[len("data:"):].strip()))
                if len(got) >= 3 or time.time() - start > 8:
                    break
    assert got, "expected at least one streamed activity event"
    assert all("kind" in e for e in got)


def test_spa_served(webui_server: str) -> None:
    base = webui_server
    r = httpx.get(base + "/", timeout=10)
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")
