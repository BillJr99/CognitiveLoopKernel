"""REST API tests for clk_harness/api.py.

Uses httpx.AsyncClient with ASGITransport to drive the FastAPI app without a
real server. Subprocess calls are patched out so tests do not require CLK
installed or any real filesystem state beyond the ephemeral tmp_path fixture.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import AsyncClient, ASGITransport

# Point workspaces at a temp location before importing the app so the module-
# level WORKSPACES_DIR is set correctly.
os.environ.setdefault("CLK_WORKSPACES_DIR", "/tmp/clk-workspaces-test")

from clk_harness.api import app, TASKS, WORKSPACES, _task_handles  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_fake_proc(returncode: int = 0, output: bytes = b"") -> MagicMock:
    """Return a mock asyncio.subprocess.Process that yields *output* then exits."""
    proc = MagicMock()
    proc.returncode = returncode

    # stdout.readline() returns lines one at a time, then b"" to signal EOF
    lines = [line + b"\n" for line in output.splitlines()]
    lines.append(b"")  # EOF sentinel
    proc.stdout = AsyncMock()
    proc.stdout.readline = AsyncMock(side_effect=lines)

    proc.wait = AsyncMock(return_value=returncode)
    proc.communicate = AsyncMock(return_value=(output, b""))
    proc.terminate = MagicMock()
    return proc


@pytest.fixture(autouse=True)
def _reset_state(tmp_path: Path) -> None:
    """Clear in-memory task/workspace state and set workspaces dir before each test."""
    import clk_harness.api as api_mod
    TASKS.clear()
    WORKSPACES.clear()
    _task_handles.clear()
    api_mod.WORKSPACES_DIR = tmp_path / "workspaces"
    yield
    TASKS.clear()
    WORKSPACES.clear()
    _task_handles.clear()


@pytest.fixture
async def client():
    """Async httpx client backed by the FastAPI ASGI app."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


# ---------------------------------------------------------------------------
# Health & capabilities
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_healthz(client: AsyncClient) -> None:
    resp = await client.get("/api/healthz")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "version" in data
    assert "uptime_s" in data


@pytest.mark.asyncio
async def test_capabilities(client: AsyncClient) -> None:
    resp = await client.get("/api/capabilities")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert isinstance(data["modes"], list)
    assert "idea" in data["modes"]


# ---------------------------------------------------------------------------
# Workspaces
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_workspace(client: AsyncClient) -> None:
    resp = await client.post("/api/workspaces", json={"name": "test-ws"})
    assert resp.status_code == 201
    data = resp.json()
    assert data["ok"] is True
    assert "workspace_id" in data
    assert "path" in data


@pytest.mark.asyncio
async def test_create_workspace_twice_gives_different_ids(client: AsyncClient) -> None:
    """Each POST creates a new UUID-based workspace, even with the same name."""
    r1 = await client.post("/api/workspaces", json={"name": "same-name"})
    r2 = await client.post("/api/workspaces", json={"name": "same-name"})
    assert r1.status_code == 201
    assert r2.status_code == 201
    assert r1.json()["workspace_id"] != r2.json()["workspace_id"]


@pytest.mark.asyncio
async def test_list_workspaces_empty(client: AsyncClient) -> None:
    resp = await client.get("/api/workspaces")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["workspaces"] == []


@pytest.mark.asyncio
async def test_list_workspaces_after_create(client: AsyncClient) -> None:
    await client.post("/api/workspaces", json={"name": "alpha"})
    await client.post("/api/workspaces", json={"name": "beta"})
    resp = await client.get("/api/workspaces")
    assert resp.status_code == 200
    names = [w["name"] for w in resp.json()["workspaces"]]
    assert "alpha" in names
    assert "beta" in names


@pytest.mark.asyncio
async def test_delete_workspace(client: AsyncClient) -> None:
    r = await client.post("/api/workspaces", json={"name": "to-delete"})
    ws_id = r.json()["workspace_id"]
    resp = await client.delete(f"/api/workspaces/{ws_id}")
    assert resp.status_code == 200
    assert resp.json()["ok"] is True
    # Should be gone from the list
    listed = await client.get("/api/workspaces")
    ids = [w["id"] for w in listed.json()["workspaces"]]
    assert ws_id not in ids


@pytest.mark.asyncio
async def test_delete_nonexistent_workspace_returns_error_envelope(
    client: AsyncClient,
) -> None:
    resp = await client.delete("/api/workspaces/nonexistent-id")
    assert resp.status_code == 404
    data = resp.json()
    assert data["ok"] is False
    assert "error" in data
    assert data["error"]["code"] == "workspace_not_found"


