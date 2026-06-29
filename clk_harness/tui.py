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
      /loop ralph|autoresearch [N]  start a loop (autoresearch uses ralph in research mode)
      /stop                  request the active loop to stop
      /provider <name>       switch active provider
      /status                show a status snapshot in the log
      /quit                  exit the TUI
"""

from __future__ import annotations

import curses
import json
import queue
import re
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
from .git_ops import (
    add_all,
    commit as git_commit,
    commits_ahead,
    has_changes,
    has_remote,
    is_repo,
    push as git_push,
)
from .orchestration import (
    AgentObserver,
    AgentRunner,
    AutoresearchLoop,
    Evaluator,
    MissionRunner,
    RalphLoop,
    RoleProposal,
    WorkflowRunner,
    casting_objective,
    is_baseline,
    is_provider_failure,
    list_roles,
    load_workflow,
    register_role,
    remove_role,
    render_roster_summary,
)
from .pricing import estimate_usd, format_usd
from .utils.logging_utils import log_exception
from .utils.text_extract import classify_error, extract_thought


# ---------------------------------------------------------------------------
# Error classifier — now lives in clk_harness.utils.text_extract so the
# headless web/API layer can reuse it without importing curses. It is
# re-exported here (imported above) so existing call sites are unchanged.
# ---------------------------------------------------------------------------


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
    LOG_PREFIX_RE = re.compile(
        r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\s+\[(ERROR|WARN|INFO)\]\s*(.*)$"
    )

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
            m = self.LOG_PREFIX_RE.match(line)
            if m:
                level = m.group(1)
                line = m.group(2)
            else:
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


# ``_extract_thought`` moved to clk_harness.utils.text_extract.extract_thought
# (re-exported below as the original private name so call sites are unchanged).
_extract_thought = extract_thought


class AgentStatus:
    IDLE = "idle"
    WORKING = "working"
    RECOVERING = "recovering"   # red: retry backoff in progress after a provider error
    DONE = "done"
    FAILED = "failed"
    PROVIDER_ERROR = "provider"


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
    # USD cost estimates — populated by DashboardState.end_agent via
    # clk_harness.pricing. last_usd is per-run, total_usd is cumulative.
    last_usd: float = 0.0
    total_usd: float = 0.0
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
    provider_issue: bool = False
    provider_resolution: str = ""
    live_cpu_pct: str = ""
    live_rss_kb: str = ""
    live_idle_s: float = 0.0
    live_elapsed_s: float = 0.0


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
        self.input_cursor: int = 0
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
        # Cost guardrails. ``total_usd`` is the rolling estimate based on
        # tokens × provider pricing; ``cost_per_provider`` lets /status
        # show a breakdown so the user can see which provider is eating
        # the cap. Caps are read lazily from clk.config.json.
        self.total_usd: float = 0.0
        self.cost_per_provider: Dict[str, float] = {}
        # Most-recent classified error — drives the hint bar below the
        # input. Cleared on the next successful run.
        self.last_error_kind: str = ""
        self.last_error_command: str = ""
        # Set to True while /tutorial is running so other commands can
        # display a "currently running tutorial" banner.
        self.in_tutorial: bool = False
        # Set true once /help has been opened in this session — used to
        # suppress the "press F1 for help" repeat in the hint bar.
        self.help_dismissed: bool = False
        # Count of local commits ahead of origin (refreshed lazily by
        # the worker after each successful agent commit).
        self.github_ahead: int = 0

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

    def add_file_log(self, text: str, level: str = "INFO") -> None:
        """Write directly to the session log file without adding to the TUI pane.

        Used for high-volume telemetry (tick, command) that is useful for
        post-run analysis but would flood the visible status log.
        """
        with self.lock:
            fh = self.session_log_fh
        if fh is not None:
            try:
                fh.write(
                    f"{datetime.now().isoformat(timespec='seconds')} [{level}] {text}\n"
                )
                fh.flush()
            except Exception:
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
            card.provider_issue = False
            card.provider_resolution = ""
            card.live_cpu_pct = ""
            card.live_rss_kb = ""
            card.live_idle_s = 0.0
            card.live_elapsed_s = 0.0
        # Take the first non-empty line of the objective so that multi-line
        # objectives (e.g. recovery dispatches that start with a blank line
        # after the header) don't produce a stray fragment in the log pane.
        _obj_first = next(
            (l for l in (objective or "").splitlines() if l.strip()),
            (objective or ""),
        )
        self.add_log(f"{name} :: start :: {_obj_first[:80]}", level="INFO")

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
            elif kind == "tick":
                # Strip pid= token before storing — live_pid tracks it separately
                # and we don't want it duplicated in the card's status line.
                stripped = re.sub(r"\bpid=\S+\s*", "", message).strip()
                card.live_last_line = stripped[:200]
                for tok in message.split():
                    if tok.startswith("cpu="):
                        card.live_cpu_pct = tok.split("=", 1)[1]
                    elif tok.startswith("rss_kb="):
                        card.live_rss_kb = tok.split("=", 1)[1]
                    elif tok.startswith("idle_s="):
                        try:
                            card.live_idle_s = float(tok.split("=", 1)[1])
                        except Exception:
                            pass
                    elif tok.startswith("elapsed_s="):
                        try:
                            card.live_elapsed_s = float(tok.split("=", 1)[1])
                        except Exception:
                            pass
            elif kind in ("command", "retry", "killed"):
                card.live_last_line = message[:200]
            elif kind in ("end", "timeout"):
                card.live_pid = 0
                card.live_last_line = f"{kind}: {message}"[:200]
        # Route events: high-volume telemetry goes to the session file only;
        # actionable events go to both the TUI pane and the file.
        if kind == "start":
            # A new subprocess started — if this agent was in RECOVERING
            # (backoff after a provider error), it is now retrying; show yellow.
            with self.lock:
                card2 = self.agents.get(name)
                if card2 and card2.status == AgentStatus.RECOVERING:
                    card2.status = AgentStatus.WORKING
                    card2.provider_issue = False
            self.add_log(f"{name} :: subprocess {message}", level="SYSTEM")
        elif kind == "command":
            # Full command metadata is verbose; useful for forensics, not for
            # the live status pane. Write to session log file only.
            try:
                meta = json.loads(message)
                args = meta.get("args") or []
                arg_note = "no args" if not args else f"args={args}"
                line = (
                    f"{name} :: command :: {meta.get('cmd')} "
                    f"(argv_count={meta.get('argv_count')}; {arg_note}; "
                    f"stdin={meta.get('stdin')} {meta.get('stdin_chars')} chars; "
                    f"cwd={meta.get('cwd')})"
                )
            except Exception:
                line = f"{name} :: command :: {message[:240]}"
            self.add_file_log(line, level="SYSTEM")
        elif kind == "tick":
            # Per-process telemetry ticks are high-frequency; file log only.
            self.add_file_log(f"{name} :: telemetry :: {message[:240]}", level="INFO")
        elif kind == "retry":
            # Provider error during a run — enter red RECOVERING state.
            with self.lock:
                card2 = self.agents.get(name)
                if card2:
                    card2.status = AgentStatus.RECOVERING
                    card2.provider_issue = True
            self.add_log(f"{name} :: retry :: {message[:240]}", level="WARN")
        elif kind == "killed":
            self.add_log(f"{name} :: killed :: {message[:240]}", level="WARN")
        elif kind.startswith("http_"):
            self.add_log(f"{name} :: {kind} :: {message[:240]}", level="SYSTEM")
        elif kind == "stderr_line" and message:
            self.add_log(f"{name} stderr: {message[:200]}", level="INFO")
        elif kind == "stdout_line":
            with self.lock:
                cnt = self.agents[name].live_stdout_chars
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
        provider_issue = (not ok) and is_provider_failure(error)
        with self.lock:
            card = self.agents.setdefault(name, AgentCard(name=name))
            card.status = AgentStatus.DONE if ok else (
                AgentStatus.PROVIDER_ERROR if provider_issue else AgentStatus.FAILED
            )
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
            card.provider_issue = provider_issue
            if provider_issue:
                card.provider_resolution = self._provider_resolution_message(error)
                kind, _, cmd = classify_error(error)
                self.last_error_kind = kind
                self.last_error_command = cmd
            elif ok:
                # Clear the hint after a successful run so the user
                # doesn't keep seeing a stale "install pi" suggestion.
                self.last_error_kind = ""
                self.last_error_command = ""
            self.total_input_tokens += in_tok
            self.total_output_tokens += out_tok
            # Cost accumulation. We look up pricing per-provider so a
            # mixed-provider session (chief on claude, engineer on
            # ollama) gets accurate per-provider totals.
            try:
                prov_name = card.provider or self.provider or ""
                prov_overrides = {}
                if self.paths is not None:
                    try:
                        from .config import load_providers_config as _lpc
                        prov_cfg = (_lpc(self.paths).get("providers") or {}).get(prov_name) or {}
                        prov_overrides = {
                            "pricing": prov_cfg.get("pricing"),
                            "pricing_by_model": prov_cfg.get("pricing_by_model"),
                        }
                    except Exception:
                        prov_overrides = {}
                model = (usage.get("model") or "") or ""
                run_usd = estimate_usd(prov_name, model, in_tok, out_tok, prov_overrides)
                card.last_usd = run_usd
                card.total_usd = (getattr(card, "total_usd", 0.0) or 0.0) + run_usd
                self.total_usd += run_usd
                self.cost_per_provider[prov_name] = self.cost_per_provider.get(prov_name, 0.0) + run_usd
            except Exception as exc:
                log_exception("tui.end_agent.cost", exc)
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
        if provider_issue:
            self.add_log(
                f"{name} :: provider issue :: {error[:200]}",
                level="ERROR",
            )
            self.add_log(
                f"{name} :: resolution :: {self._provider_resolution_message(error)}",
                level="WARN",
            )
        # File-action log lines: one INFO entry per file so the user
        # sees creation activity as it happens.
        for fpath in files_written:
            self.add_log(f"{name} :: wrote {fpath}", level="SYSTEM")

    def _provider_resolution_message(self, error: str) -> str:
        msg = (error or "").lower()
        if "rate limit" in msg or "quota" in msg:
            return "provider rate/quota failure; backing off by aborting this cycle, then retry after quota/reset or switch provider"
        if "timeout" in msg or "no output" in msg or "operation was aborted" in msg:
            return "provider call stalled/aborted; stalled PID is killed, configured retries are reissued with backoff, then the cycle stops if retries fail"
        if "api key" in msg or "authentication" in msg or "unauthorized" in msg or "forbidden" in msg:
            return "provider auth/config failure; fix credentials or switch provider before retrying"
        if "no endpoints available" in msg or "guardrail restrictions" in msg or "data policy" in msg:
            return "provider endpoint/policy routing issue; configured retries are reissued with backoff because this can be transient, then switch provider or adjust provider privacy settings if retries fail"
        if "cli not found" in msg or "not found" in msg:
            return "provider executable/config missing; install/configure provider or switch provider"
        return "provider failure; workflow recovery is aborted until the provider is fixed or changed"

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
                "input_cursor": self.input_cursor,
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
        # workflow_written is a workflow event, not a roster change — log it
        # separately and don't create/modify an agent card for it.
        if status == "workflow_written":
            self.state.add_log(f"workflow :: {name} :: written", level="INFO")
            return
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
        elif job.kind == "mission":
            self._do_mission()
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
        elif job.kind == "install":
            self._do_install(job.payload or "")
        elif job.kind == "configure":
            self._do_configure(job.payload or "")
        elif job.kind == "github":
            self._do_github()
        elif job.kind == "undo":
            self._do_undo(bool((job.payload or {}).get("confirm")))
        elif job.kind == "doctor":
            self._do_doctor(bool((job.payload or {}).get("fix")))
        elif job.kind == "diag":
            self._do_diag()
        elif job.kind == "tutorial":
            self._do_tutorial()
        elif job.kind == "workspaces":
            self._do_workspaces(job.payload or {})

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
            self.state.add_system_message(
                f"got it — idea captured as '{title}'. The chief will cast a "
                f"team next; agent cards above will turn yellow as they start."
            )
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
            self.state.add_log(
                f"workflow '{name}' not found — try /run engineering or check "
                f".clk/config/workflows/ for the available list",
                level="WARN",
            )
            return
        self.state.set_phase(f"workflow:{name}", busy=True)
        self.state.add_system_message(
            f"starting workflow '{name}' — the chief will cast a team and "
            f"dispatch agents stage by stage. Watch the cards above for live progress."
        )
        any_failure = False
        try:
            wf = load_workflow(wf_path)
            wf_runner = WorkflowRunner(self.paths, self.runner)
            wf_runner.run(wf)
        except Exception as exc:
            any_failure = True
            log_exception("tui.Worker._do_workflow", exc)
            self.state.add_log(f"workflow '{name}' hit an error: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)
            # Friendly post-flight summary. Always tell the user what
            # they can do next — even on failure — so they're never
            # stuck wondering "is something broken? what do I do?".
            with self.state.lock:
                tot = self.state.total_tokens
                usd = self.state.total_usd
                files = self.state.total_files
                err_kind = self.state.last_error_kind
                err_cmd = self.state.last_error_command
            if any_failure or err_kind:
                self.state.add_system_message(
                    f"workflow '{name}' finished with issues. session tokens={_format_tokens(tot)} "
                    f"cost={format_usd(usd)} files={files}"
                )
                if err_cmd:
                    self.state.add_system_message(
                        f"suggested next step: {err_cmd}  (or /provider <other> to switch)"
                    )
                else:
                    self.state.add_system_message(
                        "suggested next step: /status to inspect, /undo to roll back, "
                        "or type a follow-up message"
                    )
            else:
                self.state.add_system_message(
                    f"workflow '{name}' complete. session tokens={_format_tokens(tot)} "
                    f"cost={format_usd(usd)} files={files}"
                )
                self.state.add_system_message(
                    "next steps: type a follow-up message to keep going, "
                    "/loop ralph 5 to refine, /undo to revert, or /quit to exit."
                )

    def _do_mission(self) -> None:
        """Drive the autonomous mission (charter -> plan -> phases -> done)."""
        self.state.clear_stop()
        self.state.set_phase("mission", busy=True)
        self.state.add_system_message(
            "starting autonomous mission — the chief writes a charter and plan, "
            "then drives the lifecycle to a code-gated done. Watch the cards above; "
            "type /stop to end after the current cycle."
        )
        any_failure = False
        try:
            mr = MissionRunner(self.paths, self.runner, self.evaluator)
            plan = mr.run()
            self.state.add_system_message(
                f"mission {plan.status}: "
                f"{sum(1 for p in plan.phases if p.status == 'done')}/{len(plan.phases)} "
                f"phases done, {plan.total_cycles_used} cycles."
            )
            if plan.status != "done" and (plan.done_gate_last or {}).get("failures"):
                self.state.add_system_message(
                    "done-gate unmet: " + ", ".join(plan.done_gate_last["failures"])
                )
        except Exception as exc:
            any_failure = True
            log_exception("tui.Worker._do_mission", exc)
            self.state.add_log(f"mission hit an error: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)
            if not any_failure:
                self.state.add_system_message(
                    "next steps: type a follow-up to extend the mission, "
                    "/loop ralph 5 to refine, /undo to revert, or /quit."
                )

    def _do_loop(self, mode: str, n: int) -> None:
        self.state.clear_stop()
        self.state.set_phase(f"loop:{mode}", busy=True)
        self.state.add_system_message(
            f"starting {mode} loop for up to {n} iterations. "
            f"Type /stop to end after the current cycle, or /abort to kill a stuck call."
        )
        interrupted = False
        completed = 0
        try:
            if mode == "ralph":
                # We can't preempt mid-iteration, but we can check between iterations
                # by running one iteration at a time.
                for i in range(1, n + 1):
                    if self.state.is_stop_requested():
                        interrupted = True
                        self.state.add_log(
                            f"loop interrupted after iteration {i - 1} of {n}",
                            level="WARN",
                        )
                        break
                    self.state.iteration_count = i
                    self.state.add_system_message(
                        f"ralph iteration {i}/{n} — refining the previous output"
                    )
                    sub = RalphLoop(self.paths, self.runner, self.evaluator, max_iterations=1)
                    sub.run()
                    completed = i
            else:
                for i in range(1, n + 1):
                    if self.state.is_stop_requested():
                        interrupted = True
                        self.state.add_log(
                            f"loop interrupted after iteration {i - 1} of {n}",
                            level="WARN",
                        )
                        break
                    self.state.iteration_count = i
                    self.state.add_system_message(
                        f"autoresearch iteration {i}/{n} — exploring open questions"
                    )
                    sub = AutoresearchLoop(self.paths, self.runner, self.evaluator, max_iterations=1)
                    sub.run()
                    completed = i
        except Exception as exc:
            log_exception("tui.Worker._do_loop", exc)
            self.state.add_log(f"loop hit an error and stopped: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)
            verb = "stopped" if interrupted else "complete"
            self.state.add_system_message(
                f"{mode} loop {verb} after {completed} iteration(s). "
                f"Type /status for the breakdown, /loop {mode} {n} to keep going, "
                f"or a follow-up message to redirect."
            )

    def _do_set_provider(self, name: str) -> None:
        try:
            cfg_path = self.paths.config / "providers.json"
            data = json.loads(cfg_path.read_text(encoding="utf-8"))
            if name not in (data.get("providers") or {}):
                self.state.add_log(
                    f"'{name}' isn't a known provider. valid: "
                    f"{', '.join(sorted((data.get('providers') or {}).keys()))}",
                    level="WARN",
                )
                return
            old_name = self.state.provider or "(unset)"
            data["active"] = name
            save_json(cfg_path, data)
            self.providers_cfg = data
            self.runner.providers_cfg = data
            with self.state.lock:
                self.state.provider = name
                # New provider — clear stale error hints so the bar doesn't
                # keep suggesting /install <old_provider>.
                self.state.last_error_kind = ""
                self.state.last_error_command = ""
            # Check that the new provider is actually usable so we can
            # warn before the user's next call fails.
            try:
                from .providers import available_providers as _ap
                avail = _ap(data)
                if avail.get(name):
                    self.state.add_system_message(
                        f"provider: {old_name} → {name}  (ready)"
                    )
                else:
                    self.state.add_system_message(
                        f"provider: {old_name} → {name}  (NOT ready — try /install {name} or /configure {name})"
                    )
                    with self.state.lock:
                        self.state.last_error_kind = "not_installed"
                        self.state.last_error_command = f"/install {name}"
            except Exception:
                self.state.add_system_message(f"provider switched to {name}")
        except Exception as exc:
            log_exception("tui.Worker._do_set_provider", exc)
            self.state.add_log(f"provider switch failed: {exc}", level="ERROR")

    def _emit_status(self) -> None:
        snap = self.state.snapshot()
        # Header: short narrative the user can read at a glance.
        phase = snap.get("phase") or "idle"
        busy = snap.get("busy")
        provider = snap.get("provider") or "shell"
        agents = snap.get("agents") or {}
        narrative = (
            f"working on '{phase}'" if busy else f"idle (last phase: '{phase}')"
        )
        self.state.add_system_message(
            f"--- session snapshot ---"
        )
        self.state.add_system_message(
            f"  status     {narrative}"
        )
        self.state.add_system_message(
            f"  provider   {provider}"
        )
        self.state.add_system_message(
            f"  agents     {len(agents)} ({', '.join(sorted(agents.keys())) or 'none yet'})"
        )
        # Cost breakdown — same numbers the title bar shows, but split by
        # provider so the user can see where the spend went.
        with self.state.lock:
            usd = self.state.total_usd
            per = dict(self.state.cost_per_provider)
            tot = self.state.total_tokens
            files = self.state.total_files
            idea = self.state.idea[:80]
        self.state.add_system_message(
            f"  tokens     {_format_tokens(tot)}    files written: {files}"
        )
        self.state.add_system_message(f"  est. cost  {format_usd(usd)}")
        if per:
            for p, amount in sorted(per.items()):
                if amount > 0:
                    self.state.add_system_message(f"    - {p:<10} {format_usd(amount)}")
        if idea:
            self.state.add_system_message(f"  idea       {idea}")
        self.state.add_system_message(
            "------------------------"
        )

    # ----- subprocess helpers used by /install /configure /doctor ----------

    def _run_capture(self, cmd: List[str], cwd: Optional[Path] = None) -> Tuple[int, str, str]:
        """Run a subprocess, stream its output into the log pane, and
        return (rc, stdout, stderr). Used by /install, /configure, etc.
        These commands are interactive (they prompt y/N, ask for keys)
        so we do NOT capture stdin — the subprocess inherits ours and
        runs against /dev/tty when sourced functions need it.
        """
        import subprocess
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=str(cwd) if cwd else None,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except Exception as exc:
            log_exception("tui.Worker._run_capture", exc)
            return 1, "", str(exc)
        out_lines: List[str] = []
        err_lines: List[str] = []
        # Drain both pipes line-by-line so the log pane sees progress.
        import threading as _t
        def _pump(stream, sink, level):
            try:
                for line in stream:
                    line = line.rstrip()
                    if line:
                        sink.append(line)
                        self.state.add_log(line, level=level)
            except Exception:
                pass
        t1 = _t.Thread(target=_pump, args=(proc.stdout, out_lines, "INFO"), daemon=True)
        t2 = _t.Thread(target=_pump, args=(proc.stderr, err_lines, "WARN"), daemon=True)
        t1.start(); t2.start()
        rc = proc.wait()
        t1.join(timeout=1); t2.join(timeout=1)
        return rc, "\n".join(out_lines), "\n".join(err_lines)

    def _script(self, name: str) -> Path:
        # Locate scripts/<name> relative to the harness install.
        return Path(__file__).resolve().parent.parent / "scripts" / name

    # ----- /install ---------------------------------------------------------

    def _do_install(self, tool: str) -> None:
        tool = (tool or "").strip()
        if not tool:
            self.state.add_log("install: no tool specified", level="WARN")
            return
        self.state.set_phase(f"install {tool}", busy=True)
        try:
            script = self._script("install_tool.sh")
            if not script.exists():
                self.state.add_log(f"install: {script} not found", level="ERROR")
                return
            rc, _out, err = self._run_capture(["bash", str(script), "install", tool, "--prompt"])
            if rc == 0:
                self.state.add_system_message(f"install {tool}: done")
                # Clear the not_installed hint so the bar updates.
                with self.state.lock:
                    if self.state.last_error_kind == "not_installed":
                        self.state.last_error_kind = ""
                        self.state.last_error_command = ""
            else:
                self.state.add_log(f"install {tool}: rc={rc} {err[:200]}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    # ----- /configure -------------------------------------------------------

    def _do_configure(self, tool: str) -> None:
        tool = (tool or "").strip()
        if not tool:
            self.state.add_log("configure: no tool specified", level="WARN")
            return
        self.state.set_phase(f"configure {tool}", busy=True)
        try:
            script = self._script("install_tool.sh")
            rc, _out, err = self._run_capture(["bash", str(script), "configure", tool])
            if rc == 0:
                self.state.add_system_message(f"configure {tool}: done")
                with self.state.lock:
                    if self.state.last_error_kind == "auth":
                        self.state.last_error_kind = ""
                        self.state.last_error_command = ""
            else:
                self.state.add_log(f"configure {tool}: rc={rc} {err[:200]}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    # ----- /github ----------------------------------------------------------

    def _do_github(self) -> None:
        # GitHub re-link from inside the TUI. We don't re-run the full
        # wizard here — we just print the current state and the
        # instructions. The wizard prompts via /dev/tty which the curses
        # screen has already taken over, so attempting an interactive
        # prompt from within the TUI would corrupt the display.
        self.state.set_phase("github", busy=True)
        try:
            root = self.paths.root
            rc, out, _ = self._run_capture(["git", "-C", str(root), "remote", "-v"])
            self.state.add_system_message("current git remotes:")
            for line in (out or "").splitlines():
                self.state.add_system_message(f"  {line}")
            self.state.add_system_message(
                "to (re-)link a remote, /quit then run: ./kickoff.sh --setup"
            )
            self.state.add_system_message(
                "the wizard's GitHub block handles create | existing | skip safely from /dev/tty"
            )
        finally:
            self.state.set_phase("idle", busy=False)

    # ----- /undo ------------------------------------------------------------

    def _do_undo(self, confirm: bool) -> None:
        root = self.paths.root
        try:
            if has_changes(root):
                self.state.add_log(
                    "undo refused: uncommitted changes in the workspace. "
                    "Commit or stash first.",
                    level="WARN",
                )
                return
            # Show the diff of HEAD before doing anything.
            rc, out, err = self._run_capture(
                ["git", "-C", str(root), "log", "-1", "--stat"]
            )
            if rc != 0:
                self.state.add_log(f"undo: cannot read HEAD: {err}", level="ERROR")
                return
            if not confirm:
                self.state.add_system_message("last commit (HEAD):")
                for line in (out or "").splitlines()[:40]:
                    self.state.add_system_message(f"  {line}")
                self.state.add_system_message(
                    "type /undo confirm to revert this commit (creates a new revert commit)"
                )
                return
            rc, _out, err = self._run_capture(
                ["git", "-C", str(root), "revert", "--no-edit", "HEAD"]
            )
            if rc == 0:
                self.state.add_system_message("undo: HEAD reverted with a new commit.")
            else:
                self.state.add_log(f"undo: revert failed: {err}", level="ERROR")
        except Exception as exc:
            log_exception("tui.Worker._do_undo", exc)
            self.state.add_log(f"undo error: {exc}", level="ERROR")

    # ----- /doctor ----------------------------------------------------------

    def _do_doctor(self, fix: bool) -> None:
        self.state.set_phase("doctor", busy=True)
        try:
            from .providers import available_providers
            from .config import load_clk_config as _lcc, load_providers_config as _lpc
            prov_cfg = _lpc(self.paths)
            clk_cfg = _lcc(self.paths)
            auth_mode = (clk_cfg.get("auth_mode") or "cli").lower() if isinstance(clk_cfg, dict) else "cli"
            findings: List[Tuple[str, str, str]] = []  # (level, name, message)
            avail = available_providers(prov_cfg)
            active = prov_cfg.get("active") or clk_cfg.get("default_provider") or "shell"
            for name, ok in avail.items():
                if ok:
                    findings.append(("ok", name, "available"))
                else:
                    findings.append(("warn" if name != active else "fail", name, "unavailable"))
            # Known-bad combos.
            import os as _os
            if active == "claude" and auth_mode == "apikey" and not _os.environ.get("ANTHROPIC_API_KEY"):
                findings.append(("fail", "anthropic_key", "CLK_AUTH_MODE=apikey but ANTHROPIC_API_KEY is unset"))
            if active == "codex" and auth_mode == "apikey" and not _os.environ.get("OPENAI_API_KEY"):
                findings.append(("fail", "openai_key", "CLK_AUTH_MODE=apikey but OPENAI_API_KEY is unset"))
            # Git / GitHub.
            if not is_repo(self.paths.root):
                findings.append(("warn", "git", "no git repo at project root; auto-commit disabled"))
            # Emit.
            for level, name, msg in findings:
                self.state.add_system_message(f"doctor :: [{level:<4}] {name}: {msg}")
            failures = [f for f in findings if f[0] == "fail"]
            if not failures:
                self.state.add_system_message("doctor: all checks passed.")
                return
            if not fix:
                self.state.add_system_message(
                    f"doctor: {len(failures)} failure(s). Re-run as /doctor --fix to attempt repairs."
                )
                return
            for _, name, _ in failures:
                if name in ("anthropic_key", "openai_key"):
                    self.state.add_system_message(
                        f"doctor --fix: run /configure {active} to set the missing API key"
                    )
                elif name == active:
                    self.state.add_system_message(f"doctor --fix: run /install {name}")
        except Exception as exc:
            log_exception("tui.Worker._do_doctor", exc)
            self.state.add_log(f"doctor error: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    # ----- /diag ------------------------------------------------------------

    def _do_diag(self) -> None:
        import tarfile
        import time as _time
        ts = _time.strftime("%Y%m%d-%H%M%S")
        out_path = self.paths.root / f"clk-diag-{ts}.tar.gz"
        self.state.set_phase("diag", busy=True)
        try:
            # Build a redacted .env first in a tempfile.
            env_path = self.paths.root / ".env"
            redacted = None
            if env_path.exists():
                redacted_lines = []
                for line in env_path.read_text(encoding="utf-8").splitlines():
                    if "=" in line and not line.lstrip().startswith("#"):
                        k, v = line.split("=", 1)
                        if any(s in k.upper() for s in ("KEY", "TOKEN", "SECRET", "PASS")):
                            v = f"<redacted: {len(v)} chars>"
                        redacted_lines.append(f"{k}={v}")
                    else:
                        redacted_lines.append(line)
                redacted = self.paths.state / ".env.redacted"
                redacted.write_text("\n".join(redacted_lines) + "\n", encoding="utf-8")

            with tarfile.open(out_path, "w:gz") as tf:
                # Pick up logs (last ~5MB total), state, last 3 runs.
                for sub in ("logs", "state"):
                    d = self.paths.clk / sub
                    if d.exists():
                        tf.add(d, arcname=f".clk/{sub}")
                runs_dir = self.paths.runs
                if runs_dir.exists():
                    runs = sorted([p for p in runs_dir.glob("*") if p.is_dir()],
                                  reverse=True)[:3]
                    for r in runs:
                        tf.add(r, arcname=f".clk/runs/{r.name}")
                if redacted and redacted.exists():
                    tf.add(redacted, arcname=".env.redacted")
            if redacted and redacted.exists():
                redacted.unlink()
            self.state.add_system_message(f"diag: wrote {out_path}")
            self.state.add_system_message("share this tarball in your bug report (API keys are redacted)")
        except Exception as exc:
            log_exception("tui.Worker._do_diag", exc)
            self.state.add_log(f"diag error: {exc}", level="ERROR")
        finally:
            self.state.set_phase("idle", busy=False)

    # ----- /tutorial --------------------------------------------------------

    def _do_tutorial(self) -> None:
        # Switch to the shell provider, sandbox state under
        # .clk/state/.tutorial/, run one engineering cycle, restore.
        original_provider = self.state.provider
        try:
            with self.state.lock:
                self.state.in_tutorial = True
            self.state.add_system_message(
                "tutorial: switching to the shell provider; nothing will be charged."
            )
            self._do_set_provider("shell")
            self.state.add_system_message(
                "tutorial: idea = 'Add a hello() function to greeter.py'"
            )
            self._do_idea("Add a hello() function to greeter.py")
            self._do_workflow("engineering")
            self.state.add_system_message("tutorial: done. Type /quit, or type an idea to keep going.")
            # Mark seen so the welcome banner stops mentioning the tutorial.
            try:
                if self.paths and self.paths.state:
                    (self.paths.state / ".seen-tutorial").write_text("seen\n", encoding="utf-8")
            except Exception:
                pass
        except Exception as exc:
            log_exception("tui.Worker._do_tutorial", exc)
            self.state.add_log(f"tutorial error: {exc}", level="ERROR")
        finally:
            with self.state.lock:
                self.state.in_tutorial = False
            # Restore the user's previous provider if it was something other than shell.
            if original_provider and original_provider != "shell":
                self._do_set_provider(original_provider)

    # ----- /workspaces ------------------------------------------------------

    def _do_workspaces(self, payload: Dict[str, Any]) -> None:
        action = (payload.get("action") or "list").lower()
        args = payload.get("args") or []
        # Workspaces live one dir above the kickoff dir (the kickoff was
        # created under <repo>/workspace/kickoff-<ts>). Walk up to find
        # the workspace/ parent.
        kickoff_dir = self.paths.root
        ws_parent = kickoff_dir.parent if kickoff_dir.parent.name == "workspace" else (kickoff_dir / ".." / "..").resolve() / "workspace"
        if action == "list":
            if not ws_parent.exists():
                self.state.add_system_message("workspaces: no workspace/ dir found")
                return
            count = 0
            for d in sorted(ws_parent.glob("kickoff-*"), reverse=True):
                if not d.is_dir():
                    continue
                count += 1
                idea = ""
                idea_path = d / ".clk" / "state" / "idea.json"
                if idea_path.exists():
                    try:
                        idea = (json.loads(idea_path.read_text(encoding="utf-8")).get("title") or "")[:60]
                    except Exception:
                        idea = ""
                marker = "* " if d.resolve() == kickoff_dir.resolve() else "  "
                self.state.add_system_message(f"{marker}{d.name} :: {idea}")
            if count == 0:
                self.state.add_system_message("workspaces: no kickoff dirs yet")
        elif action == "rename":
            if len(args) < 2:
                self.state.add_log("workspaces rename: usage /workspaces rename <old> <new>", level="WARN")
                return
            old, new = ws_parent / args[0], ws_parent / args[1]
            if not old.exists():
                self.state.add_log(f"workspaces rename: {old} not found", level="WARN")
                return
            if new.exists():
                self.state.add_log(f"workspaces rename: {new} already exists", level="WARN")
                return
            old.rename(new)
            self.state.add_system_message(f"workspaces: renamed {args[0]} -> {args[1]}")
        elif action == "switch":
            self.state.add_system_message(
                "workspaces switch: /quit this TUI, then cd into the target dir and run ./.clk/scripts/clk tui"
            )
        elif action == "clean":
            self.state.add_system_message(
                "workspaces clean: run `./kickoff.sh --clean 7d` from the repo root — "
                "it prompts before deleting."
            )
        else:
            self.state.add_log(f"workspaces: unknown action {action}", level="WARN")

    def _maybe_commit(self, agent: str, objective: str, validation: str, *files: str) -> None:
        try:
            if not is_repo(self.paths.root):
                return
            if not has_changes(self.paths.root):
                return
            if not add_all(self.paths.root):
                return
            ok = git_commit(
                self.paths.root,
                agent=agent,
                objective=objective,
                files_changed=list(files),
                validation=validation,
                next_step="continue conversation",
            )
            if not ok:
                return
            # Push to GitHub if the user opted in (CLK_GITHUB_PUSH_ON_COMMIT=true)
            # and there's actually a remote. Errors are non-fatal — the
            # commit is local-only until the user can push themselves.
            import os
            push_on_commit = os.environ.get("CLK_GITHUB_PUSH_ON_COMMIT", "false").lower() == "true"
            if push_on_commit and has_remote(self.paths.root):
                self.state.add_log("pushing commit to origin…", level="SYSTEM")
                if git_push(self.paths.root):
                    self.state.add_log("push succeeded.", level="SYSTEM")
                else:
                    self.state.add_log(
                        "push failed — commit is still saved locally. /github to re-check the remote.",
                        level="WARN",
                    )
            # Refresh the title-bar ahead counter either way so the user
            # can see at a glance how many unpushed commits they have.
            try:
                ahead = commits_ahead(self.paths.root)
                with self.state.lock:
                    self.state.github_ahead = ahead
            except Exception:
                pass
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

    def _loop(self, stdscr: "curses._CursesWindow") -> None:
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
                except Exception:
                    pass
        else:
            self.state.add_system_message(
                "Welcome back. Type an idea, or /help for commands."
            )

    def _emit_provider_health(self) -> None:
        """Surface the available/broken status of each configured provider."""
        if not self.state.paths:
            return
        try:
            from .providers import available_providers
            from .config import save_providers_config
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
            err_cmd = s.last_error_command
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
                self.state.add_log(f"sent SIGTERM to {name} (pid {pid}) — the cycle will report a timeout", level="WARN")
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
    except Exception:
        pass

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
        except Exception:
            pass
    return 0
