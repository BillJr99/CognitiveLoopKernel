"""Cognitive Loop Kernel TUI dashboard.

A curses-based front end inspired by gnhf / Archon. The screen is
re-drawn in place (no scrolling output) and shows:

  * a top status bar (project, provider, current phase, busy indicator)
  * a grid of agent cards, one per configured agent. Each card shows
    status (idle / working / done / failed), run count, current task,
    and the last result preview.
  * a status log pane that updates in place, colored by severity.
  * an idea / context line, the current conversation tail, and a
    Claude-Code-style ``>`` input field at the very bottom.

The TUI uses no third-party dependencies - just stdlib ``curses``,
``threading``, and ``queue``.

Conversational dispatch:
  * If no idea has been captured yet, the first user message becomes
    the idea, then the engineering workflow runs.
  * Subsequent free-text messages append to the conversation file
    ``.clk/state/conversation.md`` and trigger another engineering
    cycle so the agents see the new context on every turn.
  * Slash commands give explicit control:
      /idea <text>           replace the captured idea
      /run [workflow]        run a single workflow cycle (default: engineering)
      /loop ralph|autoresearch [N]  start a loop
      /stop                  request the active loop to stop
      /provider <name>       switch active provider
      /status                show a status snapshot in the log
      /quit                  exit the TUI
"""

from __future__ import annotations

import curses
import json
import queue
import sys
import textwrap
import threading
import time
import traceback
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, Tuple

from .config import (
    Paths,
    is_initialized,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    project_paths,
    save_json,
)
from .git_ops import add_all, commit as git_commit, has_changes, is_repo
from .orchestration import (
    AgentObserver,
    AgentRunner,
    AutoresearchLoop,
    Evaluator,
    RalphLoop,
    WorkflowRunner,
    load_workflow,
)
from .utils.logging_utils import log_exception


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


class AgentStatus:
    IDLE = "idle"
    WORKING = "working"
    DONE = "done"
    FAILED = "failed"


@dataclass
class AgentCard:
    name: str
    status: str = AgentStatus.IDLE
    current_task: str = ""
    last_result: str = ""
    last_error: str = ""
    runs: int = 0
    last_started_mono: float = 0.0
    last_duration_s: float = 0.0


@dataclass
class LogLine:
    ts: str
    level: str  # INFO | WARN | ERROR | USER | SYSTEM
    text: str


class DashboardState:
    """Thread-safe state shared between the UI thread and the worker."""

    def __init__(self, agent_names: List[str]) -> None:
        self.lock = threading.Lock()
        self.agents: Dict[str, AgentCard] = {n: AgentCard(name=n) for n in agent_names}
        self.log: Deque[LogLine] = deque(maxlen=400)
        self.idea: str = ""
        self.project_name: str = ""
        self.provider: str = ""
        self.phase: str = "idle"
        self.busy: bool = False
        self.input_buffer: str = ""
        self.conversation: List[Tuple[str, str]] = []
        self.stop_requested: bool = False
        self.iteration_count: int = 0

    # ----- mutators (locked) --------------------------------------------

    def add_log(self, text: str, level: str = "INFO") -> None:
        line = LogLine(ts=datetime.now().strftime("%H:%M:%S"), level=level, text=text)
        with self.lock:
            self.log.append(line)

    def begin_agent(self, name: str, objective: str) -> None:
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.status = AgentStatus.WORKING
            card.current_task = objective
            card.last_started_mono = time.monotonic()
        self.add_log(f"{name} :: start :: {objective[:80]}", level="INFO")

    def end_agent(self, name: str, ok: bool, preview: str = "", error: str = "") -> None:
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.status = AgentStatus.DONE if ok else AgentStatus.FAILED
            card.current_task = ""
            card.last_result = (preview or "").strip().replace("\n", " ")[:120]
            card.last_error = error[:120]
            card.runs += 1
            if card.last_started_mono:
                card.last_duration_s = time.monotonic() - card.last_started_mono
        self.add_log(
            f"{name} :: {'ok' if ok else 'fail'} :: {(preview or '').strip().splitlines()[0][:80] if preview else ''}",
            level="INFO" if ok else "WARN",
        )

    def set_phase(self, phase: str, busy: Optional[bool] = None) -> None:
        with self.lock:
            self.phase = phase
            if busy is not None:
                self.busy = busy

    def set_idea(self, idea: str) -> None:
        with self.lock:
            self.idea = idea[:500]

    def add_user_message(self, text: str) -> None:
        with self.lock:
            self.conversation.append(("user", text))
        self.add_log(text, level="USER")

    def add_system_message(self, text: str) -> None:
        with self.lock:
            self.conversation.append(("system", text))
        self.add_log(text, level="SYSTEM")

    def request_stop(self) -> None:
        with self.lock:
            self.stop_requested = True
        self.add_log("stop requested by user", level="WARN")

    def clear_stop(self) -> None:
        with self.lock:
            self.stop_requested = False

    def is_stop_requested(self) -> bool:
        with self.lock:
            return self.stop_requested

    def snapshot(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "agents": {k: vars(v).copy() for k, v in self.agents.items()},
                "log": list(self.log),
                "idea": self.idea,
                "project_name": self.project_name,
                "provider": self.provider,
                "phase": self.phase,
                "busy": self.busy,
                "input_buffer": self.input_buffer,
                "conversation": list(self.conversation),
            }


