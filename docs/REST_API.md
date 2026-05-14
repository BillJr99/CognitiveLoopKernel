# CLK REST API Reference

The CLK REST API is a thin FastAPI wrapper around the CLK CLI.  It lets you
start, monitor, and cancel research tasks; manage isolated workspaces; and
stream live output from any running CLK command — all over plain HTTP.

## Quick start

```bash
# 1. Install API dependencies
pip install "clk-harness[api]"

# 2. Start the server (default port 8001)
python -m clk_harness.api
# or
uvicorn clk_harness.api:app --host 0.0.0.0 --port 8001

# 3. Create a workspace
curl -s -X POST http://localhost:8001/api/workspaces \
     -H 'Content-Type: application/json' \
     -d '{"name": "my-project"}'
# → {"ok": true, "workspace_id": "<uuid>", "path": "/workspaces/<uuid>"}

# 4. Capture an idea and stream the output
WS=<workspace_id from step 3>
curl -s -X POST http://localhost:8001/api/research \
     -H 'Content-Type: application/json' \
     -d "{\"command\": \"idea\", \"args\": [\"A local-first journaling app\"], \"workspace_id\": \"$WS\"}"
# → {"ok": true, "task_id": "<uuid>", "workspace_id": "<uuid>"}

TASK=<task_id from above>
curl -sN http://localhost:8001/api/research/$TASK/stream
# Streams SSE events until the task finishes.
```

## Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CLK_WORKSPACES_DIR` | `/workspaces` | Root directory under which workspaces are created. Mount a volume here so workspaces persist across container restarts. |
| `CLK_API_PORT` | `8001` | TCP port the server binds to when launched as `python -m clk_harness.api`. |

## Authentication

The API has **no authentication**.  It relies on network-boundary trust.
Do not expose it to the public internet without a reverse-proxy or firewall.

## Response envelope

Every endpoint returns JSON.  Successful responses always include `"ok": true`;
error responses include `"ok": false` and an `error` object:

```json
// success
{ "ok": true, ... }

// error
{
  "ok": false,
  "error": {
    "code": "workspace_not_found",
    "message": "Workspace 'abc' not found."
  }
}
```

---

## Endpoints

### `GET /api/healthz`

Liveness check.

**Response**
```json
{
  "ok": true,
  "version": "1.0.0",
  "uptime_s": 42.7
}
```

---

### `GET /api/capabilities`

Return the list of CLK commands exposed by this API.

**Response**
```json
{
  "ok": true,
  "modes": ["init", "idea", "plan", "run", "loop", "status"]
}
```

---

### `GET /api/workflows`

Return the bundled workflow templates.

**Response**
```json
{
  "ok": true,
  "workflows": [
    { "name": "engineering", "path": "engineering.yaml", "description": "..." },
    { "name": "discovery",   "path": "discovery.yaml",   "description": "..." }
  ]
}
```

---

### `POST /api/workspaces`

Create a named, persistent workspace directory.  Each call allocates a fresh
UUID and a new directory, even if the provided `name` has been used before.
To recover a workspace after a server restart, re-register it by posting with
the same name — note that a **new UUID will be assigned** each time.

**Request body**
```json
{ "name": "my-project" }
```

