"""Tests for POST /api/workspaces/{workspace_id}/nudge.

Covers the two key behaviours:
  (1) action:none when there is no pending/running task for the workspace.
  (2) action:restarted — a new task_id is returned, the previous task is
      marked cancelled, and a new task is registered in TASKS.
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


def _add_workspace(ws_id: str, path: Path) -> None:
    api_module.WORKSPACES[ws_id] = {
        "id": ws_id, "name": "test-ws", "path": str(path),
        "created_at": _TS,
    }


def _add_task(ws_id: str, task_id: str, status: str = "running") -> None:
    api_module.TASKS[task_id] = {
        "task_id": task_id, "workspace_id": ws_id,
        "command": "run", "args": ["--workflow", "engineering"],
        "status": status, "created_at": _TS, "started_at": _TS,
        "finished_at": None, "exit_code": None, "lines": [], "proc": None,
    }


@pytest.fixture(autouse=True)
def _clean() -> None:
    yield
    # Remove any workspace/task seeded by this test. Match tasks by their own
    # workspace_id prefix too, so cleanup works even if the workspace entry
    # was already removed (and catches server-chained tasks with UUID ids).
    for k in list(api_module.TASKS):
        task = api_module.TASKS[k]
        if k.startswith("_nudge_test_") or str(task.get("workspace_id", "")).startswith("_nudge_test_"):
            handle = api_module._task_handles.pop(k, None)
            if handle and not handle.done():
                handle.cancel()
            del api_module.TASKS[k]
    for k in list(api_module.WORKSPACES):
        if k.startswith("_nudge_test_"):
            del api_module.WORKSPACES[k]


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.mark.asyncio
async def test_nudge_unknown_workspace(client: AsyncClient) -> None:
    r = await client.post("/api/workspaces/does-not-exist/nudge")
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_nudge_no_running_tasks(client: AsyncClient, tmp_path: Path) -> None:
    ws_id = "_nudge_test_none"
    _add_workspace(ws_id, tmp_path)
    r = await client.post(f"/api/workspaces/{ws_id}/nudge")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "none"
    assert body["reason"] == "no_running_task"


@pytest.mark.asyncio
async def test_nudge_no_running_tasks_after_done(client: AsyncClient, tmp_path: Path) -> None:
    ws_id = "_nudge_test_done"
    old_id = "_nudge_test_task_done"
    _add_workspace(ws_id, tmp_path)
    _add_task(ws_id, old_id, status="done")   # finished task — not cancellable
    r = await client.post(f"/api/workspaces/{ws_id}/nudge")
    assert r.status_code == 200
    assert r.json()["action"] == "none"


@pytest.mark.asyncio
async def test_nudge_restarts_running_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_id = "_nudge_test_restart"
    old_id = "_nudge_test_task_old"
    _add_workspace(ws_id, tmp_path)
    _add_task(ws_id, old_id, status="running")

    async def _noop(task_id: str) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(api_module, "_run_task", _noop)

    r = await client.post(f"/api/workspaces/{ws_id}/nudge")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["action"] == "restarted"
    new_id: str = body["task_id"]
    assert new_id != old_id

    # Old task must be cancelled.
    assert api_module.TASKS[old_id]["status"] == "cancelled"

    # New task must exist with the same workspace and command/args.
    new_task = api_module.TASKS[new_id]
    assert new_task["workspace_id"] == ws_id
    assert new_task["command"] == "run"
    assert new_task["args"] == ["--workflow", "engineering"]


@pytest.mark.asyncio
async def test_nudge_restarts_pending_task(
    client: AsyncClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    ws_id = "_nudge_test_pending"
    old_id = "_nudge_test_task_pending"
    _add_workspace(ws_id, tmp_path)
    _add_task(ws_id, old_id, status="pending")  # pending counts as cancellable

    async def _noop(task_id: str) -> None:
        await asyncio.sleep(0)

    monkeypatch.setattr(api_module, "_run_task", _noop)

    r = await client.post(f"/api/workspaces/{ws_id}/nudge")
    assert r.status_code == 200
    body = r.json()
    assert body["action"] == "restarted"
    assert api_module.TASKS[old_id]["status"] == "cancelled"
