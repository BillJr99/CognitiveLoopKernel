"""Curses application: layout, rendering, key handling, entry point.
"""

from __future__ import annotations

import curses
import json
import sys
import time
from datetime import datetime
from typing import Dict, List, Optional, Tuple

from ..config import (
    is_initialized,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    project_paths,
)
from ..log import get_logger, log_exception
from ..orchestration import (
    AgentRunner,
    Evaluator,
)
from ..pricing import format_usd
from .commands import Job, Worker
from .dashboard import AgentCard, AgentStatus, DashboardObserver, DashboardState
from .stream import _StreamToLog
from .theme import _format_tokens, _word_wrap

logger = get_logger(__name__)


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
        # Heartbeat: when an agent has been WORKING for a while we emit
        # "still working: Ns" log lines so the user knows the process
        # is alive (vs. genuinely stuck). Per-agent so one slow chief
        # doesn't flood the pane.
        self._heartbeat_last: Dict[str, float] = {}
        # Heartbeat cadence. Longer than the original 8s/8s so a normal
        # 30-60s model call doesn't drown the log in "still working" lines.
        self.HEARTBEAT_FIRST_S = 15.0   # first heartbeat after this much silence
        self.HEARTBEAT_REPEAT_S = 15.0  # then every N seconds while still working
        # /help modal overlay: toggled by /help, F1, or `?` when the input
        # buffer is empty. Esc/q dismisses. While visible, _render draws
        # an extra centred panel above everything else.
        self._help_visible: bool = False

    # --- entrypoint ------------------------------------------------------

    def run(self) -> None:
        try:
            curses.wrapper(self._loop)
        except Exception as exc:
            log_exception("tui.TuiApp.run", exc)

    # --- main loop -------------------------------------------------------

    def _loop(self, stdscr: "curses.window") -> None:
        curses.curs_set(1)
        stdscr.nodelay(True)
        stdscr.timeout(80)
        self._init_colors()
        # First-run welcome: a one-time multi-line greeting that explains
        # what CLK is, what agents are, and the most useful commands. We
        # gate on a marker file under .clk/state so subsequent runs show
        # a one-liner instead.
        self._emit_welcome()
        # Proactive provider health check — surface broken providers
        # *before* the user types their first idea so they don't get a
        # surprise "cli not found" three seconds in.
        self._emit_provider_health()
        while True:
            try:
                self._render(stdscr)
                ch = stdscr.getch()
                if ch == -1:
                    self._spinner_idx = (self._spinner_idx + 1) % len(self._spinner)
                    self._tick_heartbeat()
                    continue
                if not self._handle_key(ch):
                    break
            except KeyboardInterrupt:
                break
            except Exception as exc:
                log_exception("tui.TuiApp._loop", exc)
                self.state.add_log(f"render error: {exc}", level="ERROR")
        self.worker.stop()

    # --- welcome & health ------------------------------------------------

    def _emit_welcome(self) -> None:
        """Emit the welcome banner. Multi-line on first run, one-liner after."""
        marker = self.state.paths.state / ".seen-welcome" if self.state.paths else None
        first_run = True
        if marker is not None:
            try:
                first_run = not marker.exists()
            except Exception:
                first_run = True
        if first_run:
            lines = [
                "Welcome to CLK — Cognitive Loop Kernel.",
                "Type any idea below and a team of agents (chief, qa, ralph + dynamic roles)",
                "will plan, build, and refine it together. Each commit is checkpointed in git.",
                "",
                "Quick commands:",
                "  /help         see the full command list (or press F1)",
                "  /tutorial     run a 30-second sample idea on the shell provider (free)",
                "  /provider X   switch the active AI (claude, ollama, pi, …)",
                "  /quit         exit (Ctrl-D also works)",
            ]
            for line in lines:
                self.state.add_system_message(line)
            if marker is not None:
                try:
                    marker.parent.mkdir(parents=True, exist_ok=True)
                    marker.write_text("seen\n", encoding="utf-8")
                except Exception as _exc:
                    logger.debug("could not persist welcome marker: %s", _exc)
        else:
            self.state.add_system_message(
                "Welcome back. Type an idea, or /help for commands."
            )

    def _emit_provider_health(self) -> None:
        """Surface the available/broken status of each configured provider."""
        if not self.state.paths:
            return
        try:
            from ..config import save_providers_config
            from ..providers import available_providers
            prov_cfg = load_providers_config(self.state.paths)
            # Snapshot endpoints so we can detect auto-failover (localhost ->
            # host.docker.internal) inside available_providers and persist
            # the swap to providers.json.
            before = {
                name: (cfg or {}).get("endpoint")
                for name, cfg in (prov_cfg.get("providers") or {}).items()
            }
            avail = available_providers(prov_cfg)
            swapped = []
            for name, cfg in (prov_cfg.get("providers") or {}).items():
                new_ep = (cfg or {}).get("endpoint")
                if new_ep and before.get(name) and new_ep != before[name]:
                    swapped.append((name, before[name], new_ep))
            if swapped:
                try:
                    save_providers_config(self.state.paths, prov_cfg)
                except Exception as exc:
                    log_exception("tui.TuiApp._emit_provider_health.save", exc)
                for name, old, new in swapped:
                    self.state.add_log(
                        f"{name}: {old} unreachable, auto-switched to {new} "
                        "(host.docker.internal). providers.json updated.",
                        level="WARN",
                    )
            active = self.state.provider or prov_cfg.get("active") or ""
            self.state.add_log("provider check:", level="SYSTEM")
            for name, ok in avail.items():
                tag = "available" if ok else "UNAVAILABLE"
                marker = " (active)" if name == active else ""
                self.state.add_log(f"  {name:<10} {tag}{marker}", level="SYSTEM" if ok else "WARN")
            if not avail.get(active, False):
                # Pre-load the hint bar so the user sees a fix path
                # before typing anything.
                with self.state.lock:
                    self.state.last_error_kind = "not_installed"
                    self.state.last_error_command = "/install"
        except Exception as exc:
            log_exception("tui.TuiApp._emit_provider_health", exc)

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

    INPUT_MAX_ROWS = 5  # cap on how tall the input area can grow

    def _render(self, stdscr: "curses.window") -> None:
        h, w = stdscr.getmaxyx()
        if h < 13 or w < 60:
            stdscr.erase()
            self._safe_addstr(stdscr, 0, 0, "Terminal too small. Resize to at least 60x13.", w)
            stdscr.refresh()
            return
        stdscr.erase()
        self._draw_title(stdscr, w)
        grid_bottom = self._draw_agent_grid(stdscr, top=1, height=h, width=w)
        idea_bottom = self._draw_idea(stdscr, top=grid_bottom + 1, width=w)
        # Compute how many rows the input field needs right now.
        with self.state.lock:
            buf = self.state.input_buffer
            cursor = self.state.input_cursor
        input_rows = self._input_row_count(buf, w)
        # Layout from the bottom up:
        #   y=h-input_rows..h-1                 input field rows (1..N)
        #   y=h-input_rows-1                    input frame ("---")
        #   y=h-input_rows-2                    hint bar (state-aware)
        #   y=h-input_rows-3                    global token totals
        #   y=idea_bottom+1..h-input_rows-4     log pane
        log_height = max(3, h - idea_bottom - input_rows - 4)
        self._draw_log(stdscr, top=idea_bottom + 1, height=log_height, width=w)
        self._draw_totals(stdscr, top=h - input_rows - 3, width=w)
        self._draw_hint_bar(stdscr, top=h - input_rows - 2, width=w)
        self._draw_input(stdscr, top=h - input_rows - 1, width=w, rows=input_rows, cursor=cursor)
        if self._help_visible:
            self._draw_help_overlay(stdscr, h, w)
        stdscr.refresh()

    def _tick_heartbeat(self) -> None:
        """Emit a 'still working' note for any agent that's been WORKING
        long enough that the user might think the harness is stuck.

        The heartbeat now reports whether the underlying subprocess is
        making progress: if stdout/stderr have been streaming (the
        ``live_*`` fields are advancing), we say "active"; if they've
        been silent for a long time we say "silent" and include the
        last line we did see, so the user can tell "this is just slow"
        vs "this is genuinely stuck".
        """
        now = time.monotonic()
        with self.state.lock:
            working = [
                (
                    name,
                    card.last_started_mono,
                    card.live_pid,
                    card.live_stdout_chars + card.live_stderr_chars,
                    card.live_last_update_mono,
                    card.live_last_line,
                    card.live_cpu_pct,
                )
                for name, card in self.state.agents.items()
                if card.status == AgentStatus.WORKING
            ]
        for name, started, pid, total_bytes, last_io, last_line, cpu_str in working:
            elapsed = max(0.0, now - started)
            last = self._heartbeat_last.get(name, started)
            interval = self.HEARTBEAT_FIRST_S if last == started else self.HEARTBEAT_REPEAT_S
            if elapsed >= self.HEARTBEAT_FIRST_S and (now - last) >= interval:
                self._heartbeat_last[name] = now
                silence_s = max(0.0, now - last_io)
                if pid:
                    # Determine whether the process looks alive.
                    # A live model call typically shows near-zero CPU while
                    # blocked on a network response but its pid still exists.
                    # We trust the process is alive unless cpu has been
                    # reported as exactly zero for a long time.
                    try:
                        cpu = float(cpu_str) if cpu_str and cpu_str not in ("", "?") else -1.0
                    except Exception:
                        cpu = -1.0
                    # "looks dead" = cpu was reported as exactly 0.0 AND
                    # we've been waiting a very long time. Near-zero (e.g.
                    # 0.9%) is normal for a blocked network call.
                    looks_dead = (cpu == 0.0 and elapsed > 300)
                    if total_bytes == 0:
                        if not looks_dead:
                            # Normal slow model call — no alarm.
                            flavor = (
                                f"awaiting model response "
                                f"({elapsed:.0f}s, cpu={cpu_str or '?'}%)"
                            )
                            level = "INFO"
                        else:
                            flavor = (
                                f"no output for {elapsed:.0f}s, cpu=0% "
                                f"-- process may be dead; /abort to kill and retry"
                            )
                            level = "WARN"
                    elif silence_s > 5.0:
                        flavor = (
                            f"streaming idle {silence_s:.0f}s "
                            f"({total_bytes}b received, last='{(last_line or '-')[:60]}')"
                        )
                        level = "INFO"
                    else:
                        flavor = f"streaming ({total_bytes}b received)"
                        level = "INFO"
                    note = f"pid={pid} :: {flavor}"
                else:
                    note = "no subprocess yet (provider initializing)"
                    level = "INFO"
                self.state.add_log(
                    f"{name} :: working {elapsed:.0f}s :: {note}",
                    level=level,
                )
        # Forget heartbeats for agents that have finished, so the next
        # WORKING period starts fresh.
        with self.state.lock:
            still_working = {n for n, c in self.state.agents.items() if c.status == AgentStatus.WORKING}
        for name in list(self._heartbeat_last.keys()):
            if name not in still_working:
                self._heartbeat_last.pop(name, None)

    def _input_row_count(self, buf: str, width: int) -> int:
        """Number of rows the input field needs to show ``buf`` in full.

        Uses character wrap (not word wrap) at ``width - 1`` so cursor
        positioning is straightforward. Capped at INPUT_MAX_ROWS; past
        that the buffer scrolls (last N rows visible).
        """
        prompt_len = 2  # "> "
        total = prompt_len + len(buf)
        eff = max(1, width - 1)
        rows = max(1, (total + eff - 1) // eff)
        return min(self.INPUT_MAX_ROWS, rows)

    def _draw_title(self, stdscr, width: int) -> None:
        s = self.state
        with s.lock:
            project = s.project_name
            provider = s.provider
            phase = s.phase
            busy = s.busy
            iteration = s.iteration_count
            tot = s.total_tokens
            tot_in = s.total_input_tokens
            tot_out = s.total_output_tokens
            files = s.total_files
            usd = s.total_usd
            ahead = s.github_ahead
        spin = self._spinner[self._spinner_idx] if busy else " "
        cost_str = format_usd(usd)
        push_str = f" :: ↑{ahead}" if ahead > 0 else ""
        title = (
            f" CLK :: {project} :: provider={provider} :: phase={phase} {spin} "
            f"iter={iteration} :: tok={_format_tokens(tot)} "
            f"(in={_format_tokens(tot_in)}/out={_format_tokens(tot_out)}) :: "
            f"{cost_str} :: files={files}{push_str} "
        )
        self._fill(stdscr, 0, 0, width, " ", curses.color_pair(self.COLOR_TITLE) | curses.A_BOLD)
        self._safe_addstr(
            stdscr, 0, 0, title.ljust(width - 1)[: width - 1], width,
            curses.color_pair(self.COLOR_TITLE) | curses.A_BOLD,
        )

    def _draw_agent_grid(self, stdscr, *, top: int, height: int, width: int) -> int:
        with self.state.lock:
            # Sort: baseline first (chief leading), then alphabetical
            # dynamics. Keeps the visual anchor stable as roles come and go.
            def _sort_key(name: str) -> tuple:
                card = self.state.agents[name]
                base = 0 if card.is_baseline else 1
                # chief always first among baseline
                pri = 0 if name == "chief" else 1
                return (base, pri, name)

            names = sorted(self.state.agents.keys(), key=_sort_key)
            cards = [self.state.agents[n] for n in names]
        if not cards:
            return top
        cols = 4 if width >= 84 else (3 if width >= 64 else 2)
        rows = (len(cards) + cols - 1) // cols
        max_grid_height = max(1, height - 12)
        # Prefer 6 (room for two rotating telemetry rows), shrink as
        # vertical real estate runs out.
        preferred = 6
        card_h = preferred
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

    # Rotating telemetry views. Each view returns up to two
    # ``(label, value)`` pairs which fill the available rows below the
    # bar. Cycle period is 2.5s.
    _TELEMETRY_PERIOD_S = 2.5

    def _telemetry_views(self, card: AgentCard) -> List[List[Tuple[str, str]]]:
        prov = card.provider or "(default)"
        baseline = "baseline" if card.is_baseline else "dynamic"
        files_line = ", ".join(card.files_written[:4]) or "-"
        if len(card.files_written) > 4:
            files_line += f" (+{len(card.files_written)-4})"
        # View 1: live work
        live = [
            ("task", card.current_task or "-"),
            ("rsp", card.provider_resolution or card.last_result or card.last_error or "-"),
        ]
        # View 2: I/O
        io = [
            ("prompt", card.prompt_preview or "-"),
            ("files", f"{files_line} (total {card.total_files})"),
        ]
        # View 3: thinking + meta (incl. tokens)
        tok_str = (
            f"tok={_format_tokens(card.total_tokens)}"
            f" (in={_format_tokens(card.input_tokens)}"
            f"/out={_format_tokens(card.output_tokens)})"
        )
        if card.last_usage_source:
            tok_str += f"[{card.last_usage_source}]"
        meta_line = f"{baseline} | runs={card.runs} | dur={card.last_duration_s:.1f}s | prov={prov}"
        thinking = [
            ("think", card.last_thought or card.role or "-"),
            ("meta", meta_line),
        ]
        # View 4: token usage details
        tokens = [
            ("tokens", tok_str),
            ("source", f"last={card.last_usage_source or '-'}"),
        ]
        # View 5: live subprocess telemetry. Useful when WORKING - the
        # user can see PID + bytes flowing + the most recent stderr/
        # stdout line, so they know the underlying CLI is alive vs.
        # genuinely stuck.
        live_label = (
            f"pid={card.live_pid}" if card.live_pid
            else ("idle" if card.status != AgentStatus.WORKING else "no_pid")
        )
        live_meta = (
            f"out={card.live_stdout_chars}b "
            f"err={card.live_stderr_chars}b "
            f"cpu={card.live_cpu_pct or '?'}% "
            f"{live_label}"
        )
        subprocess_view = [
            ("live", card.live_last_line or "-"),
            ("io", live_meta),
        ]
        return [live, io, thinking, tokens, subprocess_view]

    def _draw_card(self, stdscr, y: int, x: int, h: int, w: int, card: AgentCard) -> None:
        col = {
            AgentStatus.IDLE: self.COLOR_IDLE,
            AgentStatus.WORKING: self.COLOR_WORKING,    # yellow: waiting for response
            AgentStatus.RECOVERING: self.COLOR_FAILED,  # red: retry backoff in progress
            AgentStatus.DONE: self.COLOR_DONE,
            AgentStatus.FAILED: self.COLOR_FAILED,
            AgentStatus.PROVIDER_ERROR: self.COLOR_FAILED,
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
        # Header line: name (with baseline marker) + status badge
        name_label = ("*" if card.is_baseline else " ") + card.name
        name_str = name_label[:inner_w]
        self._safe_addstr(stdscr, y + 1, x + 2, name_str, inner_w, attr | curses.A_BOLD)
        badge = f"[{card.status:<7}] r={card.runs:<3}"
        if len(name_str) + 1 + len(badge) <= inner_w:
            self._safe_addstr(stdscr, y + 1, x + w - 2 - len(badge), badge, inner_w, attr)
        # Activity meter.
        # WORKING: a 3-cell ping-pong cursor sweeping the bar; speed
        #          accelerates as the run goes long, so a stuck call
        #          looks visibly more frantic than a fresh one.
        # IDLE/DONE/FAILED: bar is filled proportional to this agent's
        #          most recent run's token count divided by the global
        #          peak run, so visual bar length = relative work.
        bar_len = max(0, inner_w - 2)
        if bar_len > 0:
            with self.state.lock:
                peak = self.state.peak_run_tokens
            if card.status == AgentStatus.WORKING:
                now = time.monotonic()
                elapsed = max(0.0, now - card.last_started_mono)
                # Fast when tokens are arriving, slow when waiting for the
                # model to reply. idle_since measures time since the last
                # stdout/stderr line from the subprocess.
                idle_since = (
                    max(0.0, now - card.live_last_update_mono)
                    if card.live_last_update_mono > 0 else elapsed
                )
                speed = 8.0 if idle_since < 2.0 else 0.8
                cursor_w = 3
                travel = max(1, bar_len - cursor_w)
                step = int(elapsed * speed) % (travel * 2)
                pos = step if step < travel else (travel * 2 - step)
                bar_chars = ["."] * bar_len
                for k in range(cursor_w):
                    if 0 <= pos + k < bar_len:
                        bar_chars[pos + k] = "="
                bar = "".join(bar_chars)
            else:
                if peak > 0 and card.last_run_tokens > 0:
                    ratio = min(1.0, card.last_run_tokens / float(peak))
                else:
                    ratio = 0.0
                filled = int(round(bar_len * ratio))
                bar = "#" * filled + "." * (bar_len - filled)
            self._safe_addstr(stdscr, y + 2, x + 2, bar, inner_w, attr)

        # Card body rows below the bar:
        #   h=4 -> 1 row (overlaps bottom border, kept for tiny terms)
        #   h=5 -> 1 row of telemetry
        #   h>=6 -> 1 always-on "live" row + 1 rotating row
        # The always-on live row is critical when the user is staring at
        # a long agent call: it keeps the elapsed-time + last subprocess
        # line on screen continuously, instead of cycling away every
        # 2.5s with the rest of the rotating views.
        if h <= 4:
            avail_rows = 1
            row_origin = y + 3
        elif h == 5:
            avail_rows = 1
            row_origin = y + 3
        else:
            avail_rows = 2
            row_origin = y + 3

        dim = curses.A_DIM | curses.color_pair(self.COLOR_LOG_INFO)

        if card.status == AgentStatus.WORKING and avail_rows >= 2:
            # Top row: live elapsed + last subprocess line, always shown
            # while WORKING so the user can see motion at a glance.
            elapsed = max(0.0, time.monotonic() - card.last_started_mono)
            live_head = (
                f"{elapsed:5.1f}s "
                + (f"pid={card.live_pid} " if card.live_pid else "        ")
                + (card.live_last_line or "awaiting...").strip()
            )
            self._safe_addstr(stdscr, row_origin, x + 2, live_head[:inner_w], inner_w, attr)
            # Bottom row: rotating telemetry as before.
            views = self._telemetry_views(card)
            tick = int(time.monotonic() / self._TELEMETRY_PERIOD_S) % len(views)
            view = views[tick]
            if view:
                label, value = view[0]
                text = f"{label}: {value}"
                self._safe_addstr(stdscr, row_origin + 1, x + 2, text[:inner_w], inner_w, dim)
            return

        views = self._telemetry_views(card)
        tick = int(time.monotonic() / self._TELEMETRY_PERIOD_S) % len(views)
        view = views[tick]
        view = view[:avail_rows]
        for i, (label, value) in enumerate(view):
            if i == 0 and avail_rows == 1:
                line = (card.current_task or card.last_result or card.last_error or "-")
                self._safe_addstr(stdscr, row_origin + i, x + 2, line[:inner_w], inner_w, attr)
                continue
            text = f"{label}: {value}"
            self._safe_addstr(stdscr, row_origin + i, x + 2, text[:inner_w], inner_w, dim)

    def _draw_idea(self, stdscr, *, top: int, width: int) -> int:
        with self.state.lock:
            idea = self.state.idea
            convo_tail = self.state.conversation[-1:] if self.state.conversation else []
        # Wrap both lines so the user sees the full content rather than
        # a truncated head. Cap each block at 3 visual rows so the agent
        # grid below has room to breathe.
        line = "idea: " + (idea or "(no idea yet - type one below)")
        idea_rows = _word_wrap(line, max(20, width - 1))[:3]
        for i, row in enumerate(idea_rows):
            self._safe_addstr(stdscr, top + i, 0, row, width, curses.A_BOLD)
        cursor = top + len(idea_rows) - 1
        if convo_tail:
            role, text = convo_tail[-1]
            tail_rows = _word_wrap(f"last [{role}]: {text}", max(20, width - 1))[:3]
            for i, row in enumerate(tail_rows):
                self._safe_addstr(stdscr, cursor + 1 + i, 0, row, width)
            cursor += len(tail_rows)
        return cursor

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
        # Word-wrap each log entry. The first wrapped row carries the
        # full ``HH:MM:SS [LEVEL] ...`` prefix; continuation rows are
        # indented so the eye can group them with their parent entry.
        inner_w = max(10, width - 2)
        flat: List[Tuple[int, str]] = []  # (level_attr, text)
        for line in lines:
            attr = self._log_attr(line.level)
            head = f"{line.ts} [{line.level}] {line.text}"
            # Derive the continuation indent from the actual prefix length so
            # wrapped lines align regardless of level-name width (INFO vs DEBUG).
            cont_indent = " " * (len(line.ts) + len(line.level) + 4)  # 4 = " [" + "] "
            wrapped = _word_wrap(head, inner_w)
            for k, row in enumerate(wrapped):
                flat.append((attr, row if k == 0 else cont_indent + row.lstrip()))
        # Scroll offset is in *visual* rows now, not log entries.
        total = len(flat)
        offset = max(0, min(self.scroll_offset, max(0, total - body_height)))
        end = total - offset
        start = max(0, end - body_height)
        slice_ = flat[start:end]
        for i, (attr, text) in enumerate(slice_):
            self._safe_addstr(stdscr, body_top + i, 1, text[:inner_w], width, attr)
        return body_top + body_height - 1

    def _draw_totals(self, stdscr, *, top: int, width: int) -> None:
        with self.state.lock:
            tot = self.state.total_tokens
            tin = self.state.total_input_tokens
            tout = self.state.total_output_tokens
            files = self.state.total_files
            agents = len(self.state.agents)
        peak = self.state.peak_run_tokens
        line = (
            f" totals :: agents={agents} :: tokens={_format_tokens(tot)} "
            f"(in={_format_tokens(tin)} / out={_format_tokens(tout)}) "
            f":: peak_run={_format_tokens(peak)} :: files={files} "
        )
        attr = curses.color_pair(self.COLOR_LOG_SYS) | curses.A_BOLD
        # Pad to width so the line reads as a band rather than a phrase.
        self._safe_addstr(stdscr, top, 0, line.ljust(width - 1)[: width - 1], width, attr)

    def _hint_for_state(self) -> str:
        """Compute the one-line hint that goes above the input prompt.

        Looks at the current dashboard state (idea captured? agent
        working? last error?) and returns a short suggestion. Returns
        an empty string when there's nothing useful to say.
        """
        s = self.state
        with s.lock:
            has_idea = bool(s.idea)
            busy = s.busy
            err_kind = s.last_error_kind
            in_tut = s.in_tutorial
            stop_req = s.stop_requested
            active_provider = s.provider or "shell"
        if in_tut:
            return "tutorial running — agents are operating in a sandbox; press /quit to exit"
        if busy:
            return "agent is working — /abort to kill the stuck subprocess if needed"
        if stop_req:
            return "stop requested — loop will end after the current cycle"
        if err_kind == "not_installed":
            return f"{active_provider} CLI not found — try /install {active_provider} or /provider <other>"
        if err_kind == "auth":
            return f"{active_provider} auth failed — /configure {active_provider} to set credentials"
        if err_kind == "rate_limit":
            return "provider rate-limited — wait, or /provider <other> to switch"
        if err_kind == "timeout":
            return "provider call stalled — /abort to kill it, then retry or switch provider"
        if not has_idea:
            return "type your idea, or /tutorial for a sample run, or /help"
        return "type a message to continue, or /help for commands"

    def _draw_hint_bar(self, stdscr, *, top: int, width: int) -> None:
        hint = self._hint_for_state()
        if not hint:
            return
        text = ("  " + hint).ljust(width - 1)[: width - 1]
        # Subtle styling — log info color, no bold — so the hint never
        # competes with the title bar or the cards for attention.
        self._safe_addstr(stdscr, top, 0, text, width, curses.color_pair(self.COLOR_LOG_INFO))

    # ----- /help overlay ----------------------------------------------------

    _HELP_ROWS: List[Tuple[str, str]] = [
        ("/help, F1, ?",            "Toggle this overlay."),
        ("Esc, q",                  "Dismiss the overlay."),
        ("",                        ""),
        ("/idea <text>",            "Replace the captured idea."),
        ("/cast",                   "Have the chief re-pick the team."),
        ("/roles list|add|drop",    "Inspect or edit the agent roster."),
        ("/run [workflow]",         "Run one workflow cycle (default: engineering)."),
        ("/loop ralph|autoresearch [N]", "Refinement loop for N iterations."),
        ("/stop",                   "Stop the active loop after the current cycle."),
        ("/gauntlet on|off|PRESET",  "Toggle the gauntlet loop (preset: quick|standard|rigorous)."),
        ("/abort",                  "Hard-kill the running provider subprocess."),
        ("",                        ""),
        ("/provider <name>",        "Switch the active provider."),
        ("/install [tool]",         "Install a missing CLI (claude, pi, ollama, …)."),
        ("/configure [tool]",       "Re-run a tool's first-use config."),
        ("/github",                 "(Re-)connect this workspace to a GitHub remote."),
        ("",                        ""),
        ("/doctor [--fix]",         "Health-check providers and config."),
        ("/diag",                   "Build a redacted diagnostic bundle."),
        ("/tutorial",               "Run a free sample idea on the shell provider."),
        ("/workspaces list|switch|rename|clean", "Manage past kickoff dirs."),
        ("/undo",                   "Revert the last clk-authored commit."),
        ("/status",                 "Print a snapshot to the log."),
        ("/quit, Ctrl-D",           "Exit the TUI."),
    ]

    def _draw_help_overlay(self, stdscr, h: int, w: int) -> None:
        rows = self._HELP_ROWS
        # Box dimensions: leave at least 4 cells of margin on each side
        # so the underlying log/cards are still partially visible.
        box_w = max(50, min(w - 4, 84))
        box_h = min(h - 2, len(rows) + 5)
        y0 = max(1, (h - box_h) // 2)
        x0 = max(1, (w - box_w) // 2)
        attr = curses.color_pair(self.COLOR_LOG_SYS) | curses.A_BOLD
        bg = curses.color_pair(self.COLOR_TITLE)
        # Top + bottom borders, side walls.
        self._safe_addstr(stdscr, y0, x0, "+" + "-" * (box_w - 2) + "+", box_w, attr)
        for r in range(1, box_h - 1):
            self._safe_addstr(stdscr, y0 + r, x0, "|" + " " * (box_w - 2) + "|", box_w, attr)
        self._safe_addstr(stdscr, y0 + box_h - 1, x0, "+" + "-" * (box_w - 2) + "+", box_w, attr)
        # Title.
        title = " CLK :: help "
        self._safe_addstr(
            stdscr, y0, x0 + max(1, (box_w - len(title)) // 2), title, box_w, bg | curses.A_BOLD
        )
        # Body.
        col_w = (box_w - 6) // 3
        for i, (cmd, desc) in enumerate(rows[: box_h - 4]):
            line = f"  {cmd:<{col_w}} {desc}"[: box_w - 3]
            self._safe_addstr(stdscr, y0 + 2 + i, x0 + 1, line, box_w - 2, curses.color_pair(self.COLOR_LOG_INFO))
        # Footer hint.
        footer = " Esc / q to close "
        self._safe_addstr(
            stdscr, y0 + box_h - 1,
            x0 + max(1, (box_w - len(footer)) // 2),
            footer, box_w, bg | curses.A_BOLD,
        )

    def _draw_input(self, stdscr, *, top: int, width: int, rows: int = 1, cursor: int = 0) -> None:
        # Frame line above the input rows.
        self._safe_addstr(
            stdscr, top, 0, "-" * (width - 1), width, curses.color_pair(self.COLOR_FRAME)
        )
        with self.state.lock:
            buf = self.state.input_buffer
            cursor = max(0, min(cursor, len(buf)))
        prompt = "> "
        full = prompt + buf
        eff = max(1, width - 1)
        # Character-wrap so cursor math is exact even when the user
        # types continuous strings (URLs, paste).
        chunks_all = [full[i:i + eff] for i in range(0, max(eff, len(full)), eff)] or [""]
        cursor_abs = len(prompt) + cursor
        cursor_chunk = min(len(chunks_all) - 1, cursor_abs // eff)
        first_chunk = max(0, min(cursor_chunk, len(chunks_all) - rows))
        # If the buffer needs more rows than we have, show the LAST
        # ``rows`` chunks containing the cursor so the cursor stays visible.
        chunks = chunks_all[first_chunk:first_chunk + rows]
        attr = curses.color_pair(self.COLOR_PROMPT) | curses.A_BOLD
        base_y = top + 1
        for i, line in enumerate(chunks):
            self._safe_addstr(stdscr, base_y + i, 0, line, width, attr)
        # Cursor goes after the last visible character.
        try:
            rel = cursor_abs - first_chunk * eff
            cursor_y = base_y + max(0, min(rows - 1, rel // eff))
            cursor_x = max(0, min(rel % eff, width - 2))
            stdscr.move(cursor_y, cursor_x)
        except Exception:
            pass

    # --- input handling --------------------------------------------------

    def _handle_key(self, ch: int) -> bool:
        # When the /help overlay is up, Esc/q dismisses it and all
        # other keys (including arrows) pass through normally. This
        # lets agent state continue updating beneath the overlay.
        if self._help_visible:
            if ch in (27, ord('q'), ord('Q')):  # Esc / q
                self._help_visible = False
                with self.state.lock:
                    self.state.help_dismissed = True
                return True
        # F1 always toggles help — no need to clear the input buffer.
        if ch == curses.KEY_F1:
            self._help_visible = not self._help_visible
            return True
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
        if ch == curses.KEY_LEFT:
            with self.state.lock:
                self.state.input_cursor = max(0, self.state.input_cursor - 1)
            return True
        if ch == curses.KEY_RIGHT:
            with self.state.lock:
                self.state.input_cursor = min(len(self.state.input_buffer), self.state.input_cursor + 1)
            return True
        if ch in (curses.KEY_SLEFT, 1):  # Shift-left where supported, Ctrl-A
            with self.state.lock:
                self.state.input_cursor = 0
            return True
        if ch in (curses.KEY_SRIGHT, 5):  # Shift-right where supported, Ctrl-E
            with self.state.lock:
                self.state.input_cursor = len(self.state.input_buffer)
            return True
        if ch == curses.KEY_DC:
            with self.state.lock:
                i = self.state.input_cursor
                if i < len(self.state.input_buffer):
                    self.state.input_buffer = self.state.input_buffer[:i] + self.state.input_buffer[i + 1:]
            return True
        if ch in (curses.KEY_BACKSPACE, 127, 8):
            with self.state.lock:
                i = self.state.input_cursor
                if i > 0:
                    self.state.input_buffer = self.state.input_buffer[:i - 1] + self.state.input_buffer[i:]
                    self.state.input_cursor = i - 1
            return True
        if ch in (10, 13, curses.KEY_ENTER):
            with self.state.lock:
                msg = self.state.input_buffer
                self.state.input_buffer = ""
                self.state.input_cursor = 0
            if msg.strip():
                return self._dispatch(msg.strip())
            return True
        if 32 <= ch < 127:
            # When the input buffer is empty, ? opens /help instead of
            # being typed — same convention as many CLIs.
            if ch == ord('?'):
                with self.state.lock:
                    empty = (self.state.input_buffer == "")
                if empty:
                    self._help_visible = True
                    return True
            with self.state.lock:
                if len(self.state.input_buffer) < 1024:
                    i = max(0, min(self.state.input_cursor, len(self.state.input_buffer)))
                    self.state.input_buffer = self.state.input_buffer[:i] + chr(ch) + self.state.input_buffer[i:]
                    self.state.input_cursor = i + 1
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
            if cmd in ("mission", "auto"):
                if args and not self.state.idea:
                    self.worker.submit(Job("idea", " ".join(args)))
                self.worker.submit(Job("mission"))
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
            if cmd == "gauntlet":
                self.worker.submit(Job("gauntlet", args[0] if args else ""))
                return True
            if cmd == "cast":
                self.worker.submit(Job("cast"))
                return True
            if cmd == "abort":
                self._do_abort()
                return True
            if cmd == "help":
                self._help_visible = True
                return True
            if cmd == "install":
                tool = args[0] if args else (self.state.provider or "")
                self.worker.submit(Job("install", tool))
                return True
            if cmd == "configure":
                tool = args[0] if args else (self.state.provider or "")
                self.worker.submit(Job("configure", tool))
                return True
            if cmd == "github":
                self.worker.submit(Job("github"))
                return True
            if cmd == "undo":
                # Two-step confirm: first /undo prints the diff, second
                # /undo confirm actually reverts. Matches the
                # always-confirm policy.
                confirm = bool(args and args[0].lower() == "confirm")
                self.worker.submit(Job("undo", {"confirm": confirm}))
                return True
            if cmd == "doctor":
                fix = bool(args and args[0] == "--fix")
                self.worker.submit(Job("doctor", {"fix": fix}))
                return True
            if cmd == "diag":
                self.worker.submit(Job("diag"))
                return True
            if cmd == "tutorial":
                self.worker.submit(Job("tutorial"))
                return True
            if cmd == "workspaces":
                action = args[0] if args else "list"
                rest = args[1:]
                self.worker.submit(Job("workspaces", {"action": action, "args": rest}))
                return True
            if cmd == "roles":
                if not args:
                    self.worker.submit(Job("roles", {"action": "list"}))
                    return True
                sub = args[0].lower()
                if sub == "list":
                    self.worker.submit(Job("roles", {"action": "list"}))
                elif sub in ("add", "drop", "remove"):
                    name = args[1] if len(args) > 1 else ""
                    role_text = " ".join(args[2:]).strip().strip('"') if len(args) > 2 else ""
                    op = "add" if sub == "add" else "remove"
                    self.worker.submit(Job("roles", {"action": op, "name": name, "role": role_text}))
                else:
                    self.state.add_log(f"unknown roles op: {sub}", level="WARN")
                return True
            self.state.add_log(
                f"'/{cmd}' isn't a command I know. Type /help (or press F1) for the list.",
                level="WARN",
            )
            return True
        # Free-text: first message becomes the idea; subsequent ones are
        # appended to the conversation file and trigger another run.
        # The engineering workflow's first stage IS chief casting (the
        # chief decomposes + casts the team + authors the workflow YAML
        # all in one call), so we deliberately do NOT submit a separate
        # Job("cast") here. Doing so would invoke chief twice back-to-
        # back per user message and was the cause of the "chief stuck
        # at 90+ seconds" symptom. /cast remains as an explicit manual
        # trigger when you want a re-cast without running engineering.
        # Autonomy by default: a free-text message drives the full autonomous
        # mission (charter -> plan -> phases -> code-gated done) rather than a
        # single workflow pass. Use /run for a one-shot engineering cycle.
        if not self.state.idea:
            self.worker.submit(Job("idea", msg))
            self.worker.submit(Job("mission"))
        else:
            self._append_conversation(msg)
            self.worker.submit(Job("mission"))
        return True

    def _do_abort(self) -> None:
        """Send SIGTERM (then SIGKILL) to every WORKING agent's subprocess.

        Runs on the curses thread, NOT the worker thread, because the
        worker is the one blocked inside ``provider.invoke()``. Killing
        the subprocess unblocks ``proc.wait()`` in run_streaming, which
        causes the provider to return an error response, which the
        worker treats as a normal failed run.
        """
        import os
        import signal
        with self.state.lock:
            targets = [
                (name, card.live_pid)
                for name, card in self.state.agents.items()
                if card.status == AgentStatus.WORKING and card.live_pid
            ]
        if not targets:
            self.state.add_log(
                "nothing to abort — no agent subprocess is running right now.",
                level="WARN",
            )
            return
        self.state.add_system_message(
            f"aborting {len(targets)} stuck subprocess(es)…"
        )
        for name, pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
                self.state.add_log(
                    f"sent SIGTERM to {name} (pid {pid}) — the cycle will report a timeout", level="WARN"
                )
            except ProcessLookupError:
                self.state.add_log(f"abort: {name} pid={pid} already gone", level="INFO")
            except Exception as exc:
                self.state.add_log(f"abort: {name} pid={pid} failed: {exc}", level="ERROR")

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

    # Crashed-session detection: if a previous TUI exited without removing
    # its lock file, mention that and offer the user a clean restart hint.
    # We don't auto-resume the conversation here — that's the worker's job
    # if the user types something — but we do surface the situation so
    # they know what happened.
    lock_path = paths.state / ".tui-active"
    prior_session = None
    try:
        if lock_path.exists():
            prior_pid = lock_path.read_text(encoding="utf-8").strip()
            # Stale lock if the PID no longer exists.
            try:
                import os as _os
                if prior_pid.isdigit():
                    _os.kill(int(prior_pid), 0)
                    # Still running — leave the file alone, don't claim it.
                    print(
                        f"[tui] another CLK TUI is already running (pid {prior_pid}); "
                        f"close it first or rm -f {lock_path}",
                        file=sys.stderr,
                    )
                    return 2
            except (OSError, ValueError):
                prior_session = prior_pid
        paths.state.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(str(__import__('os').getpid()), encoding="utf-8")
    except Exception as _exc:
        logger.debug("could not write session lock: %s", _exc)

    state = DashboardState(agent_names, paths=paths, agents_cfg=agents_cfg)
    state.project_name = clk_cfg.get("project_name") or paths.root.name
    state.provider = providers_cfg.get("active") or clk_cfg.get("default_provider") or "shell"
    if prior_session:
        state.add_log(
            f"recovered from a crashed session (prior pid {prior_session}). "
            f"Your conversation is preserved under .clk/state/conversation.md.",
            level="WARN",
        )
    # Mirror every status-pane line to a persistent file so we have a
    # full session trace inside the kickoff dir for later analysis.
    state.attach_session_log(paths.logs / "session.log")

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

    # Pre-populate from existing idea if any.
    idea_path = paths.state / "idea.json"
    if idea_path.exists():
        try:
            payload = json.loads(idea_path.read_text(encoding="utf-8"))
            state.set_idea(payload.get("statement") or payload.get("title") or "")
        except Exception as exc:
            log_exception("tui.run.load_idea", exc)

    # Route every stderr/stdout write into the dashboard log pane BEFORE
    # the worker starts processing jobs, so even the very first job's
    # output (subprocess, log(), traceback.print_exc()) cannot reach the
    # real terminal and corrupt the curses display. The original streams
    # are restored on exit so post-TUI shell output looks normal again.
    old_stderr = sys.stderr
    old_stdout = sys.stdout
    sys.stderr = _StreamToLog(state, default_level="INFO")
    sys.stdout = _StreamToLog(state, default_level="INFO")
    worker.start()

    if initial_prompt:
        worker.submit(Job("idea", initial_prompt))
        worker.submit(Job("run", "engineering"))

    app = TuiApp(state, worker)
    try:
        app.run()
    finally:
        worker.stop()
        worker.join(timeout=2.0)
        sys.stderr = old_stderr
        sys.stdout = old_stdout
        state.close_session_log()
        # Clean up the lock so the next run doesn't see "crashed session".
        try:
            if lock_path.exists():
                lock_path.unlink()
        except Exception as _exc:
            logger.debug("could not remove session lock: %s", _exc)
    return 0
