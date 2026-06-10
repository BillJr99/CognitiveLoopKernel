"""Tests for GET /api/providers/discover (guided-mode provider scan).

Network probes and PATH lookups are monkeypatched so the test is hermetic;
the point is the response shape and the availability/mode classification.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("CLK_WORKSPACES_DIR", "/tmp/clk-workspaces-discover-test")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from clk_harness.api import app  # noqa: E402
from clk_harness import env_file, webui_router  # noqa: E402


@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


@pytest.fixture(autouse=True)
def isolated_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("CLK_ENV_FILE", str(target))
    for key in ("ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY", "GOOGLE_API_KEY"):
        monkeypatch.delenv(key, raising=False)
    return target


@pytest.fixture(autouse=True)
def no_network(monkeypatch: pytest.MonkeyPatch):
    """Stub the HTTP probe: ollama up with two models, openwebui down."""

    def fake_probe(ptype: str, endpoint: str, api_key: str) -> dict:
        if ptype == "ollama":
            return {
                "ok": True, "supported": True, "reachable": True,
                "models": ["llama3.1", "qwen2.5-coder"],
                "endpoint": "http://host.docker.internal:11434",
            }
        return {
            "ok": True, "supported": True, "reachable": False,
            "models": [], "endpoint": endpoint or "http://localhost:8080",
        }

    monkeypatch.setattr(webui_router, "_probe_blocking", fake_probe)


@pytest.mark.asyncio
async def test_discover_shape_and_shell_absent(client: AsyncClient, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    r = await client.get("/api/providers/discover")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    names = [p["name"] for p in body["providers"]]
    assert "shell" not in names
    assert set(names) == {"ollama", "openwebui", "claude", "codex", "gemini", "pi"}
    for p in body["providers"]:
        for field in ("name", "type", "kind", "label", "available", "models",
                      "needs_api_key", "api_key_env", "mode"):
            assert field in p, f"{p['name']} missing {field}"


@pytest.mark.asyncio
async def test_discover_http_probe_results(client: AsyncClient, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    r = await client.get("/api/providers/discover")
    by_name = {p["name"]: p for p in r.json()["providers"]}
    ollama = by_name["ollama"]
    assert ollama["available"] is True
    assert ollama["kind"] == "http"
    assert ollama["models"] == ["llama3.1", "qwen2.5-coder"]
    assert ollama["endpoint"] == "http://host.docker.internal:11434"
    assert by_name["openwebui"]["available"] is False
    # Available providers sort first.
    names = [p["name"] for p in r.json()["providers"]]
    assert names.index("ollama") < names.index("openwebui")


@pytest.mark.asyncio
async def test_discover_cli_key_classification(client: AsyncClient, monkeypatch) -> None:
    import shutil

    # No CLIs on PATH, but an Anthropic key in the global .env -> claude is
    # available in "api" mode; codex/gemini stay unavailable and report
    # which env var would unlock them.
    monkeypatch.setattr(shutil, "which", lambda cmd: None)
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-test"})
    r = await client.get("/api/providers/discover")
    by_name = {p["name"]: p for p in r.json()["providers"]}
    claude = by_name["claude"]
    assert claude["available"] is True
    assert claude["mode"] == "api"
    assert claude["cli_found"] is False
    assert claude["key_set"] is True
    assert claude["api_key_env"] == "ANTHROPIC_API_KEY"
    assert by_name["codex"]["available"] is False
    assert by_name["codex"]["needs_api_key"] is True
    assert by_name["codex"]["api_key_env"] == "OPENAI_API_KEY"
    assert by_name["gemini"]["api_key_env"] == "GEMINI_API_KEY"


@pytest.mark.asyncio
async def test_discover_cli_on_path(client: AsyncClient, monkeypatch) -> None:
    import shutil

    monkeypatch.setattr(
        shutil, "which", lambda cmd: "/usr/bin/claude" if cmd == "claude" else None
    )
    r = await client.get("/api/providers/discover")
    by_name = {p["name"]: p for p in r.json()["providers"]}
    claude = by_name["claude"]
    assert claude["available"] is True
    assert claude["mode"] == "cli"
    assert claude["cli_found"] is True
