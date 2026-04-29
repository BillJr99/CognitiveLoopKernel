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
    RoleProposal,
    WorkflowRunner,
    casting_objective,
    is_baseline,
    list_roles,
    load_workflow,
    register_role,
    remove_role,
    render_roster_summary,
)
from .utils.logging_utils import log_exception


# ---------------------------------------------------------------------------
# stderr/stdout capture
# ---------------------------------------------------------------------------


class _StreamToLog:
    """File-like object that routes writes into the dashboard log pane.

    Replaces ``sys.stderr`` (and optionally ``sys.stdout``) while the TUI
    is active so that ``log()`` calls, ``print()`` statements, and
    ``traceback.print_exc()`` output never reach the terminal directly
    and therefore never corrupt the curses screen.

    Lines are buffered until a ``\\n`` is seen so partial writes from
    ``print(..., end="")`` don't produce noisy fragments. Severity is
    inferred from the ``[LEVEL]`` tag the harness's own logger emits;
    everything else falls back to the default level.
    """

    LEVEL_TAGS = ("[ERROR]", "[WARN]", "[INFO]")

    def __init__(self, state: "DashboardState", default_level: str = "INFO") -> None:
        self.state = state
        self.default_level = default_level
        self._buf = ""

    def write(self, s: str) -> int:
        if not s:
            return 0
        self._buf += s
        out_len = len(s)
        while "\n" in self._buf:
            line, _, self._buf = self._buf.partition("\n")
            line = line.rstrip("\r")
            if not line.strip():
                continue
            level = self.default_level
            for tag in self.LEVEL_TAGS:
                if tag in line:
                    level = tag.strip("[]")
                    break
            try:
                self.state.add_log(line[:300], level=level)
            except Exception:
                # Last resort: never raise from a stream.write.
                pass
        return out_len

    def flush(self) -> None:
        if self._buf.strip():
            try:
                self.state.add_log(self._buf[:300], level=self.default_level)
            except Exception:
                pass
            self._buf = ""

    def isatty(self) -> bool:
        return False

    def writable(self) -> bool:
        return True

    def fileno(self):  # raise so subprocess.* doesn't grab us
        raise OSError("StreamToLog has no fileno")


# ---------------------------------------------------------------------------
# State
# ---------------------------------------------------------------------------


