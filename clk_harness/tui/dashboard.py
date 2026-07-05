"""Dashboard model: agent cards, log lines, and shared TUI state.

``DashboardState`` is the thread-safe state shared between the UI
thread and the worker; ``DashboardObserver`` adapts AgentRunner
callbacks onto it.
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Deque, Dict, List, Optional, TextIO, Tuple

from ..config import (
    Paths,
    load_agents_config,
)
from ..log import get_logger, log_exception
from ..orchestration import (
    AgentObserver,
    is_baseline,
    is_provider_failure,
)
from ..pricing import estimate_usd
from ..utils.text_extract import classify_error, extract_thought
from .theme import _format_tokens

logger = get_logger(__name__)


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
        self.session_log_fh: Optional[TextIO] = None
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
        except Exception as _exc:
            logger.debug("session log unavailable: %s", _exc)

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
            except Exception as _exc:
                logger.debug("session log close failed: %s", _exc)

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
            (ln for ln in (objective or "").splitlines() if ln.strip()),
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
                        except Exception as _exc:
                            logger.debug("ignoring malformed idle_s token %r: %s", tok, _exc)
                    elif tok.startswith("elapsed_s="):
                        try:
                            card.live_elapsed_s = float(tok.split("=", 1)[1])
                        except Exception as _exc:
                            logger.debug("ignoring malformed elapsed_s token %r: %s", tok, _exc)
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
        candidates = [ln for ln in snippet.splitlines() if ln.strip()]
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
                        from ..config import load_providers_config as _lpc
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
            return (
                "provider rate/quota failure; backing off by aborting this cycle, "
                "then retry after quota/reset or switch provider"
            )
        if "timeout" in msg or "no output" in msg or "operation was aborted" in msg:
            return (
                "provider call stalled/aborted; stalled PID is killed, configured retries are reissued "
                "with backoff, then the cycle stops if retries fail"
            )
        if "api key" in msg or "authentication" in msg or "unauthorized" in msg or "forbidden" in msg:
            return "provider auth/config failure; fix credentials or switch provider before retrying"
        if "no endpoints available" in msg or "guardrail restrictions" in msg or "data policy" in msg:
            return (
                "provider endpoint/policy routing issue; configured retries are reissued with backoff "
                "because this can be transient, then switch provider or adjust provider privacy settings "
                "if retries fail"
            )
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
            _paths = getattr(self.state, "paths", None)
            agents = (load_agents_config(_paths).get("agents") or {}) if _paths is not None else {}
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

