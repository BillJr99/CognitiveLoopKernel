"""Common provider interface."""

from __future__ import annotations

import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot service a request."""


ProgressKind = str  # "start" | "stdout_line" | "stderr_line" | "end" | "timeout" | "tick"
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
    # Streaming progress callback. Providers that drive a CLI subprocess
    # call this with kind in {"start", "stdout_line", "stderr_line",
    # "end", "timeout", "tick"} so the UI can show real-time activity
    # rather than a stalled spinner. Observers in the orchestration
    # layer wire this to the TUI's status pane / agent cards.
    on_progress: Optional[ProgressFn] = None


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


def run_streaming(
    cmd: List[str],
    *,
    stdin_text: Optional[str],
    timeout_s: int,
    cwd: Optional[Path] = None,
    on_progress: Optional[ProgressFn] = None,
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
    timeout, -2 on launch failure.
    """
    progress = on_progress or (lambda kind, msg: None)
    try:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE if stdin_text is not None else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            cwd=str(cwd) if cwd else None,
            bufsize=1,  # line-buffered
        )
    except FileNotFoundError as exc:
        progress("end", f"launch_failed: {exc}")
        return -2, "", str(exc)
    progress("start", f"pid={proc.pid} cmd={' '.join(cmd[:4])}")

    out_buf: List[str] = []
    err_buf: List[str] = []

    def _feed_stdin() -> None:
        try:
            if stdin_text is not None and proc.stdin is not None:
                proc.stdin.write(stdin_text)
                proc.stdin.flush()
        except Exception:
            pass
        finally:
            try:
                if proc.stdin is not None:
                    proc.stdin.close()
            except Exception:
                pass

    def _read_stream(stream, buf: List[str], kind: str) -> None:
        try:
            for line in stream:
                buf.append(line)
                progress(kind, line.rstrip())
        except Exception:
            pass

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
    try:
        rc = proc.wait(timeout=timeout_s)
    except subprocess.TimeoutExpired:
        progress("timeout", f"after {timeout_s}s; killing pid={proc.pid}")
        try:
            proc.kill()
        except Exception:
            pass
        try:
            proc.wait(timeout=5)
        except Exception:
            pass
        rc = -1  # canonicalize to -1 on timeout regardless of signal
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

    def invoke(self, req: AgentRequest) -> AgentResponse:
        raise NotImplementedError
