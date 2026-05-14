"""CognitiveLoopKernel REST API.

A thin FastAPI wrapper around the CLK CLI.  Every workspace is an isolated
directory; CLK is invoked as a subprocess with ``cwd=workspace_dir`` so that
``project_paths()`` (which uses ``os.getcwd()``) resolves correctly.

Environment variables
---------------------
CLK_WORKSPACES_DIR
    Root directory under which workspaces are created.
    Defaults to ``/workspaces``.
CLK_API_PORT
    TCP port the server binds to when run as ``__main__``.
    Defaults to ``8001``.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKSPACES_DIR = Path(os.environ.get("CLK_WORKSPACES_DIR", "/workspaces"))
START_TIME = datetime.utcnow()

COMMANDS = ["init", "idea", "plan", "run", "loop", "status"]

# ---------------------------------------------------------------------------
# In-memory task store
# ---------------------------------------------------------------------------

# task shape:
# {
#   task_id: str,
#   workspace_id: str,
#   command: str,
#   args: list[str],
#   status: "pending" | "running" | "done" | "failed" | "cancelled",
#   started_at: str,          # ISO-8601 UTC
#   finished_at: str | None,
#   exit_code: int | None,
#   lines: list[str],
#   proc: asyncio.subprocess.Process | None,
# }
TASKS: Dict[str, Dict[str, Any]] = {}

# workspace shape:
# {
#   id: str,
#   name: str,
#   path: str,
#   created_at: str,
# }
WORKSPACES: Dict[str, Dict[str, Any]] = {}

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(
    title="CognitiveLoopKernel REST API",
    version="1.0.0",
    description="Programmatic HTTP access to the CLK multi-agent development harness.",
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    return HTTPException(
        status_code=status,
        detail={"ok": False, "error": {"code": code, "message": message}},
    )


def _ensure_workspaces_dir() -> None:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


def _workspace_path(workspace_id: str) -> Path:
    return WORKSPACES_DIR / workspace_id


def _clk_cmd(command: str, args: List[str]) -> List[str]:
    """Build the CLK subprocess command."""
    return [sys.executable, "-m", "clk_harness.cli", command] + args


async def _run_task(task_id: str) -> None:
    """Background coroutine: run CLK as a subprocess and collect output."""
    task = TASKS[task_id]
    workspace_id = task["workspace_id"]
    ws_path = _workspace_path(workspace_id)
    command = task["command"]
    args = list(task["args"])

    task["status"] = "running"
    task["started_at"] = _now_iso()

    # If the workspace is not yet initialised, run `init` first (unless that
    # is already the requested command).
    clk_dir = ws_path / ".clk"
    if command != "init" and not clk_dir.exists():
        try:
            init_proc = await asyncio.create_subprocess_exec(
                *_clk_cmd("init", []),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
                cwd=str(ws_path),
            )
            init_out, _ = await init_proc.communicate()
            for line in (init_out or b"").decode(errors="replace").splitlines():
                task["lines"].append(f"[init] {line}")
        except Exception as exc:  # noqa: BLE001
            task["lines"].append(f"[init-error] {exc}")

    try:
        proc = await asyncio.create_subprocess_exec(
            *_clk_cmd(command, args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ws_path),
        )
        task["proc"] = proc

        # Stream lines as they arrive
        assert proc.stdout is not None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            task["lines"].append(line_bytes.decode(errors="replace").rstrip("\n"))

        await proc.wait()
        task["exit_code"] = proc.returncode
        task["status"] = "done" if proc.returncode == 0 else "failed"

    except asyncio.CancelledError:
        task["status"] = "cancelled"
        task["exit_code"] = -1
    except Exception as exc:  # noqa: BLE001
        task["lines"].append(f"[api-error] {exc}")
        task["status"] = "failed"
        task["exit_code"] = -1
    finally:
        task["finished_at"] = _now_iso()
        task["proc"] = None


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str


class ResearchRequest(BaseModel):
    command: str
    args: List[str] = []
    workspace_id: Optional[str] = None
    workflow: Optional[str] = None  # convenience: injects --workflow <value> for `run`


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

# -- Health & capabilities ---------------------------------------------------

@app.get("/api/healthz")
async def healthz() -> Dict[str, Any]:
    uptime = (datetime.utcnow() - START_TIME).total_seconds()
    return {"ok": True, "version": "1.0.0", "uptime_s": round(uptime, 2)}


@app.get("/api/capabilities")
async def capabilities() -> Dict[str, Any]:
    return {"ok": True, "modes": COMMANDS}


# -- Workflows ---------------------------------------------------------------

@app.get("/api/workflows")
async def list_workflows() -> Dict[str, Any]:
    """Return bundled workflow templates known to the package."""
    try:
        from clk_harness.templates import WORKFLOWS  # type: ignore
        result = []
        for name, body in WORKFLOWS.items():
            # Extract a one-line description from the YAML comment if present
            description = ""
            for line in body.splitlines():
                line = line.strip()
                if line.startswith("#"):
                    description = line.lstrip("# ").strip()
                    break
            result.append({"name": name.replace(".yaml", ""), "path": name, "description": description})
        return {"ok": True, "workflows": result}
    except Exception as exc:  # noqa: BLE001
        return {"ok": True, "workflows": [], "warning": str(exc)}


# -- Workspaces --------------------------------------------------------------

@app.post("/api/workspaces", status_code=201)
async def create_workspace(body: WorkspaceCreate) -> Dict[str, Any]:
    _ensure_workspaces_dir()
    workspace_id = str(uuid.uuid4())
    ws_path = _workspace_path(workspace_id)
    ws_path.mkdir(parents=True, exist_ok=True)
    entry = {
        "id": workspace_id,
        "name": body.name,
        "path": str(ws_path),
        "created_at": _now_iso(),
    }
    WORKSPACES[workspace_id] = entry
    return {"ok": True, "workspace_id": workspace_id, "path": str(ws_path)}


@app.get("/api/workspaces")
async def list_workspaces() -> Dict[str, Any]:
    return {"ok": True, "workspaces": list(WORKSPACES.values())}


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str) -> Dict[str, Any]:
    if workspace_id not in WORKSPACES:
        raise _err("workspace_not_found", f"Workspace {workspace_id!r} not found.", 404)
    ws_path = _workspace_path(workspace_id)
    try:
        if ws_path.exists():
            shutil.rmtree(ws_path)
    except Exception as exc:  # noqa: BLE001
        raise _err("delete_failed", str(exc)) from exc
    del WORKSPACES[workspace_id]
    return {"ok": True}


# -- Research tasks ----------------------------------------------------------

@app.post("/api/research", status_code=202)
async def create_research(body: ResearchRequest) -> Dict[str, Any]:
    if body.command not in COMMANDS:
        raise _err(
            "invalid_command",
            f"command must be one of {COMMANDS}; got {body.command!r}.",
        )

    _ensure_workspaces_dir()

    # Resolve or create workspace
    if body.workspace_id:
        if body.workspace_id not in WORKSPACES:
            raise _err("workspace_not_found", f"Workspace {body.workspace_id!r} not found.", 404)
        workspace_id = body.workspace_id
    else:
        # Ephemeral workspace
        workspace_id = str(uuid.uuid4())
        ws_path = _workspace_path(workspace_id)
        ws_path.mkdir(parents=True, exist_ok=True)
        WORKSPACES[workspace_id] = {
            "id": workspace_id,
            "name": f"ephemeral-{workspace_id[:8]}",
            "path": str(ws_path),
            "created_at": _now_iso(),
        }

    # Build args — inject --workflow if provided and command is `run`
    args = list(body.args)
    if body.workflow and body.command == "run" and "--workflow" not in args:
        args = ["--workflow", body.workflow] + args

    task_id = str(uuid.uuid4())
    task: Dict[str, Any] = {
        "task_id": task_id,
        "workspace_id": workspace_id,
        "command": body.command,
        "args": args,
        "status": "pending",
        "started_at": _now_iso(),
        "finished_at": None,
        "exit_code": None,
        "lines": [],
        "proc": None,
    }
    TASKS[task_id] = task

    asyncio.create_task(_run_task(task_id))

    return {"ok": True, "task_id": task_id, "workspace_id": workspace_id}


@app.get("/api/research/{task_id}")
async def get_task(task_id: str) -> Dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    return {
        "ok": True,
        "task_id": task["task_id"],
        "workspace_id": task["workspace_id"],
        "command": task["command"],
        "status": task["status"],
        "started_at": task["started_at"],
        "finished_at": task["finished_at"],
        "exit_code": task["exit_code"],
        "line_count": len(task["lines"]),
    }


@app.get("/api/research/{task_id}/stream")
async def stream_task(task_id: str, request: Request) -> StreamingResponse:
    """SSE stream of task output lines, then a final status event."""
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)

    import json as _json

    async def _generate():
        seq = 0
        sent = 0  # lines already sent
        while True:
            if await request.is_disconnected():
                break
            current_lines = task["lines"]
            while sent < len(current_lines):
                payload = _json.dumps({"line": current_lines[sent], "seq": seq})
                yield f"data: {payload}\n\n"
                sent += 1
                seq += 1
            if task["status"] in ("done", "failed", "cancelled"):
                # Flush any final lines then send terminal event
                current_lines = task["lines"]
                while sent < len(current_lines):
                    payload = _json.dumps({"line": current_lines[sent], "seq": seq})
                    yield f"data: {payload}\n\n"
                    sent += 1
                    seq += 1
                done_payload = _json.dumps(
                    {"status": task["status"], "exit_code": task["exit_code"]}
                )
                yield f"data: {done_payload}\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(
        _generate(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@app.get("/api/research/{task_id}/artifacts")
async def list_artifacts(task_id: str) -> Dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    ws_path = _workspace_path(task["workspace_id"])
    artifacts = []
    if ws_path.exists():
        for p in sorted(ws_path.rglob("*")):
            if p.is_file():
                rel = str(p.relative_to(ws_path))
                stat = p.stat()
                artifacts.append({
                    "path": rel,
                    "size": stat.st_size,
                    "modified": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z",
                })
    return {"ok": True, "task_id": task_id, "artifacts": artifacts}


@app.get("/api/research/{task_id}/artifacts/{artifact_path:path}")
async def get_artifact(task_id: str, artifact_path: str) -> FileResponse:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    ws_path = _workspace_path(task["workspace_id"])
    file_path = (ws_path / artifact_path).resolve()
    # Safety: must stay inside workspace
    if not str(file_path).startswith(str(ws_path.resolve())):
        raise _err("forbidden", "Path escapes workspace boundary.", 403)
    if not file_path.exists() or not file_path.is_file():
        raise _err("artifact_not_found", f"Artifact {artifact_path!r} not found.", 404)
    return FileResponse(str(file_path))


@app.post("/api/research/{task_id}/cancel")
async def cancel_task(task_id: str) -> Dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    if task["status"] not in ("pending", "running"):
        raise _err("not_cancellable", f"Task is already {task['status']!r}.")
    proc = task.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    task["status"] = "cancelled"
    task["finished_at"] = _now_iso()
    task["exit_code"] = -1
    return {"ok": True}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("CLK_API_PORT", "8001")))
