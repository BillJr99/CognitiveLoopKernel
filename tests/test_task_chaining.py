"""Tests for server-side task chaining (ResearchRequest.then_run).

Guided mode regression: the cast -> build hop used to live in the browser,
so navigating away mid-cast meant the build never started. The server now
spawns the follow-up `run` task itself when the first task finishes.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

import clk_harness.api as api_module
from clk_harness.api import app

_TS = "2026-01-01T00:00:00Z"


class _FakeStdout:
    async def readline(self) -> bytes:
        return b""


class _FakeProc:
    def __init__(self, returncode: int = 0) -> None:
        self.returncode = returncode
        self.stdout = _FakeStdout()

    async def wait(self) -> int:
        return self.returncode

    async def communicate(self):
        return b"", b""

    def terminate(self) -> None:
        pass


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield
    for k in list(api_module.TASKS):
        task = api_module.TASKS[k]
        if str(task.get("workspace_id", "")).startswith("_chain_test_"):
            handle = api_module._task_handles.pop(k, None)
            if handle and not handle.done():
                handle.cancel()
            del api_module.TASKS[k]
    for k in list(api_module.WORKSPACES):
        if k.startswith("_chain_test_"):
            del api_module.WORKSPACES[k]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


def _fake_subprocess(monkeypatch: pytest.MonkeyPatch, returncode: int = 0) -> None:
    async def fake_exec(*args, **kwargs):
        return _FakeProc(returncode)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    # Skip the auto-init path so only one subprocess runs per task.
    from clk_harness import config as config_module
    monkeypatch.setattr(config_module, "is_initialized", lambda paths: True)


async def _wait_for(predicate, timeout_s: float = 5.0) -> None:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout_s
    while not predicate():
        if loop.time() > deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.02)


@pytest.mark.asyncio
async def test_then_run_spawns_chained_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_id = "_chain_test_ok"
    api_module.WORKSPACES[ws_id] = {
        "id": ws_id, "name": "chain", "path": str(tmp_path), "created_at": _TS,
    }
    _fake_subprocess(monkeypatch, returncode=0)

    r = await client.post("/api/research", json={
        "command": "idea", "args": ["build a thing"],
        "workspace_id": ws_id, "then_run": "engineering",
    })
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    await _wait_for(lambda: api_module.TASKS[task_id].get("chained_task_id"))
    chained_id = api_module.TASKS[task_id]["chained_task_id"]
    chained = api_module.TASKS[chained_id]
    assert chained["workspace_id"] == ws_id
    assert chained["command"] == "run"
    assert chained["args"] == ["--workflow", "engineering"]

    # The status endpoint must expose the chained id so the UI can follow it.
    s = await client.get(f"/api/research/{task_id}")
    assert s.json()["chained_task_id"] == chained_id


@pytest.mark.asyncio
async def test_then_run_skipped_on_failure(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_id = "_chain_test_fail"
    api_module.WORKSPACES[ws_id] = {
        "id": ws_id, "name": "chain-fail", "path": str(tmp_path), "created_at": _TS,
    }
    _fake_subprocess(monkeypatch, returncode=1)

    r = await client.post("/api/research", json={
        "command": "idea", "args": ["broken"],
        "workspace_id": ws_id, "then_run": "engineering",
    })
    task_id = r.json()["task_id"]

    await _wait_for(lambda: api_module.TASKS[task_id]["status"] == "failed")
    assert api_module.TASKS[task_id].get("chained_task_id") is None


@pytest.mark.asyncio
async def test_no_then_run_no_chain(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_id = "_chain_test_none"
    api_module.WORKSPACES[ws_id] = {
        "id": ws_id, "name": "no-chain", "path": str(tmp_path), "created_at": _TS,
    }
    _fake_subprocess(monkeypatch, returncode=0)

    r = await client.post("/api/research", json={
        "command": "idea", "args": ["simple"], "workspace_id": ws_id,
    })
    task_id = r.json()["task_id"]

    await _wait_for(lambda: api_module.TASKS[task_id]["status"] == "done")
    assert api_module.TASKS[task_id].get("chained_task_id") is None
