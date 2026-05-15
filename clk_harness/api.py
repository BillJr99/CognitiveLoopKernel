"""CognitiveLoopKernel REST API.

A thin FastAPI wrapper around the CLK CLI.  Every workspace is an isolated
directory; CLK is invoked as a subprocess with ``cwd=workspace_dir`` so that
``project_paths()`` (which uses ``os.getcwd()``) resolves correctly.

Environment variables
---------------------
CLK_WORKSPACES_DIR
    Root directory under which workspaces are created.
    Defaults to ``/workspaces``.
CLK_API_HOST
    Network interface the server binds to.  Defaults to ``0.0.0.0``
    (all interfaces).  Set to ``127.0.0.1`` to restrict to loopback.
CLK_API_PORT
    TCP port the server binds to when run as ``__main__``.
    Defaults to ``8001``.

Note: The ``clk-api`` console script entry point (``main()``) guards against
missing optional dependencies at import time and will print a clear error if
fastapi/uvicorn are not installed rather than crashing with an ImportError.
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field
from starlette.exceptions import HTTPException as StarletteHTTPException

# ---------------------------------------------------------------------------
# Version
# ---------------------------------------------------------------------------

try:
    from importlib.metadata import version as _pkg_version
    _API_VERSION = _pkg_version("clk-harness")
except Exception:
    _API_VERSION = "0.0.0"

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WORKSPACES_DIR = Path(os.environ.get("CLK_WORKSPACES_DIR", "/workspaces"))
START_TIME = datetime.utcnow()

# Default bind address — exposes the API on ALL interfaces. Intended for
# isolated sandbox / container use; set CLK_API_HOST=127.0.0.1 to restrict.
DEFAULT_HOST = "0.0.0.0"
DEFAULT_PORT = 8001


def get_bind_host() -> str:
    return os.environ.get("CLK_API_HOST", DEFAULT_HOST)


def get_bind_port() -> int:
    try:
        return int(os.environ.get("CLK_API_PORT", str(DEFAULT_PORT)))
    except ValueError:
        return DEFAULT_PORT


COMMANDS = ["init", "idea", "plan", "run", "loop", "status"]
MAX_TASK_LINES = 10_000

logger = logging.getLogger(__name__)

TASKS: Dict[str, Dict[str, Any]] = {}
_task_handles: Dict[str, asyncio.Task] = {}
WORKSPACES: Dict[str, Dict[str, Any]] = {}

app = FastAPI(
    title="CognitiveLoopKernel REST API",
    version=_API_VERSION,
    description="Programmatic HTTP access to the CLK multi-agent development harness.",
)


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(StarletteHTTPException)
async def starlette_http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    """Normalise Starlette 404/405/etc. into the CLK error envelope."""
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {"code": str(exc.status_code), "message": str(exc.detail)}},
    )


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """Normalise FastAPI HTTPException into the CLK error envelope."""
    if isinstance(exc.detail, dict):
        return JSONResponse(status_code=exc.status_code, content=exc.detail)
    return JSONResponse(
        status_code=exc.status_code,
        content={"ok": False, "error": {"code": str(exc.status_code), "message": exc.detail}},
    )


@app.exception_handler(RequestValidationError)
async def validation_exception_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return JSONResponse(
        status_code=422,
        content={"ok": False, "error": {"code": "validation_error", "message": str(exc)}},
    )


@app.exception_handler(Exception)
async def _global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    logger.exception("Unhandled exception on %s", request.url)
    return JSONResponse(
        status_code=500,
        content={"ok": False, "error": {"code": "internal_error", "message": "An internal server error occurred."}},
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now_iso() -> str:
    return datetime.utcnow().isoformat() + "Z"


def _err(code: str, message: str, status: int = 400) -> HTTPException:
    """Return a structured HTTPException using the CLK ``{ok, error}`` envelope."""
    return HTTPException(
        status_code=status,
        detail={"ok": False, "error": {"code": code, "message": message}},
    )


def _ensure_workspaces_dir() -> None:
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)


def _workspace_path(workspace_id: str) -> Path:
    return WORKSPACES_DIR / workspace_id


def _clk_cmd(command: str, args: List[str]) -> List[str]:
    """Build the argv list that invokes ``clk_harness.cli`` as a subprocess."""
    return [sys.executable, "-m", "clk_harness.cli", command] + args


# TASKS in-memory registry shape:
# {
#   "task_id":      str (UUID),
#   "workspace_id": str (UUID),
#   "command":      str (one of COMMANDS),
#   "args":         list[str],
#   "status":       "pending" | "running" | "done" | "failed" | "cancelled",
#   "created_at":   ISO-8601 str,
#   "started_at":   ISO-8601 str | None  (set when _run_task transitions to "running"),
#   "finished_at":  ISO-8601 str | None,
#   "exit_code":    int | None,
#   "lines":        list[str]  (stdout lines, capped at MAX_TASK_LINES),
#   "proc":         asyncio.subprocess.Process | None  (live handle while running),
# }

async def _run_task(task_id: str) -> None:
    """Background coroutine that drives a CLK subprocess for *task_id*.

    Transitions ``TASKS[task_id]["status"]`` through pending → running →
    done | failed | cancelled, and updates ``started_at`` / ``finished_at``
    timestamps.  Always cleans up ``_task_handles`` in its finally block.
    """
    task = TASKS.get(task_id)
    if task is None:
        return
    if task.get("status") == "cancelled":
        task["finished_at"] = _now_iso()
        _task_handles.pop(task_id, None)
        return
    task["status"] = "running"
    task["started_at"] = _now_iso()

    workspace_id = task["workspace_id"]
    ws_path = _workspace_path(workspace_id)
    command = task["command"]
    args = list(task["args"])

    try:
        clk_dir = ws_path / ".clk"
        if command != "init" and not clk_dir.exists():
            try:
                init_proc = await asyncio.create_subprocess_exec(
                    *_clk_cmd("init", []),
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.STDOUT,
                    cwd=str(ws_path),
                )
                task["proc"] = init_proc
                try:
                    init_out, _ = await init_proc.communicate()
                except asyncio.CancelledError:
                    init_proc.terminate()
                    raise
                finally:
                    task["proc"] = None
                for line in (init_out or b"").decode(errors="replace").splitlines():
                    if len(task["lines"]) < MAX_TASK_LINES:
                        task["lines"].append(f"[init] {line}")
                if init_proc.returncode != 0:
                    task["status"] = "failed"
                    task["exit_code"] = init_proc.returncode
                    return
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                if len(task["lines"]) < MAX_TASK_LINES:
                    task["lines"].append(f"[init-error] {exc}")
                task["status"] = "failed"
                task["exit_code"] = -1
                return

        proc = await asyncio.create_subprocess_exec(
            *_clk_cmd(command, args),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(ws_path),
        )
        task["proc"] = proc

        assert proc.stdout is not None
        while True:
            line_bytes = await proc.stdout.readline()
            if not line_bytes:
                break
            if len(task["lines"]) < MAX_TASK_LINES:
                task["lines"].append(line_bytes.decode(errors="replace").rstrip("\n"))

        await proc.wait()
        returncode = proc.returncode
        if task["status"] != "cancelled":
            task["exit_code"] = returncode
            task["status"] = "done" if returncode == 0 else "failed"

    except asyncio.CancelledError:
        task["status"] = "cancelled"
        task["exit_code"] = -1
    except Exception as exc:  # noqa: BLE001
        if len(task["lines"]) < MAX_TASK_LINES:
            task["lines"].append(f"[api-error] {exc}")
        if task["status"] != "cancelled":
            task["status"] = "failed"
        task["exit_code"] = -1
    finally:
        task["finished_at"] = _now_iso()
        task["proc"] = None
        _task_handles.pop(task_id, None)


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------

class WorkspaceCreate(BaseModel):
    name: str


class ResearchRequest(BaseModel):
    command: str
    args: List[str] = Field(default_factory=list)
    workspace_id: Optional[str] = None
    workflow: Optional[str] = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/healthz")
async def healthz() -> Dict[str, Any]:
    uptime = (datetime.utcnow() - START_TIME).total_seconds()
    return {"ok": True, "version": _API_VERSION, "uptime_s": round(uptime, 2)}


@app.get("/api/capabilities")
async def capabilities() -> Dict[str, Any]:
    return {"ok": True, "modes": COMMANDS}


@app.get("/api/workflows")
async def list_workflows() -> Dict[str, Any]:
    try:
        from clk_harness.templates import WORKFLOWS  # type: ignore
        result = []
        for name, body in WORKFLOWS.items():
            description = ""
            try:
                import yaml as _yaml
                parsed = _yaml.safe_load(body)
                description = parsed.get("description", "") if isinstance(parsed, dict) else ""
            except Exception:  # noqa: BLE001
                pass
            if not description:
                import re as _re
                m = _re.search(r"^description:\s*(.+)", body, _re.MULTILINE)
                if m:
                    description = m.group(1).strip().strip("'\"")
            if not description:
                for line in body.splitlines():
                    stripped = line.strip()
                    if stripped.startswith("#"):
                        description = stripped.lstrip("# ").strip()
                        break
            result.append({"name": name.replace(".yaml", ""), "path": name, "description": description})
        return {"ok": True, "workflows": result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("Failed to load workflow templates")
        raise HTTPException(
            status_code=500,
            detail={"ok": False, "error": {"code": "template_load_failed", "message": "Failed to load workflow templates."}},
        ) from exc


@app.post("/api/workspaces", status_code=201)
async def create_workspace(body: WorkspaceCreate) -> Dict[str, Any]:
    _ensure_workspaces_dir()
    workspace_id = str(uuid.uuid4())
    ws_path = _workspace_path(workspace_id)
    ws_path.mkdir(parents=True, exist_ok=True)
    entry = {"id": workspace_id, "name": body.name, "path": str(ws_path), "created_at": _now_iso()}
    WORKSPACES[workspace_id] = entry
    return {"ok": True, "workspace_id": workspace_id, "path": str(ws_path)}


@app.get("/api/workspaces")
async def list_workspaces() -> Dict[str, Any]:
    return {"ok": True, "workspaces": list(WORKSPACES.values())}


@app.delete("/api/workspaces/{workspace_id}")
async def delete_workspace(workspace_id: str) -> Dict[str, Any]:
    if workspace_id not in WORKSPACES:
        raise _err("workspace_not_found", f"Workspace {workspace_id!r} not found.", 404)
    active = [
        t for t in TASKS.values()
        if t.get("workspace_id") == workspace_id and t.get("status") in ("pending", "running")
    ]
    if active:
        raise _err("workspace_in_use", f"Workspace has {len(active)} active task(s). Cancel them first.", 409)
    ws_path = _workspace_path(workspace_id)
    try:
        if ws_path.exists():
            shutil.rmtree(ws_path)
    except Exception as exc:  # noqa: BLE001
        raise _err("delete_failed", str(exc), 500) from exc
    del WORKSPACES[workspace_id]
    return {"ok": True}


@app.post("/api/research", status_code=202)
async def create_research(body: ResearchRequest) -> Dict[str, Any]:
    if body.command not in COMMANDS:
        raise _err("invalid_command", f"command must be one of {COMMANDS}; got {body.command!r}.")

    _ensure_workspaces_dir()

    if body.workspace_id:
        if body.workspace_id not in WORKSPACES:
            raise _err("workspace_not_found", f"Workspace {body.workspace_id!r} not found.", 404)
        workspace_id = body.workspace_id
    else:
        workspace_id = str(uuid.uuid4())
        ws_path = _workspace_path(workspace_id)
        ws_path.mkdir(parents=True, exist_ok=True)
        WORKSPACES[workspace_id] = {
            "id": workspace_id, "name": f"ephemeral-{workspace_id[:8]}",
            "path": str(ws_path), "created_at": _now_iso(),
        }

    args = list(body.args)
    if body.workflow and body.command == "run" and not any(
        a == "--workflow" or a.startswith("--workflow=") for a in args
    ):
        args = ["--workflow", body.workflow] + args

    task_id = str(uuid.uuid4())
    task: Dict[str, Any] = {
        "task_id": task_id, "workspace_id": workspace_id, "command": body.command,
        "args": args, "status": "pending", "created_at": _now_iso(),
        "started_at": None, "finished_at": None, "exit_code": None, "lines": [], "proc": None,
    }
    TASKS[task_id] = task
    _task_handles[task_id] = asyncio.create_task(_run_task(task_id))
    return {"ok": True, "task_id": task_id, "workspace_id": workspace_id}


@app.get("/api/research/{task_id}")
async def get_task(task_id: str) -> Dict[str, Any]:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    return {
        "ok": True, "task_id": task["task_id"], "workspace_id": task["workspace_id"],
        "command": task["command"], "status": task["status"], "created_at": task.get("created_at"),
        "started_at": task["started_at"], "finished_at": task["finished_at"],
        "exit_code": task["exit_code"], "line_count": len(task["lines"]),
    }


@app.get("/api/research/{task_id}/stream")
async def stream_task(task_id: str, request: Request) -> StreamingResponse:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)

    import json as _json

    async def _generate():
        seq = 0
        sent = 0
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
                current_lines = task["lines"]
                while sent < len(current_lines):
                    payload = _json.dumps({"line": current_lines[sent], "seq": seq})
                    yield f"data: {payload}\n\n"
                    sent += 1
                    seq += 1
                done_payload = _json.dumps({"status": task["status"], "exit_code": task["exit_code"]})
                yield f"data: {done_payload}\n\n"
                break
            await asyncio.sleep(0.2)

    return StreamingResponse(_generate(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


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
                artifacts.append({"path": rel, "size": stat.st_size,
                                   "modified": datetime.utcfromtimestamp(stat.st_mtime).isoformat() + "Z"})
    return {"ok": True, "task_id": task_id, "artifacts": artifacts}


@app.get("/api/research/{task_id}/artifacts/{artifact_path:path}")
async def get_artifact(task_id: str, artifact_path: str) -> FileResponse:
    task = TASKS.get(task_id)
    if task is None:
        raise _err("task_not_found", f"Task {task_id!r} not found.", 404)
    ws_path = _workspace_path(task["workspace_id"])
    file_path = (ws_path / artifact_path).resolve()
    ws_path_resolved = ws_path.resolve()
    try:
        file_path.relative_to(ws_path_resolved)
    except ValueError:
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
    task["status"] = "cancelled"
    task["finished_at"] = _now_iso()
    task["exit_code"] = -1
    proc = task.get("proc")
    if proc is not None:
        try:
            proc.terminate()
        except ProcessLookupError:
            pass
    if task_id in _task_handles:
        _task_handles[task_id].cancel()
    return {"ok": True}


def main() -> None:
    """Console-script entry point: ``clk-api``."""
    try:
        import fastapi  # noqa: F401
        import uvicorn  # noqa: F401
    except ImportError:
        print("Error: REST API dependencies not installed. Run: pip install 'clk-harness[api]'",
              file=__import__('sys').stderr)
        raise SystemExit(1)

    uvicorn.run(app, host=get_bind_host(), port=get_bind_port())


if __name__ == "__main__":
    main()
