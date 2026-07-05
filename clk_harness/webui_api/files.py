"""Web-UI workspace file + git-history endpoints.
"""

from __future__ import annotations

import asyncio
import os
import re
import zipfile
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import Query
from fastapi.responses import FileResponse

from ..log import get_logger
from .router import (
    _HIDDEN_DIRS,
    _MAX_FILE_BYTES,
    _MAX_FILES,
    FileWrite,
    _api,
    _is_probably_binary,
    _require_workspace,
    _safe_unlink,
    _safe_ws_file,
    router,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Workspace files: list / read / write + the "follow up with the agents" idea
# ---------------------------------------------------------------------------

@router.get("/api/workspaces/{workspace_id}/files")
async def list_files(workspace_id: str) -> Dict[str, Any]:
    """List the files the agents have produced in the workspace.

    Harness-internal directories (``.clk``, ``.git``, ``node_modules`` …) are
    skipped so this reflects the user-facing deliverables, not bookkeeping.
    """
    from datetime import datetime, timezone

    paths = _require_workspace(workspace_id)
    root = paths.root.resolve()
    files: List[Dict[str, Any]] = []
    truncated = False
    if root.exists():
        for dirpath, dirnames, filenames in os.walk(root):
            # Prune hidden/internal dirs in place so os.walk doesn't descend.
            dirnames[:] = sorted(d for d in dirnames if d not in _HIDDEN_DIRS)
            for name in sorted(filenames):
                if len(files) >= _MAX_FILES:
                    truncated = True
                    break
                fp = Path(dirpath) / name
                # Skip symlinks: following them with stat() could leak
                # size/mtime for targets outside the workspace.
                if fp.is_symlink():
                    continue
                try:
                    stat = fp.stat()
                except OSError:
                    continue
                files.append({
                    "path": str(fp.relative_to(root)),
                    "size": stat.st_size,
                    "modified": datetime.fromtimestamp(stat.st_mtime, timezone.utc)
                        .isoformat().replace("+00:00", "Z"),
                })
            if truncated:
                break
    files.sort(key=lambda f: f["path"])
    return {"ok": True, "files": files, "count": len(files), "truncated": truncated}


@router.get("/api/workspaces/{workspace_id}/download")
async def download_workspace(workspace_id: str) -> FileResponse:
    """Download the workspace's deliverables as a zip.

    Mirrors the file listing: harness-internal directories (``.clk``,
    ``.git``, ``node_modules`` …) and symlinks are excluded so the archive
    is just the user-facing files the agents produced.
    """
    paths = _require_workspace(workspace_id)
    root = paths.root.resolve()

    def _build_zip_to_tempfile() -> str:
        # Build to a temp file on disk so we stream it back in chunks rather
        # than holding the whole archive in memory.
        import tempfile
        fd, tmp_path = tempfile.mkstemp(suffix=".zip", prefix="clk-ws-")
        os.close(fd)
        # If zipping fails (e.g. a permission error while walking/writing) the
        # FileResponse -- and its cleanup BackgroundTask -- is never created, so
        # remove the temp file here before re-raising to avoid leaking it.
        try:
            with zipfile.ZipFile(tmp_path, "w", zipfile.ZIP_DEFLATED) as zf:
                if root.exists():
                    for dirpath, dirnames, filenames in os.walk(root):
                        dirnames[:] = [d for d in dirnames if d not in _HIDDEN_DIRS]
                        for name in filenames:
                            fp = Path(dirpath) / name
                            if fp.is_symlink():
                                continue
                            try:
                                zf.write(fp, fp.relative_to(root).as_posix())
                            except OSError:
                                continue
        except BaseException:
            _safe_unlink(tmp_path)
            raise
        return tmp_path

    tmp_path = await asyncio.to_thread(_build_zip_to_tempfile)
    entry = _api().WORKSPACES.get(workspace_id) or {}
    raw_name = str(entry.get("name") or workspace_id[:8])
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", raw_name).strip("-") or "workspace"
    from starlette.background import BackgroundTask
    return FileResponse(
        tmp_path,
        media_type="application/zip",
        filename=f"{safe}.zip",
        background=BackgroundTask(lambda: _safe_unlink(tmp_path)),
    )


@router.get("/api/workspaces/{workspace_id}/file")
async def read_file(workspace_id: str, path: str = Query(...)) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    target = _safe_ws_file(paths, path)
    if not target.exists() or not target.is_file():
        raise _api()._err("file_not_found", f"File {path!r} not found.", 404)
    raw = target.read_bytes()
    too_big = len(raw) > _MAX_FILE_BYTES
    chunk = raw[:_MAX_FILE_BYTES]
    if _is_probably_binary(chunk):
        return {"ok": True, "path": path, "binary": True, "size": len(raw)}
    return {
        "ok": True,
        "path": path,
        "binary": False,
        "size": len(raw),
        "truncated": too_big,
        "content": chunk.decode("utf-8", errors="replace"),
    }


@router.put("/api/workspaces/{workspace_id}/file")
async def write_file(workspace_id: str, body: FileWrite) -> Dict[str, Any]:
    paths = _require_workspace(workspace_id)
    paths.root.mkdir(parents=True, exist_ok=True)
    target = _safe_ws_file(paths, body.path, for_write=True)
    if body.path.endswith(("/", "\\")) or (target.exists() and target.is_dir()):
        raise _api()._err("invalid_path", f"{body.path!r} is a directory, not a file.", 400)
    data = body.content.encode("utf-8")
    if len(data) > _MAX_FILE_BYTES:
        raise _api()._err("too_large", "File exceeds the 1 MB editor limit.", 413)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body.content, encoding="utf-8")
    return {"ok": True, "path": body.path, "size": len(data)}


