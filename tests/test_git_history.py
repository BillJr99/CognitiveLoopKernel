"""Git history mining for the Files tab: git_ops helpers + REST endpoints.

The Files tab's History view is read-only time travel over the commits the
harness makes as agents work. These tests pin the log/patch/file-at-commit
helpers (internal-path filtering included) and the three API endpoints.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import AsyncIterator

import pytest
import pytest_asyncio

os.environ.setdefault("CLK_WORKSPACES_DIR", "/tmp/clk-workspaces-webui-test")

from httpx import ASGITransport, AsyncClient  # noqa: E402

from clk_harness import git_ops  # noqa: E402
from clk_harness.api import app  # noqa: E402


def _init_repo(root: Path) -> None:
    import subprocess

    assert git_ops.init_repo(root)
    # CI/dev machines may force commit signing globally; tests must not
    # depend on a signing setup.
    subprocess.run(
        ["git", "config", "commit.gpgsign", "false"],
        cwd=root, check=True, capture_output=True,
    )


def _commit_all(root: Path, agent: str, objective: str) -> str:
    git_ops.add_all(root)
    assert git_ops.commit(root, agent=agent, objective=objective)
    sha = git_ops.head_sha(root)
    assert sha
    return sha


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _init_repo(tmp_path)
    (tmp_path / "README.md").write_text("v1\n", encoding="utf-8")
    (tmp_path / ".clk").mkdir()
    (tmp_path / ".clk" / "state.json").write_text("{}\n", encoding="utf-8")
    _commit_all(tmp_path, "engineer", "initial slice")
    (tmp_path / "README.md").write_text("v1\nv2\n", encoding="utf-8")
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    _commit_all(tmp_path, "ralph", "refine readme and add main")
    return tmp_path


# ---------------------------------------------------------------------------
# git_ops unit tests
# ---------------------------------------------------------------------------

def test_log_entries_shape_and_order(repo: Path) -> None:
    commits = git_ops.log_entries(repo)
    assert len(commits) == 2
    newest, oldest = commits
    assert newest["subject"] == "[ralph] refine readme and add main"
    assert oldest["subject"] == "[engineer] initial slice"
    for c in commits:
        assert set(c) >= {"sha", "short", "author", "date", "subject",
                          "insertions", "deletions", "files"}
    paths = {f["path"] for f in newest["files"]}
    assert paths == {"README.md", "src/main.py"}
    assert newest["insertions"] == 2


def test_log_entries_filters_internal_paths(repo: Path) -> None:
    commits = git_ops.log_entries(repo)
    oldest = commits[-1]
    paths = {f["path"] for f in oldest["files"]}
    assert ".clk/state.json" not in paths
    assert "README.md" in paths


def test_log_entries_internal_only_commit_dropped(repo: Path) -> None:
    (repo / ".clk" / "state.json").write_text('{"x": 1}\n', encoding="utf-8")
    _commit_all(repo, "chief", "bookkeeping only")
    commits = git_ops.log_entries(repo)
    assert all(c["subject"] != "[chief] bookkeeping only" for c in commits)


def test_log_entries_path_filter(repo: Path) -> None:
    commits = git_ops.log_entries(repo, path="src/main.py")
    assert len(commits) == 1
    assert commits[0]["subject"].startswith("[ralph]")


def test_log_entries_rev_pins_start(repo: Path) -> None:
    all_commits = git_ops.log_entries(repo)
    old_sha = all_commits[-1]["sha"]
    commits = git_ops.log_entries(repo, rev=old_sha, limit=1)
    assert len(commits) == 1
    assert commits[0]["sha"] == old_sha


def test_file_at_returns_old_version(repo: Path) -> None:
    old_sha = git_ops.log_entries(repo)[-1]["sha"]
    assert git_ops.file_at(repo, old_sha, "README.md") == b"v1\n"
    assert git_ops.file_at(repo, "HEAD", "README.md") == b"v1\nv2\n"
    assert git_ops.file_at(repo, old_sha, "src/main.py") is None  # not yet created


def test_commit_patch_contains_diff(repo: Path) -> None:
    head = git_ops.head_sha(repo)
    assert head
    detail = git_ops.commit_patch(repo, head)
    assert detail is not None
    assert "+v2" in detail["patch"]
    assert detail["truncated"] is False


def test_non_repo_returns_empty(tmp_path: Path) -> None:
    assert git_ops.log_entries(tmp_path) == []
    assert git_ops.file_at(tmp_path, "HEAD", "x") is None
    assert git_ops.commit_patch(tmp_path, "HEAD") is None
    assert git_ops.status_entries(tmp_path) == []
    assert git_ops.working_tree_patch(tmp_path) is None


def test_status_entries_classify_changes(repo: Path) -> None:
    (repo / "README.md").write_text("v1\nv2\nv3\n", encoding="utf-8")  # modified
    (repo / "brand_new.md").write_text("hello\n", encoding="utf-8")  # untracked
    (repo / "src" / "main.py").unlink()  # deleted
    (repo / ".clk" / "noise.json").write_text("{}\n", encoding="utf-8")  # internal
    entries = {e["path"]: e["state"] for e in git_ops.status_entries(repo)}
    assert entries["README.md"] == "modified"
    assert entries["brand_new.md"] == "new"
    assert entries["src/main.py"] == "deleted"
    assert not any(p.startswith(".clk") for p in entries)


def test_status_entries_clean_tree_empty(repo: Path) -> None:
    assert git_ops.status_entries(repo) == []


def test_working_tree_patch_shows_uncommitted_edit(repo: Path) -> None:
    (repo / "README.md").write_text("v1\nv2\nuncommitted\n", encoding="utf-8")
    patch = git_ops.working_tree_patch(repo)
    assert patch is not None
    assert "+uncommitted" in patch["patch"]


def test_snapshot_rollback_preserves_head(repo: Path) -> None:
    import subprocess

    head = git_ops.head_sha(repo)
    assert git_ops.snapshot_rollback(repo, "build_stage") is True
    refs = subprocess.run(
        ["git", "for-each-ref", "refs/clk/rollbacks", "--format=%(objectname)"],
        cwd=repo, capture_output=True, text=True, check=True,
    ).stdout.split()
    assert head in refs


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@pytest_asyncio.fixture
async def client() -> AsyncIterator[AsyncClient]:
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as c:
        yield c


async def _ws_with_history(client: AsyncClient) -> tuple[str, Path]:
    from clk_harness import api

    r = await client.post("/api/workspaces", json={"name": "git-hist"})
    assert r.status_code == 201, r.text
    ws_id = r.json()["workspace_id"]
    root = api._workspace_path(ws_id)
    _init_repo(root)
    (root / "notes.md").write_text("first\n", encoding="utf-8")
    _commit_all(root, "engineer", "write notes")
    (root / "notes.md").write_text("first\nsecond\n", encoding="utf-8")
    _commit_all(root, "ralph", "expand notes")
    return ws_id, root


@pytest.mark.asyncio
async def test_git_log_endpoint(client: AsyncClient) -> None:
    ws_id, _ = await _ws_with_history(client)
    r = await client.get(f"/api/workspaces/{ws_id}/git/log")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert body["count"] == 2
    assert body["commits"][0]["subject"] == "[ralph] expand notes"


@pytest.mark.asyncio
async def test_git_log_endpoint_path_filter(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    (root / "other.md").write_text("x\n", encoding="utf-8")
    _commit_all(root, "qa", "add other")
    r = await client.get(f"/api/workspaces/{ws_id}/git/log", params={"path": "other.md"})
    assert r.json()["count"] == 1


@pytest.mark.asyncio
async def test_git_commit_detail_endpoint(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    head = git_ops.head_sha(root)
    r = await client.get(f"/api/workspaces/{ws_id}/git/commit/{head}")
    assert r.status_code == 200
    body = r.json()
    assert body["commit"]["subject"] == "[ralph] expand notes"
    assert "+second" in body["patch"]


@pytest.mark.asyncio
async def test_git_commit_detail_rejects_bad_sha(client: AsyncClient) -> None:
    ws_id, _ = await _ws_with_history(client)
    r = await client.get(f"/api/workspaces/{ws_id}/git/commit/not-a-sha")
    assert r.status_code == 400


@pytest.mark.asyncio
async def test_git_file_at_endpoint(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    old = git_ops.log_entries(root)[-1]["sha"]
    r = await client.get(
        f"/api/workspaces/{ws_id}/git/file", params={"sha": old, "path": "notes.md"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["content"] == "first\n"
    assert body["binary"] is False


@pytest.mark.asyncio
async def test_git_file_at_blocks_internal_paths(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    head = git_ops.head_sha(root)
    r = await client.get(
        f"/api/workspaces/{ws_id}/git/file",
        params={"sha": head, "path": ".clk/config/clk.config.json"},
    )
    assert r.status_code == 403


@pytest.mark.asyncio
async def test_git_file_at_missing_file_404(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    head = git_ops.head_sha(root)
    r = await client.get(
        f"/api/workspaces/{ws_id}/git/file", params={"sha": head, "path": "nope.md"}
    )
    assert r.status_code == 404


@pytest.mark.asyncio
async def test_git_status_endpoint(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    r = await client.get(f"/api/workspaces/{ws_id}/git/status")
    assert r.status_code == 200
    assert r.json()["dirty"] is False
    (root / "wip.md").write_text("draft\n", encoding="utf-8")
    body = (await client.get(f"/api/workspaces/{ws_id}/git/status")).json()
    assert body["dirty"] is True
    assert {"path": "wip.md", "state": "new"} in body["files"]


@pytest.mark.asyncio
async def test_git_diff_endpoint(client: AsyncClient) -> None:
    ws_id, root = await _ws_with_history(client)
    (root / "notes.md").write_text("first\nsecond\nthird\n", encoding="utf-8")
    r = await client.get(f"/api/workspaces/{ws_id}/git/diff")
    assert r.status_code == 200
    assert "+third" in r.json()["patch"]
