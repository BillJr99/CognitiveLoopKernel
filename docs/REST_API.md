# CLK REST API Reference

The CLK REST API is a thin FastAPI wrapper around the CLK CLI.  It lets you
start, monitor, and cancel research tasks (using the commands `init`, `idea`,
`plan`, `run`, `loop`, and `status` — see `/api/capabilities` for the
authoritative list); manage isolated workspaces; and stream live output from
running tasks — all over plain HTTP.

## Quick start

```bash
# 1. Install API dependencies
pip install "clk-harness[api]"

# 2. Start the server (default port 8001)
clk-api
# or via the module entry point
python -m clk_harness.api
# or via uvicorn directly
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
| `CLK_WORKSPACES_DIR` | `/workspaces` | Root directory under which workspaces are created. Mount a volume here so workspace *directories* persist across container restarts (note: the in-memory workspace registry is not persisted — see workspace notes below). |
| `CLK_API_PORT` | `8001` | TCP port the server binds to when launched as `clk-api` or `python -m clk_harness.api`. |

## Authentication

The API has **no authentication**.  It relies on network-boundary trust.
Do not expose it to the public internet without a reverse-proxy or firewall.

## Response envelope

Most endpoints return JSON.  Successful responses always include `"ok": true`;
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

**Exceptions:** `GET /api/research/{task_id}/stream` returns `text/event-stream`
(SSE — see the stream endpoint section for the event format) and
`GET /api/research/{task_id}/artifacts/{path}` returns the raw file content with
the file's natural MIME type.

---

## Endpoints

### `GET /api/healthz`

Liveness check.

**Response**
```json
{
  "ok": true,
  "version": "0.1.0",
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

If the template package cannot be loaded the response will be HTTP 500 with
`{"ok": false, "error": {"code": "template_load_failed", "message": "Failed to load workflow templates."}}`.

---

### `POST /api/workspaces`

Create a named workspace directory.  Every call allocates a **brand-new UUID**
and a new directory on disk, even if the same `name` has been used before.

> **Important — workspace registry is in-memory only.**
> The `WORKSPACES` mapping lives entirely in the server process and is lost
> on every restart.  Workspace *directories* on disk survive a restart, but
> they are **not automatically re-registered** — there is currently no
> "recover/import existing directory" mechanism.
>
> Practical consequences:
> - After a server restart, previously-created workspace directories on disk
>   are **not addressable through the API**.  Any stored workspace UUID or
>   task ID from a previous server session is no longer valid.
> - Posting to `/api/workspaces` again (with any name) creates a completely
>   new workspace with a new UUID — it does **not** reconnect to an existing
>   directory.

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
> restarts. Workspace directories on disk survive a restart, but they are not
> re-registered automatically. See `POST /api/workspaces` for details.

---

### `DELETE /api/workspaces/{workspace_id}`

Delete a workspace and all its files.

Returns `409 Conflict` if any task using this workspace is currently `pending`
or `running`. Cancel all active tasks before deleting the workspace.

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
| `workflow` | string | no | Convenience shortcut: injects `--workflow <value>` when `command` is `run`. Ignored if `--workflow` or `--workflow=<value>` is already in `args`. |

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

> CLK automatically runs `init` before the requested command whenever the
> workspace has not yet been initialised (i.e. `.clk/` is absent), regardless
> of whether `workspace_id` was provided.  The auto-`init` step is skipped
> only when the requested command is already `init`.

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
  "created_at": "2024-01-15T12:00:04.000000Z",
  "started_at": "2024-01-15T12:00:05.123456Z",
  "finished_at": null,
  "exit_code": null,
  "line_count": 42
}
```

`status` values:

| Value | Meaning |
|---|---|
| `pending` | Task accepted, not yet started (`started_at` is null). |
| `running` | Subprocess is active. |
| `done` | Subprocess exited with code 0. |
| `failed` | Subprocess exited with non-zero code. |
| `cancelled` | Task was cancelled via `POST /cancel`. |

Timestamp fields:

| Field | Description |
|---|---|
| `created_at` | ISO-8601 UTC timestamp set when the task is accepted (always present). |
| `started_at` | ISO-8601 UTC timestamp set when the subprocess begins executing; `null` while status is `pending`. |
| `finished_at` | ISO-8601 UTC timestamp set when the task reaches a terminal state; `null` while `pending` or `running`. |

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
The response body is the raw file content; the `Content-Type` is inferred
from the file extension.

---

### `POST /api/research/{task_id}/cancel`

Send `SIGTERM` to the running subprocess and mark the task cancelled.
Has no effect on tasks that are already `done`, `failed`, or `cancelled`.

**Response**
```json
{ "ok": true }
```

### Workspace files & git history

The web console's Files tab is backed by these endpoints (the full
web-UI surface — config, providers, doctor, activity stream — lives in
`clk_harness/webui_router.py`; the highlights):

| Endpoint | Purpose |
|---|---|
| `GET /api/workspaces/{id}/files` | List the workspace's files (live from disk; `.clk`, `.git`, `node_modules` etc. excluded). |
| `GET /api/workspaces/{id}/file?path=` | Read one file (binary detection, 1 MB truncation flag). |
| `PUT /api/workspaces/{id}/file` | Write one file (`{path, content}`). |
| `GET /api/workspaces/{id}/download` | Download the workspace deliverables as a zip. |
| `GET /api/workspaces/{id}/git/log?path=&limit=` | Commit history, newest first, with per-commit file stats; `path` filters to one file (renames followed). Harness-internal paths are excluded. |
| `GET /api/workspaces/{id}/git/commit/{sha}` | One commit's metadata plus its unified diff (truncated past ~200 KB). |
| `GET /api/workspaces/{id}/git/file?sha=&path=` | A file's content as of a specific commit — read-only time travel. |
| `GET /api/workspaces/{id}/git/status` | Uncommitted working-tree changes vs HEAD as `{path, state}` entries (`new` / `modified` / `deleted` / `renamed`). |
| `GET /api/workspaces/{id}/git/diff` | Unified diff of uncommitted changes vs HEAD (untracked files appear in `/git/status`, not in the patch). |
| `GET /api/workspaces/{id}/logs?tail=` | Tail the harness session logs (`.clk/logs/*.log`) — bounded reads, safe to poll. |

All of them apply the same traversal and hidden-dir guards as the file
editor, and commit SHAs are validated as hex before reaching git.

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

## Telegram bot

The `clk-telegram-bot` console script consumes this API: `/api/workspaces`,
`/api/research`, `/api/research/{id}`, `/api/research/{id}/cancel`,
`/api/research/{id}/stream`, and `/api/research/{id}/artifacts`. See
[README → Telegram Bot](../README.md#telegram-bot) for setup, the wizard,
and the chat command reference.
