"""Agent action protocol.

Agents recommend changes; this module turns those recommendations into
real edits on disk. Without it the harness only ever produces text;
with it any provider (shell stub, Claude, Codex, Ollama, Pi) can drive
the project forward by emitting ``ACTION:`` blocks in their response.

Block grammar (machine-parsed; appears between markers, may repeat)::

    ACTION: write
    PATH: relative/path.ext
    CONTENT:
    <full file body>
    END_ACTION

    ACTION: edit
    PATH: relative/path.ext
    OLD:
    <exact pre-existing text>
    NEW:
    <replacement text>
    END_ACTION

    ACTION: append
    PATH: relative/path.ext
    CONTENT:
    <text appended verbatim>
    END_ACTION

    ACTION: delete
    PATH: relative/path.ext
    END_ACTION

    ACTION: run
    CMD: <shell command>
    END_ACTION

    ACTION: done
    REASON: <one line describing why we're done>
    END_ACTION

Safety rules (enforced here, not on the agent)
- PATH must be relative and must resolve under ``project_root``. Any
  attempt to escape (``../``, absolute paths) is rejected.
- The cap from ``clk.config.json::validation.max_files_per_batch`` (default
  25) limits how many file-mutating actions we apply per response. Past
  that we stop and log; nothing is half-applied.
- Files about to be overwritten are first copied to
  ``.clk/backups/<run_id>/`` so the original is recoverable.
- ``run`` commands inherit the harness env, run inside ``project_root``,
  capture stdout/stderr, and timeout at 120s by default.
- ``run`` rejects obvious-foot-gun patterns (``sudo``, ``rm -rf /``,
  ``rm -rf ~``).
- ``done`` writes ``.clk/state/done.md`` with the supplied reason; the
  loops already treat that as a "stop iterating" signal.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import Paths
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class Action:
    kind: str  # write | edit | append | delete | run | done
    path: str = ""
    content: str = ""
    old: str = ""
    new: str = ""
    cmd: str = ""
    reason: str = ""


_HEAD_RE = re.compile(r"^\s*ACTION\s*:\s*(write|edit|append|delete|run|done)\s*$", re.IGNORECASE | re.MULTILINE)
_END_RE = re.compile(r"^\s*END_ACTION\s*$", re.IGNORECASE)


def _next_section(lines: List[str], start: int) -> Tuple[Optional[str], List[str], int]:
    """Read a `KEY:` header followed by either an inline value or
    indented/multi-line content until the next header or END_ACTION.

    Returns ``(key_upper, body_lines, next_index)``.
    """
    if start >= len(lines):
        return None, [], start
    header_re = re.compile(r"^\s*([A-Z_]+)\s*:\s*(.*)$")
    m = header_re.match(lines[start])
    if not m:
        return None, [], start
    key = m.group(1).upper()
    inline = m.group(2)
    body: List[str] = []
    i = start + 1
    if inline:
        body.append(inline)
    while i < len(lines):
        if _END_RE.match(lines[i]):
            break
        if header_re.match(lines[i]) and lines[i].strip().split(":", 1)[0].upper() in {
            "PATH", "CONTENT", "OLD", "NEW", "CMD", "REASON"
        }:
            break
        body.append(lines[i])
        i += 1
    return key, body, i


def parse_actions(text: str) -> List[Action]:
    """Extract every ACTION block from ``text``. Robust to noise."""
    if not text or "ACTION:" not in text and "ACTION :" not in text:
        return []
    out: List[Action] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        action = Action(kind=m.group(1).lower())
        i += 1
        # Sections within the block until END_ACTION.
        while i < len(lines) and not _END_RE.match(lines[i]):
            key, body, j = _next_section(lines, i)
            if key is None:
                i += 1
                continue
            joined = "\n".join(body).strip("\n")
            if key == "PATH":
                action.path = joined.strip()
            elif key == "CONTENT":
                action.content = joined
            elif key == "OLD":
                action.old = joined
            elif key == "NEW":
                action.new = joined
            elif key == "CMD":
                action.cmd = joined.strip()
            elif key == "REASON":
                action.reason = joined.strip()
            i = j
        # consume the END_ACTION line (if present)
        if i < len(lines) and _END_RE.match(lines[i]):
            i += 1
        if action.kind:
            out.append(action)
    return out


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


_DANGEROUS_RE = re.compile(
    r"\b(sudo\b|rm\s+-rf\s+/|rm\s+-rf\s+~|>\s*/dev/sd|mkfs|:\(\)\s*\{)"
)


@dataclass
class ActionResult:
    files_written: List[str] = field(default_factory=list)
    files_deleted: List[str] = field(default_factory=list)
    commands_run: List[str] = field(default_factory=list)
    command_outputs: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)  # human-readable reasons
    errors: List[str] = field(default_factory=list)
    done: bool = False
    done_reason: str = ""

    def is_empty(self) -> bool:
        return not (
            self.files_written
            or self.files_deleted
            or self.commands_run
            or self.skipped
            or self.errors
            or self.done
        )

    def summary(self) -> str:
        bits: List[str] = []
        if self.files_written:
            bits.append(f"wrote {len(self.files_written)}")
        if self.files_deleted:
            bits.append(f"deleted {len(self.files_deleted)}")
        if self.commands_run:
            bits.append(f"ran {len(self.commands_run)}")
        if self.skipped:
            bits.append(f"skipped {len(self.skipped)}")
        if self.errors:
            bits.append(f"errors {len(self.errors)}")
        if self.done:
            bits.append("done")
        return " ".join(bits) or "(no actions)"


_HARNESS_DIR_NAMES = (".clk", "workspace")


def _normalize_rel(root: Path, rel: str) -> str:
    """Strip redundant prefixes that agents commonly add by mistake.

    Agents now operate directly at the project root (the ``workspace/``
    directory has been folded into root). They occasionally still emit
    ``PATH: workspace/foo.py`` out of habit; we strip that prefix to
    avoid creating a stray ``workspace/`` subdirectory.

    Also strips a leading ``./`` for the same ergonomics.
    """
    rel = (rel or "").strip()
    if not rel:
        return rel
    while rel.startswith("./"):
        rel = rel[2:]
    # Drop a leading ``workspace/`` left over from the previous layout.
    for _ in range(2):
        if rel == "workspace" or rel.startswith("workspace/") or rel.startswith("workspace\\"):
            rel = rel[len("workspace") + 1:] if len(rel) > len("workspace") else ""
        else:
            break
    return rel


def _resolve_safe(root: Path, rel: str) -> Optional[Path]:
    """Resolve ``rel`` relative to the project ``root``, refusing escapes
    and refusing any path that targets the harness (``.clk/``) tree.

    Returns the resolved Path on success, or ``None`` when the path is
    rejected. The two failure modes:

      * The path resolves outside the project root (``../escape``,
        absolute paths).
      * The path resolves into ``.clk/`` — that subtree is reserved
        for the harness (config, runs, prompts, blackboard, harness
        sources) and never directly written by agent ACTION blocks.
        Agents emit POST blocks instead; the harness routes those into
        ``.clk/blackboard/``.
    """
    rel = _normalize_rel(root, rel)
    if not rel:
        return None
    if rel.startswith("/"):
        return None
    root.mkdir(parents=True, exist_ok=True)
    candidate = (root / rel).resolve()
    root_resolved = root.resolve()
    try:
        relative = candidate.relative_to(root_resolved)
    except ValueError:
        return None
    parts = relative.parts
    if parts and parts[0] == ".clk":
        return None
    return candidate


def _backup(paths: Paths, target: Path, backup_root: Path) -> None:
    if not target.exists():
        return
    try:
        try:
            rel = target.relative_to(paths.root)
        except ValueError:
            # Target lives outside the project; back it up by basename
            # rather than dropping the backup entirely.
            rel = Path(target.name)
        dest = backup_root / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, dest)
    except Exception as exc:
        log_exception("orchestration.actions._backup", exc)


def apply_actions(
    paths: Paths,
    response_text: str,
    *,
    agent_name: str = "agent",
    clk_cfg: Optional[Dict[str, Any]] = None,
    timeout_s: int = 120,
) -> ActionResult:
    """Parse and execute every ACTION block in ``response_text``.

    File-mutating actions resolve paths under ``paths.root`` (the
    project root, which is also ``paths.workspace`` under the current
    layout). The ``.clk/`` subtree is reserved for the harness and is
    rejected by ``_resolve_safe``. ``run`` commands execute with cwd
    set to the project root.

    Returns an ``ActionResult`` that callers should merge into the
    ``files_written`` reported on the AgentRun, so the TUI's per-card
    file list and the title-bar totals reflect harness-driven changes.
    """
    result = ActionResult()
    actions = parse_actions(response_text)
    if not actions:
        return result

    cfg = clk_cfg or {}
    cap = int(((cfg.get("validation") or {}).get("max_files_per_batch")) or 25)
    paths.backups.mkdir(parents=True, exist_ok=True)
    paths.workspace.mkdir(parents=True, exist_ok=True)
    backup_root = paths.backups / f"{datetime.now().strftime('%Y%m%dT%H%M%S')}-{agent_name}"

    file_actions = sum(1 for a in actions if a.kind in ("write", "edit", "append", "delete"))
    if file_actions > cap:
        log(
            f"actions[{agent_name}]: {file_actions} file actions exceeds cap {cap}; "
            "applying first {cap} only and skipping the rest",
            level="WARN",
        )

    applied_files = 0
    for action in actions:
        try:
            if action.kind == "done":
                _do_done(paths, action, result)
                log_event(paths, "action_applied", agent=agent_name, kind="done",
                          action="done", reason=action.reason, ok=True)
                continue
            if action.kind == "run":
                pre_cmds = len(result.commands_run)
                pre_errors = len(result.errors)
                log_event(
                    paths,
                    "shell_command_start",
                    agent=agent_name,
                    action="run",
                    cmd=action.cmd,
                    cwd=str(paths.workspace),
                    timeout_s=timeout_s,
                )
                _do_run(paths, action, result, timeout_s=timeout_s)
                ran = len(result.commands_run) > pre_cmds
                output = result.command_outputs[-1] if ran and result.command_outputs else ""
                errors = result.errors[pre_errors:]
                log_event(paths, "action_applied", agent=agent_name, kind="run",
                          action="run", cmd=action.cmd, ok=ran and not errors,
                          output=output, output_chars=len(output or ""),
                          errors=list(errors), timeout_s=timeout_s)
                log_event(
                    paths,
                    "shell_command_end",
                    agent=agent_name,
                    action="run",
                    cmd=action.cmd,
                    ok=ran and not errors,
                    output=output,
                    output_chars=len(output or ""),
                    errors=list(errors),
                )
                continue
            # File-mutating actions
            if applied_files >= cap:
                result.skipped.append(f"{action.kind} {action.path}: cap_reached")
                log_event(paths, "action_skipped", agent=agent_name, kind=action.kind,
                          path=action.path, reason="cap_reached")
                continue
            applied_files += 1
            pre_skips = len(result.skipped)
            normalized_path = _normalize_rel(paths.workspace, action.path) if action.path else action.path
            if action.kind == "write":
                _do_write(paths, action, result, backup_root)
            elif action.kind == "edit":
                _do_edit(paths, action, result, backup_root)
            elif action.kind == "append":
                _do_append(paths, action, result, backup_root)
            elif action.kind == "delete":
                _do_delete(paths, action, result, backup_root)
            else:
                result.skipped.append(f"unknown action: {action.kind}")
                log_event(paths, "action_skipped", agent=agent_name,
                          kind=action.kind, reason="unknown_kind")
                continue
            # Detect outcome from the result lists' tails
            if normalized_path != action.path:
                log_event(paths, "action_path_normalized", agent=agent_name,
                          kind=action.kind, original=action.path, used=normalized_path)
            written = result.files_written[-1:] if result.files_written else []
            deleted = result.files_deleted[-1:] if result.files_deleted else []
            skipped = result.skipped[pre_skips:]
            log_event(
                paths,
                "action_applied",
                agent=agent_name,
                kind=action.kind,
                action=action.kind,
                path=normalized_path,
                ok=bool(written or deleted) and not (skipped and skipped[0].startswith(action.kind)),
                file_written=(written[0] if written else None),
                file_deleted=(deleted[0] if deleted else None),
                content_chars=len(action.content) if action.content else 0,
                content=action.content if action.kind in ("write", "append") else "",
                old=action.old if action.kind == "edit" else "",
                new=action.new if action.kind == "edit" else "",
                skipped=list(skipped),
            )
        except Exception as exc:
            log_exception(f"orchestration.actions.apply[{action.kind}]", exc)
            result.errors.append(f"{action.kind} {action.path or action.cmd}: {exc}")
            log_event(paths, "action_error", agent=agent_name,
                      kind=action.kind, path=action.path, error=str(exc))
    return result


def _rel(paths: Paths, target: Path) -> str:
    try:
        return str(target.relative_to(paths.root))
    except ValueError:
        return str(target)


def _do_write(paths: Paths, action: Action, result: ActionResult, backup_root: Path) -> None:
    target = _resolve_safe(paths.workspace, action.path)
    if target is None:
        result.skipped.append(f"write {action.path}: path_outside_workspace")
        return
    if target.exists():
        _backup(paths, target, backup_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = action.content if action.content.endswith("\n") else action.content + "\n"
    target.write_text(body, encoding="utf-8")
    result.files_written.append(_rel(paths, target))


def _do_edit(paths: Paths, action: Action, result: ActionResult, backup_root: Path) -> None:
    target = _resolve_safe(paths.workspace, action.path)
    if target is None:
        result.skipped.append(f"edit {action.path}: path_outside_project")
        return
    if not target.exists():
        result.skipped.append(f"edit {action.path}: file_missing")
        return
    if not action.old:
        result.skipped.append(f"edit {action.path}: empty_OLD")
        return
    text = target.read_text(encoding="utf-8")
    if action.old not in text:
        result.skipped.append(f"edit {action.path}: OLD_not_found")
        return
    occurrences = text.count(action.old)
    if occurrences > 1:
        result.skipped.append(f"edit {action.path}: OLD_ambiguous_{occurrences}_matches")
        return
    _backup(paths, target, backup_root)
    target.write_text(text.replace(action.old, action.new, 1), encoding="utf-8")
    result.files_written.append(_rel(paths, target))


def _do_append(paths: Paths, action: Action, result: ActionResult, backup_root: Path) -> None:
    target = _resolve_safe(paths.workspace, action.path)
    if target is None:
        result.skipped.append(f"append {action.path}: path_outside_project")
        return
    if target.exists():
        _backup(paths, target, backup_root)
    target.parent.mkdir(parents=True, exist_ok=True)
    body = action.content if action.content.endswith("\n") else action.content + "\n"
    with target.open("a", encoding="utf-8") as fh:
        fh.write(body)
    result.files_written.append(_rel(paths, target))


def _do_delete(paths: Paths, action: Action, result: ActionResult, backup_root: Path) -> None:
    target = _resolve_safe(paths.workspace, action.path)
    if target is None:
        result.skipped.append(f"delete {action.path}: path_outside_project")
        return
    if not target.exists():
        result.skipped.append(f"delete {action.path}: file_missing")
        return
    _backup(paths, target, backup_root)
    try:
        target.unlink()
        result.files_deleted.append(_rel(paths, target))
    except IsADirectoryError:
        result.skipped.append(f"delete {action.path}: is_directory")


def _do_run(paths: Paths, action: Action, result: ActionResult, *, timeout_s: int) -> None:
    cmd = (action.cmd or "").strip()
    if not cmd:
        result.skipped.append("run: empty_cmd")
        return
    if _DANGEROUS_RE.search(cmd):
        result.skipped.append(f"run: refused_dangerous '{cmd[:80]}'")
        return
    try:
        r = subprocess.run(
            cmd,
            shell=True,
            cwd=str(paths.workspace),
            capture_output=True,
            text=True,
            timeout=timeout_s,
        )
        out = (r.stdout or "") + (r.stderr or "")
        result.commands_run.append(cmd)
        result.command_outputs.append(out)
        if r.returncode != 0:
            result.errors.append(f"run rc={r.returncode}: {cmd[:80]}")
    except subprocess.TimeoutExpired:
        result.errors.append(f"run timeout: {cmd[:80]}")
    except Exception as exc:
        result.errors.append(f"run failed: {exc}")


def _do_done(paths: Paths, action: Action, result: ActionResult) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    body = f"# Done\n\nReason: {action.reason or '(no reason given)'}\n\nMarked at {datetime.now().isoformat(timespec='seconds')}\n"
    (paths.state / "done.md").write_text(body, encoding="utf-8")
    result.done = True
    result.done_reason = action.reason or ""
    result.files_written.append(".clk/state/done.md")


# ---------------------------------------------------------------------------
# Prompt fragment for prompts that want to expose the protocol
# ---------------------------------------------------------------------------


ACTION_PROTOCOL = """\
Action protocol (machine-executed, follow exactly)

To actually change the project, emit one or more ACTION blocks in your
response. The harness parses and applies them immediately, with backups
of any overwritten files, and reports the resulting changes back through
files_written. Do NOT just *describe* changes - emit blocks.

  ACTION: write
  PATH: relative/path.ext
  CONTENT:
  <full file body>
  END_ACTION

  ACTION: edit
  PATH: relative/path.ext
  OLD:
  <exact existing text - must appear once in the file>
  NEW:
  <replacement text>
  END_ACTION

  ACTION: append
  PATH: relative/path.ext
  CONTENT:
  <text appended verbatim>
  END_ACTION

  ACTION: delete
  PATH: relative/path.ext
  END_ACTION

  ACTION: run
  CMD: <shell command - runs in project root, captured and logged>
  END_ACTION

  ACTION: done
  REASON: <why the project's completion criteria are now met>
  END_ACTION

Rules
- All paths must be relative and inside the project root.
- File-mutating actions are capped per response (default 25). Stay focused.
- ``run`` rejects sudo and obvious-foot-gun patterns; nothing destructive.
- ``done`` writes .clk/state/done.md and signals the loops to stop.
"""
