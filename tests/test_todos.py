"""TODOS module: parsing, mutable per-author persistence, rendering."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration import todos as td


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def test_parse_grammar_all_three_states() -> None:
    items = td.parse_todos_blocks(
        "noise before\n"
        "TODOS:\n"
        "- [ ] not started\n"
        "- [~] in progress\n"
        "- [x] done lower\n"
        "- [X] done upper\n"
        "END_TODOS\n"
        "noise after"
    )
    assert items == [
        {"status": "todo", "text": "not started"},
        {"status": "doing", "text": "in progress"},
        {"status": "done", "text": "done lower"},
        {"status": "done", "text": "done upper"},
    ]


def test_parse_no_block_returns_none() -> None:
    assert td.parse_todos_blocks("just some prose, no checklist") is None
    # A bare TODOS: mention without a closing END_TODOS is not a complete block.
    assert td.parse_todos_blocks("TODOS: inline mention") is None


def test_parse_multiple_blocks_last_wins() -> None:
    """The agent re-emits its full checklist each turn; the last complete
    block overwrites earlier ones."""
    items = td.parse_todos_blocks(
        "TODOS:\n- [ ] first draft\nEND_TODOS\n"
        "then reconsidered\n"
        "TODOS:\n- [x] final a\n- [~] final b\nEND_TODOS\n"
    )
    assert items == [
        {"status": "done", "text": "final a"},
        {"status": "doing", "text": "final b"},
    ]


def test_parse_empty_block_is_intentional_clear() -> None:
    """A complete but empty block clears the list (returns [], not None)."""
    assert td.parse_todos_blocks("TODOS:\nEND_TODOS") == []


def test_parse_ignores_non_item_lines_in_block() -> None:
    items = td.parse_todos_blocks(
        "TODOS:\n- [ ] keep\nrandom commentary line\n* [x] star bullet\nEND_TODOS"
    )
    assert items == [
        {"status": "todo", "text": "keep"},
        {"status": "done", "text": "star bullet"},
    ]


# ---------------------------------------------------------------------------
# Persistence (mutable, per-author, last-write-wins)
# ---------------------------------------------------------------------------


def test_apply_persists_to_state_json(paths: Paths) -> None:
    tl = td.apply_todos_blocks(
        paths,
        "TODOS:\n- [ ] a\n- [~] b\nEND_TODOS",
        author="engineer",
        stage_id="build_a",
        workflow="engineering",
    )
    assert tl is not None
    store_file = paths.state / "todos.json"
    assert store_file.exists()
    raw = json.loads(store_file.read_text(encoding="utf-8"))
    assert set(raw["authors"].keys()) == {"engineer"}
    entry = raw["authors"]["engineer"]
    assert entry["stage_id"] == "build_a"
    assert [i["text"] for i in entry["items"]] == ["a", "b"]


def test_apply_no_block_is_noop(paths: Paths) -> None:
    assert td.apply_todos_blocks(paths, "no checklist here", author="qa") is None
    assert not (paths.state / "todos.json").exists()


def test_apply_overwrites_same_author(paths: Paths) -> None:
    td.apply_todos_blocks(
        paths, "TODOS:\n- [ ] one\n- [ ] two\n- [ ] three\nEND_TODOS", author="engineer"
    )
    # Re-emit a shorter, updated list — it fully replaces the prior one.
    td.apply_todos_blocks(
        paths, "TODOS:\n- [x] one\nEND_TODOS", author="engineer"
    )
    tl = td.todos_for(paths, "engineer")
    assert tl is not None
    assert [(i.status, i.text) for i in tl.items] == [("done", "one")]


def test_apply_preserves_other_authors(paths: Paths) -> None:
    td.apply_todos_blocks(paths, "TODOS:\n- [ ] eng task\nEND_TODOS", author="engineer")
    td.apply_todos_blocks(paths, "TODOS:\n- [~] qa task\nEND_TODOS", author="qa")
    # Engineer re-emits — must not clobber qa's slot.
    td.apply_todos_blocks(paths, "TODOS:\n- [x] eng task\nEND_TODOS", author="engineer")

    raw = json.loads((paths.state / "todos.json").read_text(encoding="utf-8"))
    assert set(raw["authors"].keys()) == {"engineer", "qa"}
    eng = td.todos_for(paths, "engineer")
    qa = td.todos_for(paths, "qa")
    assert [(i.status, i.text) for i in eng.items] == [("done", "eng task")]
    assert [(i.status, i.text) for i in qa.items] == [("doing", "qa task")]


def test_todos_for_unknown_author_returns_none(paths: Paths) -> None:
    assert td.todos_for(paths, "nobody") is None
    assert td.todos_for(paths, "") is None


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_render_empty() -> None:
    assert td.render_todos(None) == "(no todos yet)"
    assert td.render_todos(td.TodoList(author="x", items=[])) == "(no todos yet)"


def test_render_non_empty_marks_and_counts(paths: Paths) -> None:
    td.apply_todos_blocks(
        paths, "TODOS:\n- [ ] a\n- [~] b\n- [x] c\nEND_TODOS", author="engineer"
    )
    out = td.render_todos(td.todos_for(paths, "engineer"))
    assert "2 open, 1 done" in out
    assert "- [ ] a" in out
    assert "- [~] b" in out
    assert "- [x] c" in out