def _extract_thought(text: str) -> str:
    """Pull a single 'thinking' line out of an agent's response.

    Scans for common markers (Q:, Hypothesis:, Decision:, PROPOSE_ROLE:,
    PROPOSE_WORKFLOW:, Risk:, Next:) and returns the first match. Used
    as the rotating ``thought`` view in the agent cards.
    """
    if not text:
        return ""
    markers = (
        "Q:",
        "Question:",
        "Hypothesis:",
        "Decision:",
        "Risk:",
        "Risks:",
        "Next:",
        "PROPOSE_ROLE:",
        "PROPOSE_WORKFLOW:",
    )
    for line in text.splitlines():
        s = line.strip()
        for m in markers:
            if s.startswith(m):
                return s[:240]
    return ""


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
    # Telemetry surfaced by the rotating in-card panes.
    prompt_preview: str = ""
    response_preview: str = ""
    files_written: List[str] = field(default_factory=list)
    last_thought: str = ""
    provider: str = ""
    role: str = ""
    is_baseline: bool = False
    roster_status: str = ""  # latest roster_changed status, e.g. "added"
    # Token accounting (cumulative across all runs of this agent).
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    last_run_tokens: int = 0   # tokens in the most recent run only
    last_usage_source: str = ""
    total_files: int = 0  # cumulative file write count
    # Live subprocess telemetry (set by streaming providers via the
    # ``progress`` observer hook). ``live_pid`` is non-zero while the
    # underlying CLI subprocess is alive; ``live_last_line`` is the
    # most recent stderr/stdout line so the user can see real activity
    # in the card and the log pane rather than a stalled spinner.
    live_pid: int = 0
    live_last_line: str = ""
    live_stdout_chars: int = 0
    live_stderr_chars: int = 0
    live_last_update_mono: float = 0.0


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _word_wrap(text: str, width: int) -> List[str]:
    """Wrap ``text`` to ``width`` columns, breaking on word boundaries.

    Falls back to mid-word splits when a single token exceeds width
    (e.g. a long URL). Empty input returns ``[""]`` so callers can rely
    on at least one row.
    """
    if width <= 1:
        return [text]
    if not text:
        return [""]
    out: List[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if len(word) > width:
                # Hard-break the giant token. Flush whatever we have first.
                if current:
                    out.append(current)
                    current = ""
                while len(word) > width:
                    out.append(word[:width])
                    word = word[width:]
                current = word
                continue
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out or [""]


@dataclass
class LogLine:
    ts: str
    level: str  # INFO | WARN | ERROR | USER | SYSTEM
    text: str


class DashboardState:
    """Thread-safe state shared between the UI thread and the worker."""

    def __init__(self, agent_names: List[str], *, paths: Optional[Paths] = None,
                 agents_cfg: Optional[Dict[str, Any]] = None) -> None:
        self.lock = threading.Lock()
        self.paths = paths
        self.agents: Dict[str, AgentCard] = {}
        for n in agent_names:
            cfg = ((agents_cfg or {}).get("agents") or {}).get(n) or {}
            self.agents[n] = AgentCard(
                name=n,
                role=cfg.get("role", ""),
                provider=cfg.get("provider") or "",
                is_baseline=is_baseline(n),
            )
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
        # Project-wide token + file totals (sum across all agents).
        self.total_input_tokens: int = 0
        self.total_output_tokens: int = 0
        self.total_tokens: int = 0
        self.total_files: int = 0
        # Largest single-run token total observed across any agent.
        # The activity meter normalizes against this so a heavy agent
        # (engineer with 5k token responses) shows a long bar while a
        # light one (chief with 200 tokens) shows a short one.
        self.peak_run_tokens: int = 0
        # Session log file (mirror of the in-pane status log so we
        # have a persistent trace for later analysis).
        self.session_log_fh = None

    # ----- mutators (locked) --------------------------------------------

    def add_log(self, text: str, level: str = "INFO") -> None:
        # Multi-line writes (e.g. tracebacks routed through _StreamToLog
        # if buffering races) become one entry per line so the log pane
        # wraps each piece independently. Tabs and stray \r chars are
        # also normalized so the word-wrapper sees clean spaces.
        text = (text or "").replace("\r", "").replace("\t", "    ")
        if "\n" in text:
            for piece in text.split("\n"):
                if piece.strip():
                    self.add_log(piece, level=level)
            return
        line = LogLine(ts=datetime.now().strftime("%H:%M:%S"), level=level, text=text)
        with self.lock:
            self.log.append(line)
            fh = self.session_log_fh
        if fh is not None:
            try:
                fh.write(f"{datetime.now().isoformat(timespec='seconds')} [{level}] {text}\n")
                fh.flush()
            except Exception:
                # Never let a log write blow up the TUI.
                pass

    def attach_session_log(self, path: Path) -> None:
        """Open ``path`` in append mode and mirror every log line to it.

        Called once during TUI startup. The file persists across the
        run so a `.clk/logs/session.log` accumulates the project's full
        history of TUI events for later analysis (alongside
        casting.log, agent_memory.jsonl, and the git log).
        """
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            fh = path.open("a", encoding="utf-8")
            with self.lock:
                self.session_log_fh = fh
            fh.write(
                f"\n=== session start {datetime.now().isoformat(timespec='seconds')} ===\n"
            )
            fh.flush()
        except Exception:
            pass

    def close_session_log(self) -> None:
        with self.lock:
            fh = self.session_log_fh
            self.session_log_fh = None
        if fh is not None:
            try:
                fh.write(
                    f"=== session end {datetime.now().isoformat(timespec='seconds')} ===\n"
                )
                fh.close()
            except Exception:
                pass

    def begin_agent(self, name: str, objective: str) -> None:
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.status = AgentStatus.WORKING
            card.current_task = objective
            card.last_started_mono = time.monotonic()
            # Reset transient telemetry from the previous run.
            card.prompt_preview = ""
            card.response_preview = ""
            card.files_written = []
            card.last_thought = ""
            card.live_pid = 0
            card.live_last_line = ""
            card.live_stdout_chars = 0
            card.live_stderr_chars = 0
            card.live_last_update_mono = time.monotonic()
        self.add_log(f"{name} :: start :: {objective[:80]}", level="INFO")

    def report_progress(self, name: str, kind: str, message: str) -> None:
        """Capture streaming progress from a provider's subprocess.

        Updates the card's live_* fields and (selectively) emits log
        entries so the user can see what the underlying CLI is doing
        in real time. We log every stderr line (these are usually
        status / auth / error messages from claude/codex/gemini), the
        first 5 stdout lines (so very long responses don't flood the
        pane), and the start / end / timeout events.
        """
        message = (message or "").rstrip()
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            now = time.monotonic()
            card.live_last_update_mono = now
            if kind == "start":
                # parse "pid=NNNN cmd=..." into the live fields
                pid = 0
                for tok in message.split():
                    if tok.startswith("pid="):
                        try:
                            pid = int(tok.split("=", 1)[1])
                        except Exception:
                            pid = 0
                        break
                card.live_pid = pid
                card.live_last_line = "starting..."
            elif kind in ("stdout_line", "stderr_line"):
                card.live_last_line = message[:200]
                if kind == "stdout_line":
                    card.live_stdout_chars += len(message) + 1
                else:
                    card.live_stderr_chars += len(message) + 1
            elif kind in ("end", "timeout"):
                card.live_pid = 0
                card.live_last_line = f"{kind}: {message}"[:200]
        # Emit log entries for the high-signal events so the user sees
        # what's happening even when not looking at a card.
        if kind == "start":
            self.add_log(f"{name} :: subprocess {message}", level="SYSTEM")
        elif kind == "stderr_line" and message:
            self.add_log(f"{name} stderr: {message[:200]}", level="INFO")
        elif kind == "stdout_line":
            with self.lock:
                cnt = self.agents[name].live_stdout_chars
            # Only log the first few stdout lines per run so very long
            # model responses don't flood the pane.
            if cnt < 1024:
                self.add_log(f"{name} stdout: {message[:200]}", level="INFO")
        elif kind == "timeout":
            self.add_log(f"{name} :: TIMEOUT :: {message}", level="ERROR")
        elif kind == "end":
            self.add_log(f"{name} :: subprocess {message}", level="SYSTEM")

    def set_agent_prompt(self, name: str, prompt: str) -> None:
        # Keep a short head-of-prompt; the full prompt is on disk under
        # .clk/runs/. We only need a glanceable preview here.
        snippet = (prompt or "").strip()
        if not snippet:
            return
        # Strip the boilerplate header lines so the preview shows the
        # role-specific objective, not the same operating constraints
        # for every card.
        candidates = [l for l in snippet.splitlines() if l.strip()]
        head = " ".join(candidates[:6])
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.prompt_preview = head[:240]

    def end_agent(
        self,
        name: str,
        ok: bool,
        preview: str = "",
        error: str = "",
        files_written: Optional[List[str]] = None,
        usage: Optional[Dict[str, Any]] = None,
    ) -> None:
        files_written = list(files_written or [])
        usage = dict(usage or {})
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.status = AgentStatus.DONE if ok else AgentStatus.FAILED
            card.current_task = ""
            card.last_result = (preview or "").strip().replace("\n", " ")[:240]
            card.last_error = error[:240]
            card.response_preview = (preview or "").strip()[:400]
            card.files_written = files_written
            card.last_thought = _extract_thought(preview)
            card.runs += 1
            card.total_files += len(files_written)
            if card.last_started_mono:
                card.last_duration_s = time.monotonic() - card.last_started_mono
            in_tok = int(usage.get("input_tokens") or 0)
            out_tok = int(usage.get("output_tokens") or 0)
            tot_tok = int(usage.get("total_tokens") or (in_tok + out_tok))
            card.input_tokens += in_tok
            card.output_tokens += out_tok
            card.total_tokens += tot_tok
            card.last_run_tokens = tot_tok
            card.last_usage_source = str(usage.get("source") or card.last_usage_source)
            self.total_input_tokens += in_tok
            self.total_output_tokens += out_tok
            self.total_tokens += tot_tok
            self.total_files += len(files_written)
            if tot_tok > self.peak_run_tokens:
                self.peak_run_tokens = tot_tok
        self.add_log(
            f"{name} :: {'ok' if ok else 'fail'} :: "
            f"tok={_format_tokens(int(usage.get('total_tokens') or 0))} "
            f"files={len(files_written)} :: "
            f"{(preview or '').strip().splitlines()[0][:60] if preview else ''}",
            level="INFO" if ok else "WARN",
        )
        # File-action log lines: one INFO entry per file so the user
        # sees creation activity as it happens.
        for fpath in files_written:
            self.add_log(f"{name} :: wrote {fpath}", level="SYSTEM")

    def upsert_agent(self, name: str, *, role: str = "", baseline: bool = False, status: str = "added") -> None:
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            if role:
                card.role = role
            card.is_baseline = baseline
            card.roster_status = status

    def drop_agent(self, name: str) -> None:
        with self.lock:
            self.agents.pop(name, None)

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
    def __init__(self, state: "DashboardState") -> None:
        self.state = state

    def begin(self, agent: str, objective: str) -> None:
        self.state.begin_agent(agent, objective)

    def prompt_sent(self, agent: str, prompt: str) -> None:
        self.state.set_agent_prompt(agent, prompt)

    def end(self, agent: str, run) -> None:  # type: ignore[override]
        ok = bool(run.response.ok)
        preview = run.response.text or ""
        err = run.response.error or ""
        self.state.end_agent(
            agent,
            ok=ok,
            preview=preview,
            error=err,
            files_written=list(run.files_written or []),
            usage=dict(run.response.usage or {}),
        )

    def progress(self, agent: str, kind: str, message: str) -> None:
        self.state.report_progress(agent, kind, message)

    def log(self, line: str) -> None:
        self.state.add_log(line)

    def roster_changed(self, name: str, status: str) -> None:
        # Refresh the card from the (just-mutated) agents config so the
        # role / baseline / provider fields stay accurate.
        try:
            agents = (load_agents_config(self.state.paths).get("agents") or {}) if hasattr(self.state, "paths") else {}
            cfg = agents.get(name) or {}
        except Exception:
            cfg = {}
        if status == "removed":
            self.state.drop_agent(name)
        else:
            self.state.upsert_agent(
                name,
                role=cfg.get("role", ""),
                baseline=is_baseline(name),
                status=status,
            )
        self.state.add_log(f"roster :: {name} :: {status}", level="SYSTEM")


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
        elif job.kind == "cast":
            self._do_cast()
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
        elif job.kind == "roles":
            self._do_roles(job.payload or {})
        elif job.kind == "abort":
            # /abort runs in the curses thread (not the worker) because
            # the worker is blocked on the very subprocess we're killing.
            # No-op here; see TuiApp._do_abort.
            pass

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

    def _do_cast(self) -> None:
        idea_path = self.paths.state / "idea.json"
        if not idea_path.exists():
            self.state.add_log("cast skipped: no idea captured yet", level="WARN")
            return
        try:
            payload = json.loads(idea_path.read_text(encoding="utf-8"))
            title = payload.get("title") or "Untitled idea"
            statement = payload.get("statement") or ""
        except Exception as exc:
            log_exception("tui.Worker._do_cast.read_idea", exc)
            return
        self.state.set_phase("casting", busy=True)
        try:
            objective = casting_objective(title, statement)
            self.runner.run("chief", objective, extra={"phase": "casting"})
            self.state.add_system_message(
                "casting :: " + render_roster_summary(self.paths).replace("\n", " | ")[:240]
            )
        except Exception as exc:
            log_exception("tui.Worker._do_cast", exc)
            self.state.add_log(f"casting failed: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    def _do_roles(self, payload: Dict[str, Any]) -> None:
        action = payload.get("action") or "list"
        if action == "list":
            summary = render_roster_summary(self.paths)
            for line in summary.splitlines():
                self.state.add_log(line, level="SYSTEM")
            return
        name = payload.get("name") or ""
        if action == "add":
            prop = RoleProposal(name=name, role=payload.get("role", ""), provider=payload.get("provider"))
            ok, status = register_role(
                self.paths,
                prop,
                agents_cfg=self.runner.agents_cfg,
                on_change=lambda n, s: self.state.upsert_agent(
                    n, role=prop.role, baseline=is_baseline(n), status=s
                ),
            )
            self.state.add_log(f"roles add {name}: {status}", level="SYSTEM" if ok else "WARN")
            return
        if action == "remove":
            ok, status = remove_role(
                self.paths,
                name,
                agents_cfg=self.runner.agents_cfg,
                on_change=lambda n, s: self.state.drop_agent(n) if s == "removed" else None,
            )
            self.state.add_log(f"roles remove {name}: {status}", level="SYSTEM" if ok else "WARN")
            return
        self.state.add_log(f"unknown roles action: {action}", level="WARN")

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
        # Heartbeat: when an agent has been WORKING for a while we emit
        # "still working: Ns" log lines so the user knows the process
        # is alive (vs. genuinely stuck). Per-agent so one slow chief
        # doesn't flood the pane.
        self._heartbeat_last: Dict[str, float] = {}
        # Heartbeat cadence. Longer than the original 8s/8s so a normal
        # 30-60s model call doesn't drown the log in "still working" lines.
        self.HEARTBEAT_FIRST_S = 15.0   # first heartbeat after this much silence
        self.HEARTBEAT_REPEAT_S = 15.0  # then every N seconds while still working

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
            "TUI ready. Type your idea (engineering auto-runs with chief casting "
            "as stage 1). Commands: /cast, /roles list|add|drop, /run, /loop ralph 5, "
            "/stop, /abort (kill stuck subprocess), /provider claude|codex|gemini|"
            "ollama|openwebui|shell|pi, /status, /quit."
        )
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

    def _render(self, stdscr: "curses._CursesWindow") -> None:
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
        input_rows = self._input_row_count(buf, w)
        # Layout from the bottom up:
        #   y=h-input_rows..h-1                 input field rows (1..N)
        #   y=h-input_rows-1                    input frame ("---")
        #   y=h-input_rows-2                    global token totals
        #   y=idea_bottom+1..h-input_rows-3     log pane
        log_height = max(3, h - idea_bottom - input_rows - 3)
        self._draw_log(stdscr, top=idea_bottom + 1, height=log_height, width=w)
        self._draw_totals(stdscr, top=h - input_rows - 2, width=w)
        self._draw_input(stdscr, top=h - input_rows - 1, width=w, rows=input_rows)
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
                )
                for name, card in self.state.agents.items()
                if card.status == AgentStatus.WORKING
            ]
        for name, started, pid, total_bytes, last_io, last_line in working:
            elapsed = max(0.0, now - started)
            last = self._heartbeat_last.get(name, started)
            interval = self.HEARTBEAT_FIRST_S if last == started else self.HEARTBEAT_REPEAT_S
            if elapsed >= self.HEARTBEAT_FIRST_S and (now - last) >= interval:
                self._heartbeat_last[name] = now
                silence_s = max(0.0, now - last_io)
                if pid:
                    if total_bytes == 0:
                        # Subprocess alive but hasn't produced any output
                        # yet. Most CLIs (claude, codex, gemini) buffer
                        # stdout when piped and emit it all at once after
                        # the model responds, so this is the EXPECTED
                        # state for a slow model call. Word it that way
                        # so the user doesn't think it's broken.
                        if elapsed < 60:
                            flavor = f"awaiting model response (no output yet, {elapsed:.0f}s)"
                        elif elapsed < 120:
                            flavor = (
                                f"still no output after {elapsed:.0f}s "
                                f"(slow model call or stuck; type /abort to cancel)"
                            )
                        else:
                            flavor = (
                                f"NO OUTPUT FOR {elapsed:.0f}s -- likely stuck; "
                                "type /abort to kill it and try again"
                            )
                    elif silence_s > 5.0:
                        flavor = (
                            f"streaming idle {silence_s:.0f}s "
                            f"(received {total_bytes}b so far, last='{(last_line or '-')[:60]}')"
                        )
                    else:
                        flavor = f"streaming ({total_bytes}b received)"
                    note = f"pid={pid} :: {flavor}"
                else:
                    # No PID == no subprocess (in-process providers like
                    # ollama/openwebui) or the subprocess hasn't started
                    # yet. Either way, be explicit.
                    note = "no subprocess yet (provider initializing)"
                self.state.add_log(
                    f"{name} :: working {elapsed:.0f}s :: {note}",
                    level="INFO" if total_bytes > 0 or elapsed < 120 else "WARN",
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
        spin = self._spinner[self._spinner_idx] if busy else " "
        title = (
            f" CLK :: {project} :: provider={provider} :: phase={phase} {spin} "
            f"iter={iteration} :: tok={_format_tokens(tot)} "
            f"(in={_format_tokens(tot_in)}/out={_format_tokens(tot_out)}) :: "
            f"files={files} "
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
            ("rsp", card.last_result or card.last_error or "-"),
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
                elapsed = max(0.0, time.monotonic() - card.last_started_mono)
                # 4 cells/sec baseline; +1/sec for each 5s elapsed so
                # the eye knows when something is taking unusually long.
                speed = 4.0 + (elapsed / 5.0)
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
        cont_indent = "           " + " " * 7  # ts(8) + space + [LEVEL ](7) area
        flat: List[Tuple[int, str]] = []  # (level_attr, text)
        for line in lines:
            attr = self._log_attr(line.level)
            head = f"{line.ts} [{line.level:<5}] {line.text}"
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

    def _draw_input(self, stdscr, *, top: int, width: int, rows: int = 1) -> None:
        # Frame line above the input rows.
        self._safe_addstr(
            stdscr, top, 0, "-" * (width - 1), width, curses.color_pair(self.COLOR_FRAME)
        )
        with self.state.lock:
            buf = self.state.input_buffer
        prompt = "> "
        full = prompt + buf
        eff = max(1, width - 1)
        # Character-wrap so cursor math is exact even when the user
        # types continuous strings (URLs, paste).
        chunks = [full[i:i + eff] for i in range(0, max(eff, len(full)), eff)] or [""]
        # If the buffer needs more rows than we have, show the LAST
        # ``rows`` chunks so the cursor stays visible.
        if len(chunks) > rows:
            chunks = chunks[-rows:]
        attr = curses.color_pair(self.COLOR_PROMPT) | curses.A_BOLD
        base_y = top + 1
        for i, line in enumerate(chunks):
            self._safe_addstr(stdscr, base_y + i, 0, line, width, attr)
        # Cursor goes after the last visible character.
        try:
            cursor_x = min(len(chunks[-1]), width - 2)
            cursor_y = base_y + len(chunks) - 1
            stdscr.move(cursor_y, cursor_x)
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
            if cmd == "cast":
                self.worker.submit(Job("cast"))
                return True
            if cmd == "abort":
                self._do_abort()
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
            self.state.add_log(f"unknown command: /{cmd}", level="WARN")
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
        if not self.state.idea:
            self.worker.submit(Job("idea", msg))
            self.worker.submit(Job("run", "engineering"))
        else:
            self._append_conversation(msg)
            self.worker.submit(Job("run", "engineering"))
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
            self.state.add_log("abort: no agents currently running", level="WARN")
            return
        for name, pid in targets:
            try:
                os.kill(pid, signal.SIGTERM)
                self.state.add_log(f"abort: SIGTERM sent to {name} pid={pid}", level="WARN")
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

    state = DashboardState(agent_names, paths=paths, agents_cfg=agents_cfg)
    state.project_name = clk_cfg.get("project_name") or paths.root.name
    state.provider = providers_cfg.get("active") or clk_cfg.get("default_provider") or "shell"
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
    return 0
