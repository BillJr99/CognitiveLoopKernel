"""In-process ASGI tests for the web-UI REST surface (webui_router + static_spa).

Drives ``clk_harness.api.app`` via httpx ASGITransport (no real server, no
subprocess). Covers .env masking/round-trip, per-workspace config GET/PUT,
the activity/snapshot endpoints over a synthetic activity.jsonl, the SPA
fallback, and the 404 envelope.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("CLK_WORKSPACES_DIR", "/tmp/clk-workspaces-webui-test")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from clk_harness.api import app  # noqa: E402
from clk_harness import env_file  # noqa: E402


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("CLK_ENV_FILE", str(target))
    return target


async def _make_workspace(client: AsyncClient, name: str = "ws") -> dict:
    r = await client.post("/api/workspaces", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()


# ---------------------------------------------------------------------------
# .env
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_env_get_masks_secrets(client: AsyncClient) -> None:
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-secret", "CLK_PROVIDER": "claude"})
    r = await client.get("/api/env")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    by_key = {v["key"]: v for v in body["vars"]}
    assert by_key["ANTHROPIC_API_KEY"]["value"] == env_file.MASK_SENTINEL
    assert by_key["ANTHROPIC_API_KEY"]["masked"] is True
    assert by_key["CLK_PROVIDER"]["value"] == "claude"
    assert "API Keys" in body["groups"]


@pytest.mark.asyncio
async def test_env_schema_endpoint(client: AsyncClient) -> None:
    r = await client.get("/api/env/schema")
    body = r.json()
    assert body["ok"] is True
    names = {g["name"] for g in body["groups"]}
    assert "Core" in names and "API Keys" in names
    # No values are returned in the schema endpoint.
    for g in body["groups"]:
        for v in g["vars"]:
            assert "value" not in v


@pytest.mark.asyncio
async def test_env_put_sentinel_preserves_secret(client: AsyncClient) -> None:
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-keepme"})
    # New plain value + sentinel for the secret.
    r = await client.put("/api/env", json={"values": {
        "ANTHROPIC_API_KEY": env_file.MASK_SENTINEL,
        "CLK_PROVIDER": "ollama",
    }})
    assert r.status_code == 200
    assert env_file.read_env()["ANTHROPIC_API_KEY"] == "sk-keepme"
    assert env_file.read_env()["CLK_PROVIDER"] == "ollama"


@pytest.mark.asyncio
async def test_env_put_new_secret_value(client: AsyncClient) -> None:
    r = await client.put("/api/env", json={"values": {"OPENAI_API_KEY": "sk-brandnew"}})
    assert r.status_code == 200
    assert env_file.read_env()["OPENAI_API_KEY"] == "sk-brandnew"
    # And it is masked on read-back.
    body = (await client.get("/api/env")).json()
    by_key = {v["key"]: v for v in body["vars"]}
    assert by_key["OPENAI_API_KEY"]["value"] == env_file.MASK_SENTINEL


@pytest.mark.asyncio
async def test_env_reveal_disabled_by_default(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CLK_API_ALLOW_REVEAL", raising=False)
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-secret"})
    r = await client.get("/api/env/reveal/ANTHROPIC_API_KEY")
    assert r.status_code == 403
    assert r.json()["ok"] is False


@pytest.mark.asyncio
async def test_env_reveal_when_enabled(client: AsyncClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLK_API_ALLOW_REVEAL", "1")
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-secret"})
    r = await client.get("/api/env/reveal/ANTHROPIC_API_KEY")
    assert r.status_code == 200
    assert r.json()["value"] == "sk-secret"


# ---------------------------------------------------------------------------
# Per-workspace config
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_clk_config_get_put_round_trip(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "cfg")
    wid = ws["workspace_id"]
    # Defaults come back even before init.
    r = await client.get(f"/api/workspaces/{wid}/config/clk")
    assert r.status_code == 200
    cfg = r.json()["config"]
    assert "default_provider" in cfg

    cfg["max_iterations"] = 42
    r = await client.put(f"/api/workspaces/{wid}/config/clk", json={"config": cfg})
    assert r.status_code == 200
    assert r.json()["config"]["max_iterations"] == 42


@pytest.mark.asyncio
async def test_providers_config_masks_and_preserves_secret(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "prov")
    wid = ws["workspace_id"]
    # Seed a provider api_key directly on disk.
    from clk_harness.config import Paths, load_providers_config, save_providers_config
    paths = Paths(root=Path(ws["path"]))
    paths.ensure()
    cfg = load_providers_config(paths)
    cfg.setdefault("providers", {}).setdefault("claude", {})["api_key"] = "sk-prov-secret"
    save_providers_config(paths, cfg)

    r = await client.get(f"/api/workspaces/{wid}/config/providers")
    body = r.json()
    assert body["providers"]["claude"]["api_key"] == env_file.MASK_SENTINEL

    # PUT back with the mask -> secret preserved on disk.
    r = await client.put(f"/api/workspaces/{wid}/config/providers",
                         json={"providers": body["providers"], "active": "claude"})
    assert r.status_code == 200
    reloaded = load_providers_config(paths)
    assert reloaded["providers"]["claude"]["api_key"] == "sk-prov-secret"
    assert reloaded["active"] == "claude"


@pytest.mark.asyncio
async def test_agents_config_round_trip(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "roster")
    wid = ws["workspace_id"]
    r = await client.put(f"/api/workspaces/{wid}/config/agents",
                         json={"agents": {"chief": {"role": "lead"}, "scribe": {"role": "notes"}}})
    assert r.status_code == 200
    agents = r.json()["agents"]
    assert "scribe" in agents and agents["scribe"]["role"] == "notes"


@pytest.mark.asyncio
async def test_doctor_endpoint(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "doc")
    r = await client.get(f"/api/workspaces/{ws['workspace_id']}/doctor")
    body = r.json()
    assert body["ok"] is True
    assert "findings" in body
    assert "active_provider" in body


# ---------------------------------------------------------------------------
# Activity + snapshot
# ---------------------------------------------------------------------------

def _seed_activity(ws_path: str) -> None:
    log = Path(ws_path) / ".clk" / "logs" / "activity.jsonl"
    log.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"event": "agent_dispatch", "agent": "engineer", "run_id": "r1", "provider": "shell (shell)", "workflow": "engineering"},
        {"event": "prompt_sent", "agent": "engineer", "run_id": "r1", "prompt": "go", "prompt_path": ".clk/runs/r1/prompt.txt"},
        {"event": "agent_response", "agent": "engineer", "run_id": "r1", "ok": True,
         "tokens_in": 100, "tokens_out": 50, "tokens_total": 150, "response_text": "Decision: done", "files_reported": ["a.py"]},
        {"event": "git_commit", "message": "init"},
    ]
    log.write_text("\n".join(json.dumps(e) for e in events) + "\n", encoding="utf-8")


@pytest.mark.asyncio
async def test_activity_history_and_filter(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "act")
    _seed_activity(ws["path"])
    r = await client.get(f"/api/workspaces/{ws['workspace_id']}/activity")
    body = r.json()
    assert body["ok"] is True
    kinds = [e["kind"] for e in body["events"]]
    assert "agent_dispatch" in kinds and "agent_response" in kinds

    # kinds filter.
    r2 = await client.get(f"/api/workspaces/{ws['workspace_id']}/activity?kinds=git_commit")
    body2 = r2.json()
    assert all(e["kind"] == "git_commit" for e in body2["events"])
    assert len(body2["events"]) == 1


@pytest.mark.asyncio
async def test_snapshot_endpoint(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "snap")
    _seed_activity(ws["path"])
    r = await client.get(f"/api/workspaces/{ws['workspace_id']}/snapshot")
    snap = r.json()["snapshot"]
    assert snap["agents"]["engineer"]["status"] == "done"
    assert snap["totals"]["total_tokens"] == 150
    assert snap["totals"]["commits"] == 1
    assert "a.py" in snap["files_changed"]


@pytest.mark.asyncio
async def test_unknown_workspace_404(client: AsyncClient) -> None:
    r = await client.get("/api/workspaces/does-not-exist/snapshot")
    assert r.status_code == 404
    assert r.json()["ok"] is False


# ---------------------------------------------------------------------------
# Workspace files: list / read / write + idea
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_files_list_read_write_roundtrip(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "files-ws")
    wid = ws["workspace_id"]

    r = await client.put(f"/api/workspaces/{wid}/file", json={"path": "src/app.py", "content": "print('hi')\n"})
    assert r.status_code == 200, r.text
    assert r.json()["path"] == "src/app.py"

    r = await client.get(f"/api/workspaces/{wid}/files")
    assert r.status_code == 200
    paths = [f["path"] for f in r.json()["files"]]
    assert "src/app.py" in paths

    r = await client.get(f"/api/workspaces/{wid}/file", params={"path": "src/app.py"})
    assert r.status_code == 200
    body = r.json()
    assert body["binary"] is False
    assert body["content"] == "print('hi')\n"


@pytest.mark.asyncio
async def test_rename_workspace(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "old-name")
    wid = ws["workspace_id"]
    r = await client.patch(f"/api/workspaces/{wid}", json={"name": "new-name"})
    assert r.status_code == 200, r.text
    assert r.json()["workspace"]["name"] == "new-name"
    listing = (await client.get("/api/workspaces")).json()["workspaces"]
    assert any(w["id"] == wid and w["name"] == "new-name" for w in listing)
    # Empty name is rejected.
    r = await client.patch(f"/api/workspaces/{wid}", json={"name": "  "})
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_download_workspace_zip(client: AsyncClient) -> None:
    import io
    import zipfile

    ws = await _make_workspace(client, "zip-ws")
    wid = ws["workspace_id"]
    ws_path = Path(ws["path"])
    (ws_path / "src").mkdir(parents=True, exist_ok=True)
    (ws_path / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    (ws_path / ".clk" / "logs").mkdir(parents=True, exist_ok=True)
    (ws_path / ".clk" / "logs" / "activity.jsonl").write_text("{}\n", encoding="utf-8")

    r = await client.get(f"/api/workspaces/{wid}/download")
    assert r.status_code == 200
    assert r.headers["content-type"] == "application/zip"
    assert "zip-ws.zip" in r.headers.get("content-disposition", "")
    zf = zipfile.ZipFile(io.BytesIO(r.content))
    names = zf.namelist()
    assert "src/app.py" in names
    # Harness-internal files are excluded from the archive.
    assert not any(n.startswith(".clk") for n in names)


@pytest.mark.asyncio
async def test_files_list_hides_internal_dirs(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "hide-ws")
    wid = ws["workspace_id"]
    ws_path = Path(ws["path"])
    (ws_path / ".clk" / "logs").mkdir(parents=True, exist_ok=True)
    (ws_path / ".clk" / "logs" / "activity.jsonl").write_text("{}\n", encoding="utf-8")
    (ws_path / "README.md").write_text("# hi\n", encoding="utf-8")

    r = await client.get(f"/api/workspaces/{wid}/files")
    paths = [f["path"] for f in r.json()["files"]]
    assert "README.md" in paths
    assert not any(p.startswith(".clk") for p in paths)


@pytest.mark.asyncio
async def test_file_traversal_is_blocked(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "trav-ws")
    wid = ws["workspace_id"]
    r = await client.get(f"/api/workspaces/{wid}/file", params={"path": "../../etc/passwd"})
    assert r.status_code == 403
    assert r.json()["ok"] is False
    # Writes into a harness-internal dir are refused...
    r = await client.put(f"/api/workspaces/{wid}/file", json={"path": ".clk/state/idea.json", "content": "x"})
    assert r.status_code == 403
    # ...and so are reads (internal logs/state must not leak through this API).
    r = await client.get(f"/api/workspaces/{wid}/file", params={"path": ".clk/logs/activity.jsonl"})
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_set_idea_writes_brief(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "idea-ws")
    wid = ws["workspace_id"]
    ws_path = Path(ws["path"])
    r = await client.put(f"/api/workspaces/{wid}/idea", json={"statement": "Build a thing. With tests."})
    assert r.status_code == 200, r.text
    assert r.json()["title"] == "Build a thing"
    idea = json.loads((ws_path / ".clk" / "state" / "idea.json").read_text())
    assert idea["statement"] == "Build a thing. With tests."
    assert (ws_path / ".clk" / "state" / "system_brief.md").exists()


# ---------------------------------------------------------------------------
# Provider model probe + shell-fallback doctor warning
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_probe_unsupported_provider(client: AsyncClient) -> None:
    r = await client.post("/api/providers/probe", json={"type": "claude"})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is False
    assert body["models"] == []


@pytest.mark.asyncio
async def test_probe_ollama_unreachable(client: AsyncClient) -> None:
    # A bogus endpoint must not raise — it reports unreachable + no models.
    r = await client.post("/api/providers/probe", json={"type": "ollama", "endpoint": "http://127.0.0.1:1"})
    assert r.status_code == 200
    body = r.json()
    assert body["supported"] is True
    assert body["reachable"] is False
    assert body["models"] == []


@pytest.mark.asyncio
async def test_get_providers_always_includes_defaults(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "prov-ws")
    wid = ws["workspace_id"]
    # Save a deliberately sparse providers.json (active set, no blocks).
    await client.put(f"/api/workspaces/{wid}/config/providers", json={"providers": {}, "active": "ollama"})
    r = await client.get(f"/api/workspaces/{wid}/config/providers")
    body = r.json()
    # Every built-in provider card is still offered so the UI can activate one.
    for name in ("shell", "claude", "ollama", "openwebui"):
        assert name in body["providers"], name
    assert body["active"] == "ollama"


@pytest.mark.asyncio
async def test_doctor_flags_shell_provider(client: AsyncClient) -> None:
    ws = await _make_workspace(client, "doc-ws")
    wid = ws["workspace_id"]
    r = await client.get(f"/api/workspaces/{wid}/doctor")
    assert r.status_code == 200
    body = r.json()
    # Default active provider is the shell stub; the doctor should warn.
    assert body["active_provider"] == "shell"
    assert any(f["name"] == "active_provider" and "shell" in f["message"] for f in body["findings"])


# ---------------------------------------------------------------------------
# SPA serving + envelope
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_root_serves_something(client: AsyncClient) -> None:
    r = await client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers.get("content-type", "")


@pytest.mark.asyncio
async def test_unknown_api_route_returns_envelope(client: AsyncClient) -> None:
    r = await client.get("/api/totally-unknown")
    assert r.status_code == 404
    body = r.json()
    assert body["ok"] is False
    assert "error" in body