**Response** `201 Created`
```json
{
  "ok": true,
  "workspace_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6",
  "path": "/workspaces/3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

---

### `GET /api/workspaces`

List all workspaces known to this server instance.

**Response**
```json
{
  "ok": true,
  "workspaces": [
    {
      "id": "3fa85f64-...",
      "name": "my-project",
      "path": "/workspaces/3fa85f64-...",
      "created_at": "2024-01-15T12:00:00.000000Z"
    }
  ]
}
```

> **Note:** The workspace registry is in-memory and resets when the server
> restarts. Workspace directories on disk survive a restart. To re-register an
> existing directory, POST to `/api/workspaces` with any name — a **new UUID
> is always assigned**, so update any stored references accordingly.

---

### `DELETE /api/workspaces/{workspace_id}`

Delete a workspace and all its files.

**Response** `200 OK`
```json
{ "ok": true }
```

---

### `POST /api/research`

Start a CLK command as a background task.

**Request body**

| Field | Type | Required | Description |
|---|---|---|---|
| `command` | string | yes | One of `init`, `idea`, `plan`, `run`, `loop`, `status`. |
| `args` | `string[]` | no | Extra CLI arguments forwarded verbatim (e.g. `["A journaling app"]`). |
| `workspace_id` | string | no | Existing workspace UUID. Omit to create an ephemeral workspace. |
| `workflow` | string | no | Convenience shortcut: injects `--workflow <value>` when `command` is `run`. |

**Example — capture an idea in an existing workspace**
```json
{
  "command": "idea",
  "args": ["A local-first journaling app that summarizes my week"],
  "workspace_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

**Example — run the engineering workflow**
```json
{
  "command": "run",
  "workflow": "engineering",
  "workspace_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

**Example — ephemeral workspace (auto-created)**
```json
{
  "command": "init"
}
```

**Response** `202 Accepted`
```json
{
  "ok": true,
  "task_id": "7c9e6679-7425-40de-944b-e07fc1f90ae7",
  "workspace_id": "3fa85f64-5717-4562-b3fc-2c963f66afa6"
}
```

> If `workspace_id` is omitted, CLK automatically runs `init` before the
> requested command (unless the command itself is `init`).

---

### `GET /api/research/{task_id}`

Poll task status.

**Response**
```json
{
  "ok": true,
  "task_id": "7c9e6679-...",
  "workspace_id": "3fa85f64-...",
  "command": "idea",
  "status": "running",
  "started_at": "2024-01-15T12:00:05.123456Z",
  "finished_at": null,
  "exit_code": null,
  "line_count": 42
}
```

`status` values:

| Value | Meaning |
|---|---|
| `pending` | Task accepted, not yet started. |
| `running` | Subprocess is active. |
| `done` | Subprocess exited with code 0. |
| `failed` | Subprocess exited with non-zero code. |
| `cancelled` | Task was cancelled via `POST /cancel`. |

---

### `GET /api/research/{task_id}/stream`

Server-Sent Events (SSE) stream of task output.  Connect with
`EventSource` in a browser or `curl -N` from the shell.

**Media type:** `text/event-stream`

**Events during execution:**
```
data: {"line": "CLK initialized.", "seq": 0}

data: {"line": "  project_root: /workspaces/3fa85f64-...", "seq": 1}

...
```

**Terminal event (always sent last):**
```
data: {"status": "done", "exit_code": 0}

```

The stream closes after the terminal event.  If the client disconnects
early the server stops generating events.

---

### `GET /api/research/{task_id}/artifacts`

List all files in the workspace after (or during) a task run.

**Response**
```json
{
  "ok": true,
  "task_id": "7c9e6679-...",
  "artifacts": [
    {
      "path": ".clk/state/idea.json",
      "size": 234,
      "modified": "2024-01-15T12:00:10.000000Z"
    }
  ]
}
```

---

### `GET /api/research/{task_id}/artifacts/{path}`

Download a single artifact by its relative path within the workspace.
Paths that escape the workspace boundary are rejected with `403 Forbidden`.

---

### `POST /api/research/{task_id}/cancel`

Send `SIGTERM` to the running subprocess and mark the task cancelled.
Has no effect on tasks that are already `done`, `failed`, or `cancelled`.

**Response**
```json
{ "ok": true }
```

---

## Complete workflow example

```bash
BASE=http://localhost:8001

# 1. Create a workspace
WS=$(curl -s -X POST $BASE/api/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"name": "journal-app"}' | python3 -c "import sys,json; print(json.load(sys.stdin)['workspace_id'])")

echo "Workspace: $WS"

# 2. Capture an idea
TASK=$(curl -s -X POST $BASE/api/research \
  -H 'Content-Type: application/json' \
  -d "{\"command\":\"idea\",\"args\":[\"A local-first journaling app\"],\"workspace_id\":\"$WS\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

echo "Task: $TASK"

# 3. Stream output
curl -sN $BASE/api/research/$TASK/stream

# 4. Check status
curl -s $BASE/api/research/$TASK | python3 -m json.tool

# 5. List artifacts
curl -s $BASE/api/research/$TASK/artifacts | python3 -m json.tool

# 6. Download a specific artifact
curl -s $BASE/api/research/$TASK/artifacts/.clk/state/idea.json

# 7. Run a development cycle
TASK2=$(curl -s -X POST $BASE/api/research \
  -H 'Content-Type: application/json' \
  -d "{\"command\":\"run\",\"workflow\":\"engineering\",\"workspace_id\":\"$WS\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

curl -sN $BASE/api/research/$TASK2/stream
```

## Docker

See the main [README](../README.md#rest-api) for Docker-specific instructions.
