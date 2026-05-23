import json

import httpx
import pytest

from clk_harness.integrations.telegram.clk_client import CLKClient


def _make_client(handler):
    transport = httpx.MockTransport(handler)
    inner = httpx.AsyncClient(base_url="http://test", transport=transport)
    return CLKClient(base_url="http://test", client=inner)


@pytest.mark.asyncio
async def test_healthz():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/healthz"
        return httpx.Response(200, json={"ok": True})

    c = _make_client(handler)
    out = await c.healthz()
    assert out == {"ok": True}


@pytest.mark.asyncio
async def test_start_task_sends_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(202, json={"task_id": "abc123"})

    c = _make_client(handler)
    out = await c.start_task("ws1", "run", args=["hello"])
    assert out["task_id"] == "abc123"
    assert seen["path"] == "/api/research"
    assert seen["body"] == {"workspace_id": "ws1", "command": "run", "args": ["hello"]}


@pytest.mark.asyncio
async def test_cancel_task():
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/api/research/t1/cancel"
        return httpx.Response(200, json={"ok": True})

    c = _make_client(handler)
    assert (await c.cancel_task("t1"))["ok"] is True


@pytest.mark.asyncio
async def test_get_task_error_propagates():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, json={"error": "not found"})

    c = _make_client(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await c.get_task("missing")


@pytest.mark.asyncio
async def test_list_workspaces_dict_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"workspaces": [{"id": "a"}, {"id": "b"}]})

    c = _make_client(handler)
    out = await c.list_workspaces()
    assert [w["id"] for w in out] == ["a", "b"]


@pytest.mark.asyncio
async def test_list_workspaces_list_shape():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[{"id": "a"}])

    c = _make_client(handler)
    out = await c.list_workspaces()
    assert out == [{"id": "a"}]