# ---------------------------------------------------------------------------
# Git history: the Files tab's "how did these files evolve" view
# ---------------------------------------------------------------------------

_SHA_RE = re.compile(r"^[0-9a-fA-F]{4,40}$")


def _require_sha(sha: str) -> str:
    if not _SHA_RE.match(sha or ""):
        raise _api()._err("invalid_sha", "A hex commit sha is required.", 400)
    return sha


@router.get("/api/workspaces/{workspace_id}/git/log")
async def git_log(
    workspace_id: str,
    path: Optional[str] = Query(None),
    limit: int = Query(100, ge=1, le=500),
) -> Dict[str, Any]:
    """Commit history (newest first) for the workspace, or for one file.

    Harness-internal paths (.clk, .git, …) are filtered out so the
    history mirrors what the Files tab lists.
    """
    from .. import git_ops

    paths = _require_workspace(workspace_id)
    if path:
        _safe_ws_file(paths, path)  # traversal + hidden-dir guard
    commits = await asyncio.to_thread(
        git_ops.log_entries, paths.root, path=path, limit=limit
    )
    return {"ok": True, "commits": commits, "count": len(commits)}


@router.get("/api/workspaces/{workspace_id}/git/commit/{sha}")
async def git_commit_detail(workspace_id: str, sha: str) -> Dict[str, Any]:
    """One commit's metadata + unified diff (internal paths excluded)."""
    from .. import git_ops

    paths = _require_workspace(workspace_id)
    _require_sha(sha)
    commits = await asyncio.to_thread(
        git_ops.log_entries, paths.root, limit=1, rev=sha
    )
    meta = commits[0] if commits else None
    patch = await asyncio.to_thread(git_ops.commit_patch, paths.root, sha)
    if meta is None and patch is None:
        raise _api()._err("commit_not_found", f"Commit {sha!r} not found.", 404)
    return {
        "ok": True,
        "commit": meta,
        "patch": (patch or {}).get("patch", ""),
        "patch_truncated": bool((patch or {}).get("truncated")),
    }


@router.get("/api/workspaces/{workspace_id}/git/status")
async def git_status(workspace_id: str) -> Dict[str, Any]:
    """Uncommitted working-tree changes vs HEAD (the Files tab's
    "not yet committed" view)."""
    from .. import git_ops

    paths = _require_workspace(workspace_id)
    files = await asyncio.to_thread(git_ops.status_entries, paths.root)
    return {"ok": True, "dirty": bool(files), "files": files, "count": len(files)}


@router.get("/api/workspaces/{workspace_id}/git/diff")
async def git_working_diff(workspace_id: str) -> Dict[str, Any]:
    """Unified diff of uncommitted changes vs HEAD. Untracked files don't
    appear in the patch — pair with /git/status to list them."""
    from .. import git_ops

    paths = _require_workspace(workspace_id)
    patch = await asyncio.to_thread(git_ops.working_tree_patch, paths.root)
    return {
        "ok": True,
        "patch": (patch or {}).get("patch", ""),
        "truncated": bool((patch or {}).get("truncated")),
    }


@router.get("/api/workspaces/{workspace_id}/git/file")
async def git_file_at(
    workspace_id: str, sha: str = Query(...), path: str = Query(...)
) -> Dict[str, Any]:
    """File content as of a specific commit (read-only time travel)."""
    from .. import git_ops

    paths = _require_workspace(workspace_id)
    _require_sha(sha)
    _safe_ws_file(paths, path)  # traversal + hidden-dir guard
    raw = await asyncio.to_thread(git_ops.file_at, paths.root, sha, path)
    if raw is None:
        raise _api()._err(
            "file_not_found", f"File {path!r} not found at commit {sha[:12]}.", 404
        )
    too_big = len(raw) > _MAX_FILE_BYTES
    chunk = raw[:_MAX_FILE_BYTES]
    if _is_probably_binary(chunk):
        return {"ok": True, "path": path, "sha": sha, "binary": True, "size": len(raw)}
    return {
        "ok": True,
        "path": path,
        "sha": sha,
        "binary": False,
        "size": len(raw),
        "truncated": too_big,
        "content": chunk.decode("utf-8", errors="replace"),
    }
