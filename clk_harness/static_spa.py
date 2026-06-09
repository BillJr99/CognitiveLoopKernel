"""Serve the built React single-page app from the FastAPI server.

The Vite build writes hashed assets to ``clk_harness/webui_dist/`` (see
``webui/vite.config.ts``). We mount ``/assets`` for those files and add a
catch-all that returns ``index.html`` for any non-``/api`` path so
client-side routing works. If the build output is absent (e.g. a fresh
checkout that hasn't run ``npm run build``), we serve a friendly page
explaining how to build it — the JSON API stays fully functional either
way.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse
from starlette.staticfiles import StaticFiles

DIST_DIR = Path(__file__).resolve().parent / "webui_dist"

_NOT_BUILT_HTML = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>CLK Web UI — not built</title>
<style>
  body{font-family:system-ui,-apple-system,Segoe UI,Roboto,sans-serif;
    background:#0b1020;color:#e7ecff;display:grid;place-items:center;
    min-height:100vh;margin:0}
  .card{max-width:640px;padding:2.5rem;background:#141a32;border:1px solid #263056;
    border-radius:16px;box-shadow:0 20px 60px rgba(0,0,0,.4)}
  h1{margin:0 0 .5rem;font-size:1.5rem}
  code{background:#0b1020;padding:.15rem .4rem;border-radius:6px;
    border:1px solid #263056;font-size:.9em}
  pre{background:#0b1020;padding:1rem;border-radius:10px;border:1px solid #263056;
    overflow:auto}
  a{color:#7aa2ff}
  .muted{color:#9fb0d9}
</style></head>
<body><div class="card">
  <h1>🧠 CLK Web UI isn't built yet</h1>
  <p class="muted">The REST API is running, but the front-end bundle hasn't been
  compiled. Build it once with either:</p>
  <pre>clk web --build
# or
npm --prefix webui ci &amp;&amp; npm --prefix webui run build</pre>
  <p class="muted">Then reload this page. The JSON API at
  <code>/api/*</code> works right now regardless.</p>
</div></body></html>
"""


def spa_available() -> bool:
    return (DIST_DIR / "index.html").exists()


def mount_spa(app: FastAPI) -> None:
    """Mount the SPA (or a graceful placeholder) onto ``app``.

    Must be called AFTER all ``/api`` routes/routers are registered so
    the catch-all does not shadow them.
    """
    assets_dir = DIST_DIR / "assets"
    if assets_dir.is_dir():
        app.mount("/assets", StaticFiles(directory=str(assets_dir)), name="assets")

    @app.get("/", include_in_schema=False)
    async def _index():  # noqa: ANN202
        if spa_available():
            return FileResponse(str(DIST_DIR / "index.html"))
        return HTMLResponse(_NOT_BUILT_HTML, status_code=200)

    @app.get("/{full_path:path}", include_in_schema=False)
    async def _spa_catch_all(full_path: str):  # noqa: ANN202
        # Never swallow the API surface — let it 404 through the normal
        # envelope handler instead of returning index.html.
        if full_path.startswith("api/") or full_path.startswith("assets/"):
            from .api import _err
            raise _err("not_found", f"No route for /{full_path}", 404)
        if spa_available():
            # Serve a real file if it exists (favicon, etc.), else the SPA shell.
            candidate = (DIST_DIR / full_path).resolve()
            try:
                candidate.relative_to(DIST_DIR.resolve())
            except ValueError:
                candidate = DIST_DIR / "index.html"
            if candidate.is_file():
                return FileResponse(str(candidate))
            return FileResponse(str(DIST_DIR / "index.html"))
        return HTMLResponse(_NOT_BUILT_HTML, status_code=200)


__all__ = ["mount_spa", "spa_available", "DIST_DIR"]
