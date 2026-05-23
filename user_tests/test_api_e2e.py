"""End-to-end REST API tests.

Unlike ``tests/test_api.py`` (which drives the ASGI app in-process), these
tests start the real ``clk-api`` server in a subprocess and hit it over
HTTP.  They verify the public contract documented in ``docs/REST_API.md``
end-to-end.
"""

from __future__ import annotations

import json
import time
from urllib.parse import urljoin

import httpx
import pytest


# ---------------------------------------------------------------------------
# Health & capabilities
# ---------------------------------------------------------------------------


def test_healthz(api_server: str) -> None:
    r = httpx.get(f"{api_server}/api/healthz", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert "version" in data
    assert "uptime_s" in data


def test_capabilities(api_server: str) -> None:
    r = httpx.get(f"{api_server}/api/capabilities", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    assert isinstance(data["modes"], list)
    for required in ("init", "idea", "plan", "run", "loop", "status"):
        assert required in data["modes"], f"capability {required!r} missing"


def test_list_workflows(api_server: str) -> None:
    r = httpx.get(f"{api_server}/api/workflows", timeout=10)
    assert r.status_code == 200
    data = r.json()
    assert data["ok"] is True
    names = {w["name"] for w in data["workflows"]}
    for required in ("engineering", "discovery", "product", "ralph_loop"):
        assert required in names, f"workflow {required!r} not listed"


# ---------------------------------------------------------------------------
# Workspace CRUD
# ---------------------------------------------------------------------------


def test_workspace_create_list_delete(api_server: str) -> None:
    # Empty list at start
    r = httpx.get(f"{api_server}/api/workspaces", timeout=10)
    assert r.status_code == 200
    assert r.json()["workspaces"] == []

    # Create
    r = httpx.post(
        f"{api_server}/api/workspaces",
        json={"name": "alpha"},
        timeout=10,
    )
    assert r.status_code == 201
    ws_id = r.json()["workspace_id"]
    assert ws_id

    # List shows it
    r = httpx.get(f"{api_server}/api/workspaces", timeout=10)
    names = [w["name"] for w in r.json()["workspaces"]]
    assert "alpha" in names

    # Delete
    r = httpx.delete(f"{api_server}/api/workspaces/{ws_id}", timeout=10)
    assert r.status_code == 200
    assert r.json()["ok"] is True

    # Gone from list
    r = httpx.get(f"{api_server}/api/workspaces", timeout=10)
    ids = [w["id"] for w in r.json()["workspaces"]]
    assert ws_id not in ids


def test_delete_unknown_workspace_returns_envelope(api_server: str) -> None:
    r = httpx.delete(
        f"{api_server}/api/workspaces/00000000-0000-0000-0000-000000000000",
        timeout=10,
    )
    assert r.status_code == 404
    data = r.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "workspace_not_found"


# ---------------------------------------------------------------------------
# Research tasks: ``init`` against a real CLK installation
# ---------------------------------------------------------------------------


def _wait_for_task(base: str, task_id: str, *, timeout: float = 60.0) -> dict:
    """Poll GET /api/research/{task_id} until terminal status, or timeout."""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        r = httpx.get(f"{base}/api/research/{task_id}", timeout=10)
        last = r.json()
        if last["status"] in ("done", "failed", "cancelled"):
            return last
        time.sleep(0.25)
    pytest.fail(f"task {task_id} did not terminate in {timeout}s: {last}")


def test_research_init_runs_to_completion(api_server: str) -> None:
    # Create workspace
    ws = httpx.post(
        f"{api_server}/api/workspaces", json={"name": "research-test"},
        timeout=10,
    ).json()["workspace_id"]

    # Kick off init
    r = httpx.post(
        f"{api_server}/api/research",
        json={"command": "init", "workspace_id": ws},
        timeout=10,
    )
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    final = _wait_for_task(api_server, task_id)
    assert final["status"] == "done", f"init failed: {final}"
    assert final["exit_code"] == 0


def test_research_invalid_command(api_server: str) -> None:
    r = httpx.post(
        f"{api_server}/api/research",
        json={"command": "definitely-not-a-command"},
        timeout=10,
    )
    assert r.status_code == 400
    data = r.json()
    assert data["ok"] is False
    assert data["error"]["code"] == "invalid_command"


def test_research_idea_creates_artifacts(api_server: str) -> None:
    ws = httpx.post(
        f"{api_server}/api/workspaces", json={"name": "idea-test"},
        timeout=10,
    ).json()["workspace_id"]

    # idea triggers auto-init then captures the idea (and auto-casts via shell)
    r = httpx.post(
        f"{api_server}/api/research",
        json={
            "command": "idea",
            "args": ["A journaling app", "--title", "Journal", "--no-cast"],
            "workspace_id": ws,
        },
        timeout=10,
    )
    assert r.status_code == 202
    task_id = r.json()["task_id"]

    final = _wait_for_task(api_server, task_id, timeout=90)
    assert final["status"] == "done", f"idea task failed: {final}"

    # Artifacts endpoint should expose .clk/state/idea.json
    arts = httpx.get(
        f"{api_server}/api/research/{task_id}/artifacts", timeout=10
    ).json()
    paths = {a["path"] for a in arts["artifacts"]}
    assert ".clk/state/idea.json" in paths, f"artifacts: {sorted(paths)[:20]}"

    # Download the artifact and verify its contents
    r = httpx.get(
        f"{api_server}/api/research/{task_id}/artifacts/.clk/state/idea.json",
        timeout=10,
    )
    assert r.status_code == 200
    idea = json.loads(r.text)
    assert idea["title"] == "Journal"


def test_research_stream_emits_sse(api_server: str) -> None:
    ws = httpx.post(
        f"{api_server}/api/workspaces", json={"name": "stream-test"},
        timeout=10,
    ).json()["workspace_id"]

    r = httpx.post(
        f"{api_server}/api/research",
        json={"command": "init", "workspace_id": ws},
        timeout=10,
    )
    task_id = r.json()["task_id"]

    # Read the SSE stream and collect lines until the terminal event arrives.
    lines: list[str] = []
    terminal_seen = False
    with httpx.stream(
        "GET", f"{api_server}/api/research/{task_id}/stream", timeout=60
    ) as resp:
        assert resp.status_code == 200
        ct = resp.headers.get("content-type", "")
        assert "text/event-stream" in ct, f"unexpected content-type: {ct}"
        for raw in resp.iter_lines():
            if not raw:
                continue
            if not raw.startswith("data:"):
                continue
            payload = json.loads(raw[len("data:"):].strip())
            if "status" in payload and "exit_code" in payload:
                terminal_seen = True
                assert payload["status"] in ("done", "failed", "cancelled")
                break
            lines.append(payload["line"])

    assert terminal_seen, "stream closed without a terminal event"
    assert any("CLK initialized" in l for l in lines), \
        f"expected 'CLK initialized' in stream; got: {lines!r}"


def test_research_artifact_path_traversal_blocked(api_server: str) -> None:
    ws = httpx.post(
        f"{api_server}/api/workspaces", json={"name": "traversal-test"},
        timeout=10,
    ).json()["workspace_id"]
    r = httpx.post(
        f"{api_server}/api/research",
        json={"command": "init", "workspace_id": ws},
        timeout=10,
    )
    task_id = r.json()["task_id"]
    _wait_for_task(api_server, task_id)

    r = httpx.get(
        f"{api_server}/api/research/{task_id}/artifacts/../../../etc/passwd",
        timeout=10,
        follow_redirects=False,
    )
    # Either rejected at the boundary (403) or simply not found (404). Both
    # are acceptable — what is NOT acceptable is 200.
    assert r.status_code in (403, 404), f"unexpected status: {r.status_code}"


def test_research_cancel_then_status(api_server: str) -> None:
    ws = httpx.post(
        f"{api_server}/api/workspaces", json={"name": "cancel-test"},
        timeout=10,
    ).json()["workspace_id"]

    # Pick a longer-running command (loop with shell stays modest).
    r = httpx.post(
        f"{api_server}/api/research",
        json={
            "command": "loop",
            "args": ["--max-iterations", "5"],
            "workspace_id": ws,
        },
        timeout=10,
    )
    task_id = r.json()["task_id"]

    # Wait a hair so the task transitions out of pending.
    time.sleep(0.5)
    cancel = httpx.post(
        f"{api_server}/api/research/{task_id}/cancel",
        timeout=10,
    )
    # 200 if successfully cancelled; 400 if already finished
    # (shell loop with 5 iterations can be quite fast).
    assert cancel.status_code in (200, 400)
    if cancel.status_code == 200:
        # Poll status — should report cancelled
        for _ in range(50):
            r = httpx.get(f"{api_server}/api/research/{task_id}", timeout=10)
            if r.json()["status"] in ("cancelled", "done", "failed"):
                break
            time.sleep(0.1)
        assert r.json()["status"] in ("cancelled", "done", "failed")


def test_unknown_route_uses_error_envelope(api_server: str) -> None:
    r = httpx.get(f"{api_server}/api/no-such-endpoint", timeout=10)
    assert r.status_code == 404
    data = r.json()
    assert data["ok"] is False
    assert "error" in data