# ---------------------------------------------------------------------------
# Observer that updates the dashboard
# ---------------------------------------------------------------------------


class DashboardObserver(AgentObserver):
    def __init__(self, state: DashboardState) -> None:
        self.state = state

    def begin(self, agent: str, objective: str) -> None:
        self.state.begin_agent(agent, objective)

    def end(self, agent: str, run) -> None:  # type: ignore[override]
        ok = bool(run.response.ok)
        preview = run.response.text or ""
        err = run.response.error or ""
        self.state.end_agent(agent, ok=ok, preview=preview, error=err)

    def log(self, line: str) -> None:
        self.state.add_log(line)


# ---------------------------------------------------------------------------
# Worker thread
# ---------------------------------------------------------------------------


@dataclass
class Job:
    kind: str  # idea | run | loop | stop | provider | quit | status
    payload: Any = None


class Worker(threading.Thread):
    daemon = True

    def __init__(
        self,
        paths: Paths,
        runner: AgentRunner,
        evaluator: Evaluator,
        state: DashboardState,
        clk_cfg: Dict[str, Any],
        providers_cfg: Dict[str, Any],
    ) -> None:
        super().__init__(name="clk-worker")
        self.paths = paths
        self.runner = runner
        self.evaluator = evaluator
        self.state = state
        self.clk_cfg = clk_cfg
        self.providers_cfg = providers_cfg
        self.q: queue.Queue[Job] = queue.Queue()
        self._alive = True

    def submit(self, job: Job) -> None:
        self.q.put(job)

    def stop(self) -> None:
        self._alive = False
        self.q.put(Job("quit"))

    # --- main loop -------------------------------------------------------

    def run(self) -> None:
        while self._alive:
            try:
                job = self.q.get(timeout=0.25)
            except queue.Empty:
                continue
            try:
                self._dispatch(job)
            except Exception as exc:
                log_exception("tui.Worker.run", exc)
                self.state.add_log(f"worker error: {exc}", level="ERROR")
                self.state.set_phase("idle", busy=False)

    def _dispatch(self, job: Job) -> None:
        if job.kind == "quit":
            self._alive = False
            return
        if job.kind == "idea":
            self._do_idea(job.payload or "")
        elif job.kind == "run":
            self._do_workflow(job.payload or "engineering")
        elif job.kind == "loop":
            payload = job.payload or {}
            self._do_loop(
                mode=payload.get("mode", "ralph"),
                n=int(payload.get("n", self.clk_cfg.get("max_iterations") or 5)),
            )
        elif job.kind == "stop":
            self.state.request_stop()
        elif job.kind == "provider":
            self._do_set_provider(job.payload or "shell")
        elif job.kind == "status":
            self._emit_status()

    # --- handlers --------------------------------------------------------

    def _do_idea(self, idea: str) -> None:
        idea = idea.strip()
        if not idea:
            return
        self.state.set_phase("idea", busy=True)
        self.state.set_idea(idea)
        title = idea.split(".")[0][:80] or idea[:80]
        try:
            save_json(
                self.paths.state / "idea.json",
                {
                    "title": title,
                    "statement": idea,
                    "captured_at": datetime.now().isoformat(timespec="seconds"),
                    "tags": [],
                },
            )
            (self.paths.state / "system_brief.md").write_text(
                f"# System brief\n\n**Title:** {title}\n\n## Idea\n{idea}\n",
                encoding="utf-8",
            )
            self.state.add_system_message(f"idea captured: {title}")
            self._maybe_commit("clk-tui-idea", f"Capture idea: {title}", "idea captured", ".clk/state/idea.json")
        except Exception as exc:
            log_exception("tui.Worker._do_idea", exc)
            self.state.add_log(f"idea save failed: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    def _do_workflow(self, name: str) -> None:
        wf_path = self.paths.workflows / f"{name}.yaml"
        if not wf_path.exists():
            self.state.add_log(f"workflow not found: {name}", level="WARN")
            return
        self.state.set_phase(f"workflow:{name}", busy=True)
        try:
            wf = load_workflow(wf_path)
            wf_runner = WorkflowRunner(self.paths, self.runner)
            wf_runner.run(wf)
        except Exception as exc:
            log_exception("tui.Worker._do_workflow", exc)
            self.state.add_log(f"workflow {name} failed: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    def _do_loop(self, mode: str, n: int) -> None:
        self.state.clear_stop()
        self.state.set_phase(f"loop:{mode}", busy=True)
        try:
            if mode == "ralph":
                loop = RalphLoop(self.paths, self.runner, self.evaluator, max_iterations=n)
                # We can't preempt mid-iteration, but we can check between iterations
                # by running one iteration at a time.
                for i in range(1, n + 1):
                    if self.state.is_stop_requested():
                        self.state.add_log("loop interrupted", level="WARN")
                        break
                    self.state.iteration_count = i
                    sub = RalphLoop(self.paths, self.runner, self.evaluator, max_iterations=1)
                    sub.run()
            else:
                for i in range(1, n + 1):
                    if self.state.is_stop_requested():
                        self.state.add_log("loop interrupted", level="WARN")
                        break
                    self.state.iteration_count = i
                    sub = AutoresearchLoop(self.paths, self.runner, self.evaluator, max_iterations=1)
                    sub.run()
        except Exception as exc:
            log_exception("tui.Worker._do_loop", exc)
            self.state.add_log(f"loop failed: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    def _do_set_provider(self, name: str) -> None:
        try:
            cfg_path = self.paths.config / "providers.json"
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if name not in (data.get("providers") or {}):
                self.state.add_log(f"unknown provider: {name}", level="WARN")
                return
            data["active"] = name
            save_json(cfg_path, data)
            self.providers_cfg = data
            self.runner.providers_cfg = data
            with self.state.lock:
                self.state.provider = name
            self.state.add_system_message(f"provider switched to {name}")
        except Exception as exc:
            log_exception("tui.Worker._do_set_provider", exc)
            self.state.add_log(f"provider switch failed: {exc}", level="ERROR")

    def _emit_status(self) -> None:
        snap = self.state.snapshot()
        self.state.add_system_message(
            f"phase={snap['phase']} busy={snap['busy']} provider={snap['provider']} agents={len(snap['agents'])}"
        )

    def _maybe_commit(self, agent: str, objective: str, validation: str, *files: str) -> None:
        try:
            if not is_repo(self.paths.root):
                return
            if not has_changes(self.paths.root):
                return
            if not add_all(self.paths.root):
                return
            git_commit(
                self.paths.root,
                agent=agent,
                objective=objective,
                files_changed=list(files),
                validation=validation,
                next_step="continue conversation",
            )
        except Exception as exc:
            log_exception("tui.Worker._maybe_commit", exc)


# ---------------------------------------------------------------------------
# Curses UI
# ---------------------------------------------------------------------------


class TuiApp:
    """Curses front end."""

    COLOR_TITLE = 1
    COLOR_IDLE = 2
    COLOR_WORKING = 3
    COLOR_DONE = 4
    COLOR_FAILED = 5
    COLOR_LOG_INFO = 6
    COLOR_LOG_WARN = 7
    COLOR_LOG_ERROR = 8
    COLOR_LOG_USER = 9
    COLOR_LOG_SYS = 10
    COLOR_FRAME = 11
    COLOR_PROMPT = 12

    def __init__(self, state: DashboardState, worker: Worker) -> None:
        self.state = state
        self.worker = worker
        self.scroll_offset = 0  # lines to skip from the bottom of the log
        self._spinner = "|/-\\"
        self._spinner_idx = 0
        self._last_render = 0.0

    # --- entrypoint ------------------------------------------------------

    def run(self) -> None:
        try:
            curses.wrapper(self._loop)
        except Exception as exc:
            log_exception("tui.TuiApp.run", exc)

    # --- main loop -------------------------------------------------------

    def _loop(self, stdscr: "curses._CursesWindow") -> None:
        curses.curs_set(1)
        stdscr.nodelay(True)
        stdscr.timeout(80)
        self._init_colors()
        self.state.add_system_message(
            "TUI ready. Type your idea or follow-up; /quit to exit, /run, /loop ralph 5, /stop, /provider claude."
        )
        while True:
            try:
                self._render(stdscr)
                ch = stdscr.getch()
                if ch == -1:
                    self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner)
                    continue
                if not self._handle_key(ch):
                    break
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log_exception("tui.TuiApp._loop", exc)
                self.state.add_log(f"render error: {exc}", level="ERROR")
        self.worker.stop()

    # --- colors ----------------------------------------------------------

    def _init_colors(self) -> None:
        try:
            curses.start_color()
            curses.use_default_colors()
            curses.init_pair(self.COLOR_TITLE,    curses.COLOR_BLACK,  curses.COLOR_CYAN)
            curses.init_pair(self.COLOR_IDLE,     curses.COLOR_WHITE,  -1)
            curses.init_pair(self.COLOR_WORKING,  curses.COLOR_YELLOW, -1)
            curses.init_pair(self.COLOR_DONE,     curses.COLOR_GREEN,  -1)
            curses.init_pair(self.COLOR_FAILED,   curses.COLOR_RED,    -1)
            curses.init_pair(self.COLOR_LOG_INFO, curses.COLOR_WHITE,  -1)
            curses.init_pair(self.COLOR_LOG_WARN, curses.COLOR_YELLOW, -1)
            curses.init_pair(self.COLOR_LOG_ERROR,curses.COLOR_RED,    -1)
            curses.init_pair(self.COLOR_LOG_USER, curses.COLOR_CYAN,   -1)
            curses.init_pair(self.COLOR_LOG_SYS,  curses.COLOR_MAGENTA,-1)
            curses.init_pair(self.COLOR_FRAME,    curses.COLOR_BLUE,   -1)
            curses.init_pair(self.COLOR_PROMPT,   curses.COLOR_GREEN,  -1)
        except Exception as exc:
            log_exception("tui.TuiApp._init_colors", exc)

    # --- rendering -------------------------------------------------------

    def _render(self, stdscr: "curses._CursesWindow") -> None:
        h, w = stdscr.getmaxyx()
        if h < 12 or w < 60:
            stdscr.erase()
            self._safe_addstr(stdscr, 0, 0, "Terminal too small. Resize to at least 60x12.", w)
            stdscr.refresh()
            return
        stdscr.erase()
        self._draw_title(stdscr, w)
        grid_bottom = self._draw_agent_grid(stdscr, top=1, height=h, width=w)
        idea_bottom = self._draw_idea(stdscr, top=grid_bottom + 1, width=w)
        log_bottom = self._draw_log(stdscr, top=idea_bottom + 1, height=h - idea_bottom - 4, width=w)
        self._draw_input(stdscr, top=h - 2, width=w)
        stdscr.refresh()

    def _draw_title(self, stdscr, width: int) -> None:
        s = self.state
        with s.lock:
            project = s.project_name
            provider = s.provider
            phase = s.phase
            busy = s.busy
            iteration = s.iteration_count
        spin = self._spinner[self._spinner_idx] if busy else " "
        title = f" CLK :: {project} :: provider={provider} :: phase={phase} {spin} iter={iteration} "
        self._fill(stdscr, 0, 0, width, " ", curses.color_pair(self.COLOR_TITLE) | curses.A_BOLD)
        self._safe_addstr(
            stdscr, 0, 0, title.ljust(width - 1)[: width - 1], width,
            curses.color_pair(self.COLOR_TITLE) | curses.A_BOLD,
        )

    def _draw_agent_grid(self, stdscr, *, top: int, height: int, width: int) -> int:
        with self.state.lock:
            names = sorted(self.state.agents.keys())
            cards = [self.state.agents[n] for n in names]
        if not cards:
            return top
        cols = 4 if width >= 84 else (3 if width >= 64 else 2)
        rows = (len(cards) + cols - 1) // cols
        max_grid_height = max(1, height - 12)
        card_h = 4
        if rows * card_h > max_grid_height:
            card_h = max(3, max_grid_height // rows)
        card_w = (width - 1) // cols
        for i, card in enumerate(cards):
            r = i // cols
            c = i % cols
            y = top + r * card_h
            x = c * card_w
            self._draw_card(stdscr, y, x, card_h, card_w, card)
        return top + rows * card_h

    def _draw_card(self, stdscr, y: int, x: int, h: int, w: int, card: AgentCard) -> None:
        col = {
            AgentStatus.IDLE: self.COLOR_IDLE,
            AgentStatus.WORKING: self.COLOR_WORKING,
            AgentStatus.DONE: self.COLOR_DONE,
            AgentStatus.FAILED: self.COLOR_FAILED,
        }.get(card.status, self.COLOR_IDLE)
        attr = curses.color_pair(col)
        frame_attr = curses.color_pair(self.COLOR_FRAME)
        # Border
        try:
            self._safe_addstr(stdscr, y, x, "+" + "-" * (w - 2) + "+", w, frame_attr)
            for i in range(1, h - 1):
                self._safe_addstr(stdscr, y + i, x, "|", w, frame_attr)
                self._safe_addstr(stdscr, y + i, x + w - 1, "|", w, frame_attr)
            self._safe_addstr(stdscr, y + h - 1, x, "+" + "-" * (w - 2) + "+", w, frame_attr)
        except Exception:
            pass
        inner_w = max(1, w - 4)
        # Header line: name + status badge
        name_str = card.name[:inner_w]
        self._safe_addstr(stdscr, y + 1, x + 2, name_str, inner_w, attr | curses.A_BOLD)
        badge = f"[{card.status:<7}] r={card.runs:<3}"
        if len(name_str) + 1 + len(badge) <= inner_w:
            self._safe_addstr(stdscr, y + 1, x + w - 2 - len(badge), badge, inner_w, attr)
        # Bar (simple utilization meter from runs % 8)
        bar_len = max(0, inner_w - 2)
        if bar_len > 0:
            filled = (card.runs % bar_len) if card.status != AgentStatus.WORKING else (
                int((time.monotonic() * 4) % bar_len)
            )
            bar = "#" * filled + "." * (bar_len - filled)
            self._safe_addstr(stdscr, y + 2, x + 2, bar, inner_w, attr)
        # Task / last result
        if h >= 4:
            line = card.current_task or card.last_result or card.last_error or "-"
            self._safe_addstr(stdscr, y + 3, x + 2, line[:inner_w], inner_w, attr)

    def _draw_idea(self, stdscr, *, top: int, width: int) -> int:
        with self.state.lock:
            idea = self.state.idea
            convo_tail = self.state.conversation[-1:] if self.state.conversation else []
        line = "idea: " + (idea or "(no idea yet - type one below)")
        self._safe_addstr(stdscr, top, 0, line[: width - 1], width, curses.A_BOLD)
        if convo_tail:
            role, text = convo_tail[-1]
            tail = f"last [{role}]: {text}"[: width - 1]
            self._safe_addstr(stdscr, top + 1, 0, tail, width)
            return top + 1
        return top

    def _draw_log(self, stdscr, *, top: int, height: int, width: int) -> int:
        height = max(3, height)
        # Frame
        self._safe_addstr(
            stdscr, top, 0, "-" * (width - 1), width, curses.color_pair(self.COLOR_FRAME)
        )
        title = " status log (PgUp/PgDn to scroll) "
        self._safe_addstr(
            stdscr, top, max(0, (width - len(title)) // 2), title, width,
            curses.color_pair(self.COLOR_FRAME) | curses.A_BOLD,
        )
        body_top = top + 1
        body_height = height - 1
        with self.state.lock:
            lines = list(self.state.log)
        if not lines:
            self._safe_addstr(stdscr, body_top, 1, "(no events yet)", width)
            return body_top + body_height - 1
        # Apply scroll offset
        offset = max(0, min(self.scroll_offset, max(0, len(lines) - body_height)))
        end = len(lines) - offset
        start = max(0, end - body_height)
        slice_ = lines[start:end]
        for i, line in enumerate(slice_):
            attr = self._log_attr(line.level)
            text = f"{line.ts} [{line.level:<5}] {line.text}"
            self._safe_addstr(stdscr, body_top + i, 1, text[: width - 2], width, attr)
        return body_top + body_height - 1

    def _draw_input(self, stdscr, *, top: int, width: int) -> None:
        self._safe_addstr(
            stdscr, top, 0, "-" * (width - 1), width, curses.color_pair(self.COLOR_FRAME)
        )
        with self.state.lock:
            buf = self.state.input_buffer
        prompt = "> "
        text = (prompt + buf)[: width - 1]
        self._safe_addstr(
            stdscr, top + 1, 0, text, width, curses.color_pair(self.COLOR_PROMPT) | curses.A_BOLD
        )
        try:
            cursor_x = min(len(prompt) + len(buf), width - 2)
            stdscr.move(top + 1, cursor_x)
        except Exception:
            pass

    # --- input handling --------------------------------------------------

    def _handle_key(self, ch: int) -> bool:
        if ch in (4,):  # Ctrl-D
            self.worker.stop()
            return False
        if ch == curses.KEY_RESIZE:
            return True
        if ch == curses.KEY_PPAGE:
            self.scroll_offset += 5
            return True
        if ch == curses.KEY_NPAGE:
            self.scroll_offset = max(0, self.scroll_offset - 5)
            return True
        if ch == curses.KEY_HOME:
            self.scroll_offset = 10**9
            return True
        if ch == curses.KEY_END:
            self.scroll_offset = 0
            return True
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            with self.state.lock:
                self.state.input_buffer = self.state.input_buffer[:-1]
            return True
        if ch in (10, 13, curses.KEY_ENTER):
            with self.state.lock:
                msg = self.state.input_buffer
                self.state.input_buffer = ""
            if msg.strip():
                return self._dispatch(msg.strip())
            return True
        if 32 <= ch < 127:
            with self.state.lock:
                if len(self.state.input_buffer) < 1024:
                    self.state.input_buffer += chr(ch)
            return True
        return True

    # --- chat dispatch ---------------------------------------------------

    def _dispatch(self, msg: str) -> bool:
        self.state.add_user_message(msg)
        if msg.startswith("/"):
            parts = msg[1:].split()
            if not parts:
                return True
            cmd = parts[0].lower()
            args = parts[1:]
            if cmd in ("quit", "exit"):
                self.worker.stop()
                return False
            if cmd == "idea":
                if args:
                    self.worker.submit(Job("idea", " ".join(args)))
                return True
            if cmd == "run":
                self.worker.submit(Job("run", args[0] if args else "engineering"))
                return True
            if cmd == "loop":
                mode = args[0] if args else "ralph"
                n = int(args[1]) if len(args) > 1 and args[1].isdigit() else 5
                self.worker.submit(Job("loop", {"mode": mode, "n": n}))
                return True
            if cmd == "stop":
                self.worker.submit(Job("stop"))
                return True
            if cmd == "provider":
                if args:
                    self.worker.submit(Job("provider", args[0]))
                return True
            if cmd == "status":
                self.worker.submit(Job("status"))
                return True
            self.state.add_log(f"unknown command: /{cmd}", level="WARN")
            return True
        # Free-text: first message becomes the idea; subsequent ones are
        # appended to the conversation file and trigger another run.
        if not self.state.idea:
            self.worker.submit(Job("idea", msg))
            self.worker.submit(Job("run", "engineering"))
        else:
            self._append_conversation(msg)
            self.worker.submit(Job("run", "engineering"))
        return True

    def _append_conversation(self, msg: str) -> None:
        try:
            path = self.worker.paths.state / "conversation.md"
            ts = datetime.now().isoformat(timespec="seconds")
            with path.open("a", encoding="utf-8") as fh:
                fh.write(f"\n## {ts} (user)\n{msg}\n")
        except Exception as exc:
            log_exception("tui.TuiApp._append_conversation", exc)

    # --- helpers ---------------------------------------------------------

    def _safe_addstr(self, stdscr, y: int, x: int, text: str, max_width: int, attr: int = 0) -> None:
        if y < 0 or x < 0:
            return
        try:
            h, w = stdscr.getmaxyx()
            if y >= h:
                return
            limit = max(0, min(len(text), w - x - 1))
            if limit <= 0:
                return
            stdscr.addnstr(y, x, text, limit, attr)
        except curses.error:
            pass
        except Exception as exc:
            log_exception("tui.TuiApp._safe_addstr", exc)

    def _fill(self, stdscr, y: int, x: int, width: int, ch: str, attr: int = 0) -> None:
        try:
            stdscr.addnstr(y, x, ch * (width - x - 1), max(0, width - x - 1), attr)
        except curses.error:
            pass

    def _log_attr(self, level: str) -> int:
        return {
            "INFO": curses.color_pair(self.COLOR_LOG_INFO),
            "WARN": curses.color_pair(self.COLOR_LOG_WARN) | curses.A_BOLD,
            "ERROR": curses.color_pair(self.COLOR_LOG_ERROR) | curses.A_BOLD,
            "USER": curses.color_pair(self.COLOR_LOG_USER) | curses.A_BOLD,
            "SYSTEM": curses.color_pair(self.COLOR_LOG_SYS),
        }.get(level, 0)


# ---------------------------------------------------------------------------
# Public entrypoint
# ---------------------------------------------------------------------------


def run(initial_prompt: Optional[str] = None) -> int:
    """Entry point used by ``clk tui``."""
    paths = project_paths()
    if not is_initialized(paths):
        print("CLK is not initialized. Run `clk init` first.", file=sys.stderr)
        return 2

    clk_cfg = load_clk_config(paths)
    providers_cfg = load_providers_config(paths)
    agents_cfg = load_agents_config(paths)
    agent_names = list((agents_cfg.get("agents") or {}).keys())

    state = DashboardState(agent_names)
    state.project_name = clk_cfg.get("project_name") or paths.root.name
    state.provider = providers_cfg.get("active") or clk_cfg.get("default_provider") or "shell"

    observer = DashboardObserver(state)
    runner = AgentRunner(
        paths=paths,
        agents_cfg=agents_cfg,
        providers_cfg=providers_cfg,
        clk_cfg=clk_cfg,
        observer=observer,
    )
    checks = clk_cfg.get("validation_checks") or ["test -f .clk/config/clk.config.json"]
    evaluator = Evaluator(root=paths.root, default_checks=checks)
    worker = Worker(paths, runner, evaluator, state, clk_cfg, providers_cfg)
    worker.start()

    # Pre-populate from existing idea if any.
    idea_path = paths.state / "idea.json"
    if idea_path.exists():
        try:
            payload = json.loads(idea_path.read_text(encoding="utf-8"))
            state.set_idea(payload.get("statement") or payload.get("title") or "")
        except Exception as exc:
            log_exception("tui.run.load_idea", exc)

    if initial_prompt:
        worker.submit(Job("idea", initial_prompt))
        worker.submit(Job("run", "engineering"))

    app = TuiApp(state, worker)
    try:
        app.run()
    finally:
        worker.stop()
        worker.join(timeout=2.0)
    return 0
