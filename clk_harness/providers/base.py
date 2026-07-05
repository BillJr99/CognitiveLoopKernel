"""Common provider interface."""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..log import get_logger

logger = get_logger(__name__)


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot service a request."""


ProgressKind = str  # start | command | stdout_line | stderr_line | end | timeout | tick | killed
ProgressFn = Callable[[ProgressKind, str], None]


@dataclass
class AgentRequest:
    """A single agent invocation."""

    agent: str
    prompt: str
    system: Optional[str] = None
    context_files: List[str] = field(default_factory=list)
    allowed_files: List[str] = field(default_factory=list)
    workdir: Optional[Path] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    timeout_s: int = 300
    no_output_timeout_s: int = 0
    # Streaming progress callback. Providers that drive a CLI subprocess
    # call this with kind in {"start", "stdout_line", "stderr_line",
    # "end", "timeout", "tick"} so the UI can show real-time activity
    # rather than a stalled spinner. Observers in the orchestration
    # layer wire this to the TUI's status pane / agent cards.
    on_progress: Optional[ProgressFn] = None
    # Abstract capability hints set by the chief via CAPABILITIES: in
    # PROPOSE_ROLE blocks. Each provider translates these to its own
    # CLI flags in _capabilities_to_args(). Known values:
    #   no-tools, no-builtin-tools,
    #   thinking-off, thinking-low, thinking-medium, thinking-high, thinking-xhigh
    capabilities: List[str] = field(default_factory=list)


@dataclass
class AgentResponse:
    """Result of an agent invocation."""

    ok: bool
    text: str = ""
    files_written: List[str] = field(default_factory=list)
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)
    # Token usage. ``source`` describes how the count was obtained:
    #   * "claude-json"     - parsed from `claude --output-format json`
    #   * "ollama-api"      - prompt_eval_count + eval_count
    #   * "shell-estimate"  - approximate (chars/4)
    #   * "codex-estimate"  - approximate
    #   * "pi-estimate"     - approximate
    # Keys when populated: ``input_tokens``, ``output_tokens``,
    # ``total_tokens``, ``source``. Empty dict when unknown.
    usage: Dict[str, Any] = field(default_factory=dict)


def estimate_tokens(prompt: str, response_text: str) -> Dict[str, Any]:
    """Cheap chars/4 estimator for providers that don't return usage."""
    in_tok = max(0, len(prompt or "") // 4)
    out_tok = max(0, len(response_text or "") // 4)
    return {
        "input_tokens": in_tok,
        "output_tokens": out_tok,
        "total_tokens": in_tok + out_tok,
        "source": "estimate",
    }


def _is_pid_alive(pid: int) -> bool:
    """Return True if the given pid still exists in the process table."""
    try:
        r = subprocess.run(
            ["ps", "-p", str(pid)],
            capture_output=True, text=True, timeout=1,
        )
        return r.returncode == 0
    except Exception:
        return False


def run_streaming(
    cmd: List[str],
    *,
    stdin_text: Optional[str],
    timeout_s: int,
    no_output_timeout_s: int = 0,
    cwd: Optional[Path] = None,
    on_progress: Optional[ProgressFn] = None,
    extra_env: Optional[Dict[str, str]] = None,
) -> Tuple[int, str, str]:
    """Run a subprocess and stream its stdout / stderr line-by-line via
    ``on_progress`` so callers (and the TUI) can see real-time activity.

    Without this helper a CLI provider invokes ``subprocess.run`` and
    blocks opaquely - the user has no way to tell if the subprocess is
    alive, doing work, or hung. With it, every stderr line the CLI
    prints (auth status, "Connecting...", rate-limit retries, etc.)
    becomes a log entry within milliseconds, and the harness can sample
    activity to drive a heartbeat.

    Returns ``(returncode, stdout, stderr)``. ``returncode`` is -1 on
    total timeout, -2 on launch failure, -3 on no-output timeout.
    """
    progress = on_progress or (lambda kind, msg: None)
    cmd_display = shlex.join(cmd)
    executable = cmd[0] if cmd else ""
    resolved_executable = shutil.which(executable) or executable
    env_snapshot = {
        k: os.environ.get(k, "")
        for k in (
            "PATH",
            "HOME",
            "OPENAI_API_KEY",
            "ANTHROPIC_API_KEY",
            "GEMINI_API_KEY",
            "GOOGLE_API_KEY",
            "CLK_PROVIDER",
        )
        if k in os.environ
    }
    for key in list(env_snapshot.keys()):
        if key.endswith("_API_KEY") and env_snapshot[key]:
            env_snapshot[key] = "<set>"
    command_meta = {
        "cmd": cmd_display,
        "argv": list(cmd),
        "argv_count": len(cmd),
        "executable": executable,
        "resolved_executable": resolved_executable,
        "args": list(cmd[1:]),
        "args_count": max(0, len(cmd) - 1),
        "cwd": str(cwd) if cwd else "",
        "stdin": "pipe" if stdin_text is not None else "none",
        "stdin_chars": len(stdin_text or ""),
        "timeout_s": timeout_s,
        "no_output_timeout_s": no_output_timeout_s,
        "env": env_snapshot,
    }
    progress(
        "command",
        json.dumps(command_meta, sort_keys=True),
    )
    proc_env: Optional[Dict[str, str]] = None
    if extra_env:
        proc_env = {**os.environ, **extra_env}

    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            env=proc_env,
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as exc:
        progress("end", f"launch_failed: {exc}")
        return -2, "", str(exc)
    progress("start", f"pid={proc.pid} cmd={cmd_display}")

    out_buf: List[str] = []
    err_buf: List[str] = []
    start_time = time.monotonic()
    last_output = [start_time]
    last_tick = start_time

    def _sample_process() -> str:
        try:
            r = subprocess.run(
                ["ps", "-p", str(proc.pid), "-o", "%cpu=", "-o", "rss="],
                capture_output=True,
                text=True,
                timeout=1,
            )
            parts = (r.stdout or "").strip().split()
            if len(parts) >= 2:
                return f"cpu={parts[0]} rss_kb={parts[1]}"
        except Exception as _exc:
            logger.debug("resource sample failed for pid %s: %s", proc.pid, _exc)
        return "cpu=? rss_kb=?"

    def _kill(reason: str, rc_value: int) -> int:
        progress("timeout", f"{reason}; killing pid={proc.pid}")
        try:
            proc.kill()
            progress("killed", f"pid={proc.pid} reason={reason}")
        except Exception as exc:
            progress("killed", f"pid={proc.pid} reason={reason} kill_error={exc}")
        try:
            proc.wait(timeout=5)
        except Exception as _exc:
            logger.debug("wait after kill failed for pid %s: %s", proc.pid, _exc)
        return rc_value

    def _feed_stdin() -> None:
        try:
            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
        except Exception as _exc:
            logger.debug("stdin feed to pid %s failed: %s", proc.pid, _exc)
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception as _exc:
                logger.debug("stdin close for pid %s failed: %s", proc.pid, _exc)

    def _read_stream(stream, buf: List[str], kind: str) -> None:
        try:
            for line in stream:
                buf.append(line)
                last_output[0] = time.monotonic()
                progress(kind, line.rstrip())
        except Exception as _exc:
            logger.debug("%s stream reader stopped: %s", kind, _exc)

    threads = [
        threading.Thread(target=_feed_stdin, name=f"clk-stdin-{proc.pid}", daemon=True),
        threading.Thread(target=_read_stream, args=(proc.stdout, out_buf, "stdout_line"),
                         name=f"clk-stdout-{proc.pid}", daemon=True),
        threading.Thread(target=_read_stream, args=(proc.stderr, err_buf, "stderr_line"),
                         name=f"clk-stderr-{proc.pid}", daemon=True),
    ]
    for t in threads:
        t.start()

    rc: int
    rc = -1
    while True:
        polled = proc.poll()
        if polled is not None:
            rc = int(polled)
            break
        now = time.monotonic()
        if now - last_tick >= 5.0:
            idle = now - last_output[0]
            elapsed = now - start_time
            progress(
                "tick",
                f"pid={proc.pid} elapsed_s={elapsed:.1f} idle_s={idle:.1f} {_sample_process()}",
            )
            last_tick = now
        if timeout_s > 0 and now - start_time >= timeout_s:
            rc = _kill(f"after {timeout_s}s", -1)
            break
        if no_output_timeout_s > 0 and now - last_output[0] >= no_output_timeout_s:
            if _is_pid_alive(proc.pid):
                # Process still alive — it's waiting for a model response, not dead.
                # Reset the silence timer so we check again after another interval.
                last_output[0] = now
                progress(
                    "tick",
                    f"pid={proc.pid} no_output_extended elapsed_s={now - start_time:.1f} "
                    f"process_alive=true {_sample_process()}",
                )
            else:
                rc = _kill(f"no output for {no_output_timeout_s}s (process dead)", -3)
                break
        time.sleep(0.25)
    for t in threads:
        t.join(timeout=2.0)
    progress("end", f"rc={rc}")
    return rc, "".join(out_buf), "".join(err_buf)


class AgentProvider:
    """Abstract base. Subclasses must implement :meth:`invoke`."""

    type_name: str = "base"

    def __init__(self, *, name: str, config: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.config = dict(config or {})

    def available(self) -> bool:
        """Return True if this provider can be used in the current env."""
        return True

    def describe(self) -> str:
        return f"{self.name} ({self.type_name})"

    def capabilities_to_args(self, capabilities: List[str]) -> List[str]:
        """Translate abstract capability names to provider-specific CLI args.

        Subclasses override this for providers whose CLIs support the relevant
        flags. The base implementation returns an empty list (no-op).
        """
        return []

    def invoke(self, req: AgentRequest) -> AgentResponse:
        raise NotImplementedError
