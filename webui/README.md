# CLK Web UI

The browser dashboard for the Cognitive Loop Kernel — a Vite + React +
TypeScript single-page app served by CLK's FastAPI server
(`clk_harness/api.py` + `clk_harness/webui_router.py`).

## Quick start

From the repo root, the easiest path is the CLI:

```bash
clk web --build   # builds this app, then serves it + opens a browser
```

## Develop with hot reload

```bash
# Terminal 1 — run the API the SPA talks to.
clk web --no-open            # serves on http://127.0.0.1:8001

# Terminal 2 — Vite dev server with HMR (proxies /api -> :8001).
npm install
npm run dev                  # http://localhost:5173
```

## Scripts

| Command         | What it does                                              |
|-----------------|-----------------------------------------------------------|
| `npm run dev`   | Vite dev server (HMR), proxies `/api` to the FastAPI app. |
| `npm run build` | Type-check + build to `../clk_harness/webui_dist`.        |
| `npm test`      | Vitest unit tests.                                        |

## How it talks to the backend

- `src/api/client.ts` — fetch wrapper for the `{ok, error}` envelope.
- `src/api/hooks.ts` — TanStack Query hooks for every endpoint.
- `src/api/useEventStream.ts` — SSE follower for the live activity feed
  (`/api/workspaces/{id}/activity/stream`) with auto-reconnect.
- `src/state/activity.tsx` — shares one SSE connection app-wide.

The build output (`clk_harness/webui_dist/`) is git-ignored and ships in
the wheel via `package-data`; CI builds it before packaging.
