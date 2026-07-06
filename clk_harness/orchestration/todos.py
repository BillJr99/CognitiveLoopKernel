"""Per-agent mutable checklist ("TODOS").

A lightweight, mutable per-turn checklist that fills the gap between the
**append-only** ``PROGRESS.md`` (a running log) and the **heavyweight**
charter / mission plan (chief-authored, phase-gated, durable). An agent
maintains a short list of checklist items, each in one of three states::

    - [ ] not started        (status "todo")
    - [~] in progress        (status "doing")
    - [x] done               (status "done")

Agents emit the whole checklist as a ``TODOS:`` block in their response and
re-emit it every turn; the latest block **overwrites** the agent's previous
checklist (last-write-wins), unlike blackboard posts which are immutable.

Storage is a single JSON file, ``.clk/state/todos.json``, keyed by author so
parallel agents never clobber one another's list::

    {
      "version": 1,
      "authors": {
        "engineer": {
          "items": [{"status": "doing", "text": "wire the parser"}],
          "ts": "2026-05-01T12:34:56",
          "stage_id": "build_a",
          "workflow": "engineering"
        }
      }
    }

The file lives under ``.clk/`` (harness state, hidden from agents' direct
ACTION:write reach). The runner reads an author's list back into that
author's next prompt under the ``$todos`` placeholder. This mirrors the
``POST:`` block pipeline (parse -> persist -> re-inject) almost exactly; the
one deliberate difference is the mutable, per-author, last-write-wins store
instead of one immutable JSON file per entry.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Paths, load_json, save_json
from ..log import get_logger, log_exception
from ..utils.activity_log import log_event

logger = get_logger(__name__)

# Keep injected checklists bounded so they can't bloat a prompt.
_MAX_ITEMS = 50
_MAX_TEXT = 200

_MARK_TO_STATUS = {" ": "todo", "~": "doing", "x": "done", "X": "done"}
_STATUS_TO_MARK = {"todo": " ", "doing": "~", "done": "x"}


def todos_path(paths: Paths) -> Path:
    """Path to the single mutable checklist store."""
    return paths.state / "todos.json"


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------


@dataclass
class Todo:
    status: str = "todo"  # one of: todo | doing | done
    text: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {"status": self.status, "text": self.text}

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Todo":
        status = str(raw.get("status") or "todo").lower()
        if status not in _STATUS_TO_MARK:
            status = "todo"
        return cls(status=status, text=str(raw.get("text") or ""))


@dataclass
class TodoList:
    author: str
    items: List[Todo] = field(default_factory=list)
    ts: str = ""
    stage_id: str = ""
    workflow: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "items": [t.to_dict() for t in self.items],
            "ts": self.ts,
            "stage_id": self.stage_id,
            "workflow": self.workflow,
        }

    @classmethod
    def from_dict(cls, author: str, raw: Dict[str, Any]) -> "TodoList":
        return cls(
            author=author,
            items=[Todo.from_dict(x) for x in (raw.get("items") or []) if isinstance(x, dict)],
            ts=str(raw.get("ts") or ""),
            stage_id=str(raw.get("stage_id") or ""),
            workflow=str(raw.get("workflow") or ""),
        )


# ---------------------------------------------------------------------------
# TODOS: block parsing
# ---------------------------------------------------------------------------


_TODOS_HEAD_RE = re.compile(r"^\s*TODOS\s*:\s*$", re.IGNORECASE)
_TODOS_END_RE = re.compile(r"^\s*END_TODOS\s*$", re.IGNORECASE)
_TODOS_ITEM_RE = re.compile(r"^\s*[-*]\s*\[(?P<mark>[ ~xX])\]\s*(?P<text>.*\S)\s*$")


def parse_todos_blocks(text: str) -> Optional[List[Dict[str, str]]]:
    """Extract the items of the LAST complete ``TODOS:`` block in ``text``.

    Block grammar::

        TODOS:
        - [ ] not started
        - [~] in progress
        - [x] done
        END_TODOS

    The agent re-emits its full checklist each turn, so when several
    ``TODOS:`` blocks appear the last complete one wins (overwrite
    semantics). Non-item lines inside a block are ignored. Returns the
    parsed items (possibly empty for an intentionally-cleared list), or
    ``None`` when no complete block is present.
    """
    if not text or "TODOS:" not in text:
        return None
    lines = text.splitlines()
    result: Optional[List[Dict[str, str]]] = None
    i = 0
    while i < len(lines):
        if not _TODOS_HEAD_RE.match(lines[i]):
            i += 1
            continue
        i += 1
        items: List[Dict[str, str]] = []
        closed = False
        while i < len(lines):
            line = lines[i]
            if _TODOS_END_RE.match(line):
                closed = True
                i += 1
                break
            m = _TODOS_ITEM_RE.match(line)
            if m:
                status = _MARK_TO_STATUS.get(m.group("mark"), "todo")
                items.append({"status": status, "text": m.group("text").strip()})
            i += 1
        if closed:
            result = items  # last complete block wins
    return result


# ---------------------------------------------------------------------------
# Persistence (mutable, per-author, last-write-wins)
# ---------------------------------------------------------------------------


def load_todos(paths: Paths) -> Dict[str, Any]:
    """Load the raw todos store (``{"version", "authors": {...}}``)."""
    return load_json(todos_path(paths), {"version": 1, "authors": {}})


def todos_for(paths: Paths, author: str) -> Optional[TodoList]:
    """Return ``author``'s current checklist, or ``None`` if they have none."""
    if not author:
        return None
    try:
        store = load_todos(paths)
    except Exception as exc:
        log_exception("orchestration.todos.todos_for", exc)
        return None
    entry = (store.get("authors") or {}).get(author)
    if not isinstance(entry, dict):
        return None
    return TodoList.from_dict(author, entry)