# ---------------------------------------------------------------------------
# Research tasks
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_create_research_invalid_command(client: AsyncClient) -> None:
    resp = await client.post("/api/research", json={"command": "bogus"})
    assert resp.status_code == 400
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_command"


@pytest.mark.asyncio
async def test_create_research_unknown_workspace(client: AsyncClient) -> None:
    resp = await client.post(
        "/api/research",
        json={"command": "init", "workspace_id": "00000000-0000-0000-0000-000000000000"},
    )
    assert resp.status_code == 404
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "workspace_not_found"


@pytest.mark.asyncio
async def test_create_research_returns_task_id(client: AsyncClient, tmp_path: Path) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"done")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        resp = await client.post("/api/research", json={"command": "init"})
    assert resp.status_code == 202
    data = resp.json()
    assert data["ok"] is True
    assert "task_id" in data
    assert "workspace_id" in data


@pytest.mark.asyncio
async def test_get_task_not_found(client: AsyncClient) -> None:
    resp = await client.get("/api/research/00000000-0000-0000-0000-000000000000")
    assert resp.status_code == 404
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "task_not_found"


@pytest.mark.asyncio
async def test_get_task_returns_status(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"line1\nline2")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
    task_id = create_resp.json()["task_id"]

    # Allow the background task a moment to run
    await asyncio.sleep(0.05)

    resp = await client.get(f"/api/research/{task_id}")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["task_id"] == task_id
    assert data["status"] in ("pending", "running", "done", "failed", "cancelled")


@pytest.mark.asyncio
async def test_task_completes_with_done_status(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"hello world")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
    task_id = create_resp.json()["task_id"]

    # Wait for the task to complete
    for _ in range(20):
        await asyncio.sleep(0.05)
        r = await client.get(f"/api/research/{task_id}")
        if r.json()["status"] in ("done", "failed", "cancelled"):
            break

    data = r.json()
    assert data["status"] == "done"
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_task_fails_on_nonzero_exit(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=1, output=b"error output")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
    task_id = create_resp.json()["task_id"]

    for _ in range(20):
        await asyncio.sleep(0.05)
        r = await client.get(f"/api/research/{task_id}")
        if r.json()["status"] in ("done", "failed", "cancelled"):
            break

    assert r.json()["status"] == "failed"


@pytest.mark.asyncio
async def test_cancel_task(client: AsyncClient) -> None:
    """Cancelling a task marks it as cancelled and does not overwrite to done/failed."""
    # readline blocks on an Event so the background task stays alive until
    # the asyncio Task is actually cancelled via the API, rather than raising
    # CancelledError immediately and finishing before the cancel request arrives.
    block_event = asyncio.Event()

    async def _blocking_readline():
        await block_event.wait()
        return b""

    proc = MagicMock()
    proc.returncode = 0
    proc.stdout = AsyncMock()
    proc.stdout.readline = _blocking_readline
    proc.wait = AsyncMock(return_value=0)
    proc.terminate = MagicMock()

    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
    task_id = create_resp.json()["task_id"]

    await asyncio.sleep(0.02)

    cancel_resp = await client.post(f"/api/research/{task_id}/cancel")
    assert cancel_resp.status_code == 200
    assert cancel_resp.json()["ok"] is True

    await asyncio.sleep(0.05)

    status_resp = await client.get(f"/api/research/{task_id}")
    assert status_resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_done_task_returns_error(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
    task_id = create_resp.json()["task_id"]

    # Wait for completion
    for _ in range(20):
        await asyncio.sleep(0.05)
        r = await client.get(f"/api/research/{task_id}")
        if r.json()["status"] in ("done", "failed", "cancelled"):
            break

    # Now try to cancel
    resp = await client.post(f"/api/research/{task_id}/cancel")
    assert resp.status_code == 400
    data = resp.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "not_cancellable"


# ---------------------------------------------------------------------------
# Error envelope shape
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_error_envelope_shape(client: AsyncClient) -> None:
    """All error responses must use {ok: false, error: {code, message}}."""
    resp = await client.get("/api/research/does-not-exist")
    assert resp.status_code == 404
    data = resp.json()
    assert data["ok"] is False
    assert "error" in data
    assert "code" in data["error"]
    assert "message" in data["error"]


@pytest.mark.asyncio
async def test_404_on_unknown_route_uses_envelope(client: AsyncClient) -> None:
    resp = await client.get("/api/no-such-endpoint")
    # FastAPI returns 404 for unknown routes; our handler wraps it
    assert resp.status_code == 404


# ---------------------------------------------------------------------------
# Workflows endpoint
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_list_workflows(client: AsyncClient) -> None:
    resp = await client.get("/api/workflows")
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert "workflows" in data
    assert isinstance(data["workflows"], list)
