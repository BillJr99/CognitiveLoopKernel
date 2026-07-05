"""REST API tests for clk_harness/api.py.

Uses httpx.AsyncClient with ASGITransport to drive the FastAPI app without a
real server. Subprocess calls are patched out so tests do not require CLK
installed or any real filesystem state beyond the ephemeral tmp_path fixture.

CRITICAL design constraint
--------------------------
asyncio.create_task() only *schedules* a coroutine — it does not run it.
_run_task therefore runs on the next event-loop iteration, which happens
the first time the test (or the ASGI transport) awaits *after* the task is
created.  Every test that submits a research task must keep the
``with patch(...)`` context manager open until _run_task has finished, so
the fake subprocess is still in place when the coroutine actually executes.
Failing to do this causes _run_task to call the *real* asyncio subprocess,
which spawns a real CLK process; pytest-asyncio then hangs waiting for that
process to finish when tearing down the event loop.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import AsyncIterator
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

# Point workspaces at a temp location before importing the app so the module-
# level WORKSPACES_DIR is set correctly.
os.environ.setdefault("CLK_WORKSPACES_DIR", "/tmp/clk-workspaces-test")

from clk_harness.api import TASKS, WORKSPACES, _task_handles, app  # noqa: E402

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


async def _wait_for_status(
    client: AsyncClient,
    task_id: str,
    terminal: tuple[str, ...] = ("done", "failed", "cancelled"),
    iterations: int = 100,
    interval: float = 0.1,
) -> dict:
    """Poll GET /api/research/{task_id} until a terminal status is reached."""
    resp = None
    for _ in range(iterations):
        await asyncio.sleep(interval)
        resp = await client.get(f"/api/research/{task_id}")
        if resp.json()["status"] in terminal:
            break
    assert resp is not None
    return resp.json()


@pytest_asyncio.fixture(autouse=True)
async def _reset_state(tmp_path: Path) -> AsyncIterator[None]:
    """Clear in-memory task/workspace state and set workspaces dir before each test.

    Teardown cancels and *awaits* all outstanding asyncio task handles so that
    pytest-asyncio does not hang waiting for them when it closes the event loop.
    """
    import clk_harness.api as api_mod
    TASKS.clear()
    WORKSPACES.clear()
    _task_handles.clear()
    api_mod.WORKSPACES_DIR = tmp_path / "workspaces"
    yield
    # Cancel any still-running background asyncio tasks.
    handles = list(_task_handles.values())
    for h in handles:
        h.cancel()
    # Await all handles so the event loop is idle before we clear state.
    # return_exceptions=True prevents a CancelledError from propagating.
    if handles:
        await asyncio.gather(*handles, return_exceptions=True)
    TASKS.clear()
    WORKSPACES.clear()
    _task_handles.clear()


@pytest_asyncio.fixture
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
async def test_create_research_returns_task_id(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"done")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        resp = await client.post("/api/research", json={"command": "init"})
        assert resp.status_code == 202
        data = resp.json()
        assert data["ok"] is True
        assert "task_id" in data
        assert "workspace_id" in data
        # Keep patch active until _run_task finishes (fake proc exits immediately).
        await asyncio.sleep(0.2)


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

        # Allow the background task a moment to run — must be inside the patch
        # context so _run_task sees the fake proc, not the real CLK binary.
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

        # Poll for completion — keep patch active so _run_task uses the fake proc.
        data = await _wait_for_status(client, task_id)

    assert data["status"] == "done"
    assert data["exit_code"] == 0


@pytest.mark.asyncio
async def test_task_fails_on_nonzero_exit(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=1, output=b"error output")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
        task_id = create_resp.json()["task_id"]

        data = await _wait_for_status(client, task_id)

    assert data["status"] == "failed"


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

    # Keep the patch active through the entire cancel sequence so _run_task
    # uses the mock proc (with the blocking readline) rather than real CLK.
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
        task_id = create_resp.json()["task_id"]

        # Give _run_task time to start and reach readline() while patch is active.
        await asyncio.sleep(0.05)

        cancel_resp = await client.post(f"/api/research/{task_id}/cancel")
        assert cancel_resp.status_code == 200
        assert cancel_resp.json()["ok"] is True

        # Allow the CancelledError to propagate through the task.
        await asyncio.sleep(0.1)

        status_resp = await client.get(f"/api/research/{task_id}")
        assert status_resp.json()["status"] == "cancelled"


@pytest.mark.asyncio
async def test_cancel_already_done_task_returns_error(client: AsyncClient) -> None:
    fake_proc = _make_fake_proc(returncode=0, output=b"")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        create_resp = await client.post("/api/research", json={"command": "init"})
        task_id = create_resp.json()["task_id"]

        # Wait for completion — keep patch active.
        await _wait_for_status(client, task_id)

        # Now try to cancel the completed task.
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
    assert resp.status_code == 404
    data = resp.json()
    # FastAPI 404s for unknown routes are wrapped by our HTTPException handler.
    assert data["ok"] is False
    assert "error" in data


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


# ---------------------------------------------------------------------------
# Auto-init and workflow injection
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_auto_init_runs_for_non_init_command(client: AsyncClient) -> None:
    """When command != 'init' and .clk/ is absent, _run_task should run init first."""
    calls: list[str] = []

    async def fake_create_subprocess(*args, **kwargs):
        calls.append(str(args[0]) if args else "?")
        return _make_fake_proc(returncode=0, output=b"")

    resp = await client.post("/api/workspaces", json={"name": "test-ws"})
    ws_id = resp.json()["workspace_id"]

    with patch("clk_harness.api.asyncio.create_subprocess_exec", fake_create_subprocess):
        resp = await client.post("/api/research", json={"command": "idea", "workspace_id": ws_id})
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]

        # Wait for both the implicit init and the idea command to run.
        await _wait_for_status(client, task_id)

    # Should have called subprocess at least twice: once for init, once for idea.
    assert len(calls) >= 2


@pytest.mark.asyncio
async def test_workflow_injection_adds_workflow_arg(client: AsyncClient) -> None:
    """POST /api/research with workflow= should prepend --workflow <name> to args."""
    captured_args: list[list[str]] = []

    async def fake_create_subprocess(*args, **kwargs):
        captured_args.append(list(args))
        return _make_fake_proc(returncode=0, output=b"")

    with patch("clk_harness.api.asyncio.create_subprocess_exec", fake_create_subprocess):
        resp = await client.post(
            "/api/research",
            json={"command": "run", "workflow": "my-flow"},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        await _wait_for_status(client, task_id)

    flat = [a for call in captured_args for a in call]
    assert "--workflow" in flat
    wf_idx = flat.index("--workflow")
    assert flat[wf_idx + 1] == "my-flow"


@pytest.mark.asyncio
async def test_workflow_injection_skipped_when_already_present(client: AsyncClient) -> None:
    """If args already contain --workflow, do not inject a second one."""
    captured_args: list[list[str]] = []

    async def fake_create_subprocess(*args, **kwargs):
        captured_args.append(list(args))
        return _make_fake_proc(returncode=0, output=b"")

    with patch("clk_harness.api.asyncio.create_subprocess_exec", fake_create_subprocess):
        resp = await client.post(
            "/api/research",
            json={"command": "run", "workflow": "my-flow", "args": ["--workflow", "other-flow"]},
        )
        assert resp.status_code == 202
        task_id = resp.json()["task_id"]
        await _wait_for_status(client, task_id)

    flat = [a for call in captured_args for a in call]
    assert flat.count("--workflow") == 1  # no duplicate injection


# ---------------------------------------------------------------------------
# Artifact path traversal
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_artifact_path_traversal_rejected(client: AsyncClient) -> None:
    """Path traversal attempts must return 403."""
    fake_proc = _make_fake_proc(returncode=0, output=b"")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        resp = await client.post("/api/workspaces", json={"name": "sec-test"})
        ws_id = resp.json()["workspace_id"]
        resp2 = await client.post("/api/research", json={"command": "init", "workspace_id": ws_id})
        task_id = resp2.json()["task_id"]
        await asyncio.sleep(0.1)

    resp3 = await client.get(f"/api/research/{task_id}/artifacts/../../../etc/passwd")
    assert resp3.status_code in (403, 404)


@pytest.mark.asyncio
async def test_artifact_in_workspace_accessible(client: AsyncClient, tmp_path: Path) -> None:
    """A file inside the workspace should be downloadable."""
    import clk_harness.api as api_mod

    fake_proc = _make_fake_proc(returncode=0, output=b"")
    with patch("clk_harness.api.asyncio.create_subprocess_exec", AsyncMock(return_value=fake_proc)):
        resp = await client.post("/api/workspaces", json={"name": "artifact-test"})
        ws_id = resp.json()["workspace_id"]
        resp2 = await client.post("/api/research", json={"command": "init", "workspace_id": ws_id})
        task_id = resp2.json()["task_id"]
        await asyncio.sleep(0.1)

    # Write a file into the workspace directory.
    ws_path = api_mod.WORKSPACES_DIR / ws_id
    ws_path.mkdir(parents=True, exist_ok=True)
    (ws_path / "output.txt").write_text("hello artifact")

    resp3 = await client.get(f"/api/research/{task_id}/artifacts/output.txt")
    assert resp3.status_code == 200