def apply_todos_blocks(
    paths: Paths,
    text: str,
    *,
    author: str,
    stage_id: str = "",
    workflow: str = "",
) -> Optional[TodoList]:
    """Parse the last ``TODOS:`` block and OVERWRITE ``author``'s list.

    Returns the new :class:`TodoList` when a block was applied, else
    ``None``. Only the calling author's slot is touched — other authors'
    checklists are preserved.
    """
    items_raw = parse_todos_blocks(text)
    if items_raw is None:
        return None
    items = [Todo.from_dict(x) for x in items_raw][:_MAX_ITEMS]
    todolist = TodoList(
        author=author,
        items=items,
        ts=datetime.now().isoformat(timespec="seconds"),
        stage_id=stage_id,
        workflow=workflow,
    )
    try:
        store = load_todos(paths)
        if not isinstance(store.get("authors"), dict):
            store = {"version": 1, "authors": {}}
        store["authors"][author] = todolist.to_dict()
        paths.state.mkdir(parents=True, exist_ok=True)
        save_json(todos_path(paths), store)
    except Exception as exc:
        log_exception("orchestration.todos.apply_todos_blocks", exc)
        return None
    try:
        counts = _counts(items)
        log_event(
            paths,
            "todos_updated",
            author=author,
            total=len(items),
            todo=counts["todo"],
            doing=counts["doing"],
            done=counts["done"],
            stage_id=stage_id,
        )
    except Exception as exc:
        logger.debug("todos log_event failed: %s", exc)
    return todolist


# ---------------------------------------------------------------------------
# Rendering for the $todos prompt placeholder
# ---------------------------------------------------------------------------


def _counts(items: List[Todo]) -> Dict[str, int]:
    out = {"todo": 0, "doing": 0, "done": 0}
    for t in items:
        out[t.status] = out.get(t.status, 0) + 1
    return out


def render_todos(todolist: Optional[TodoList]) -> str:
    """Render a checklist as markdown for the ``$todos`` placeholder."""
    if todolist is None or not todolist.items:
        return "(no todos yet)"
    c = _counts(todolist.items)
    open_n = c["todo"] + c["doing"]
    lines = [f"Working checklist ({open_n} open, {c['done']} done):"]
    for t in todolist.items[:_MAX_ITEMS]:
        mark = _STATUS_TO_MARK.get(t.status, " ")
        text = t.text.strip()
        if len(text) > _MAX_TEXT:
            text = text[:_MAX_TEXT].rstrip() + " …"
        lines.append(f"- [{mark}] {text}")
    return "\n".join(lines)
