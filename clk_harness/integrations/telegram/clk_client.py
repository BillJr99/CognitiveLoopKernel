"""Thin async client for the local CLK REST API.

Wraps the endpoints in ``clk_harness/api.py`` so the bot can stay
declarative. All methods accept an optional ``client`` so tests can inject
``httpx.MockTransport``.
"""

from __future__ import annotations

import os
from typing import Any, AsyncIterator, Dict, List, Optional

try:  # pragma: no cover - import error path is exercised by tests via mocks
    import httpx
except ImportError as exc:  # pragma: no cover
    raise ImportError(
        "httpx is required for the Telegram bot; install with `pip install .[telegram]`"
    ) from exc


def default_base_url() -> str:
    host = os.environ.get("CLK_API_HOST", "127.0.0.1")
    port = os.environ.get("CLK_API_PORT", "8001")
    return f"http://{host}:{port}"


class CLKClient:
    def __init__(
        self,
        base_url: Optional[str] = None,
        *,
        timeout: float = 30.0,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.base_url = (base_url or default_base_url()).rstrip("/")
        self._timeout = timeout
        self._client = client

    async def __aenter__(self) -> "CLKClient":
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout)
        return self

    async def __aexit__(self, *_exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    def _c(self) -> httpx.AsyncClient:
        if self._client is None:
            self._client = httpx.AsyncClient(base_url=self.base_url, timeout=self._timeout)
        return self._client

    async def healthz(self) -> Dict[str, Any]:
        r = await self._c().get("/api/healthz")
        r.raise_for_status()
        return r.json()

    async def list_workspaces(self) -> List[Dict[str, Any]]:
        r = await self._c().get("/api/workspaces")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("workspaces", [])
        return data

    async def create_workspace(self, name: Optional[str] = None) -> Dict[str, Any]:
        body: Dict[str, Any] = {}
        if name:
            body["name"] = name
        r = await self._c().post("/api/workspaces", json=body)
        r.raise_for_status()
        return r.json()

    async def start_task(
        self,
        workspace_id: str,
        command: str,
        args: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        body = {"workspace_id": workspace_id, "command": command, "args": args or []}
        r = await self._c().post("/api/research", json=body)
        r.raise_for_status()
        return r.json()

    async def get_task(self, task_id: str) -> Dict[str, Any]:
        r = await self._c().get(f"/api/research/{task_id}")
        r.raise_for_status()
        return r.json()

    async def cancel_task(self, task_id: str) -> Dict[str, Any]:
        r = await self._c().post(f"/api/research/{task_id}/cancel")
        r.raise_for_status()
        return r.json()

    async def stream_task(self, task_id: str) -> AsyncIterator[str]:
        async with self._c().stream("GET", f"/api/research/{task_id}/stream") as resp:
            resp.raise_for_status()
            async for line in resp.aiter_lines():
                if line:
                    yield line

    async def list_artifacts(self, task_id: str) -> List[str]:
        r = await self._c().get(f"/api/research/{task_id}/artifacts")
        r.raise_for_status()
        data = r.json()
        if isinstance(data, dict):
            return data.get("artifacts", [])
        return data
