"""Worker thread: executes TUI jobs (slash commands) off the UI thread.
"""

from __future__ import annotations

import json
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ..config import (
    Paths,
    save_json,
)
from ..git_ops import (
    add_all,
    commits_ahead,
    has_changes,
    has_remote,
    is_repo,
)
from ..git_ops import (
    commit as git_commit,
)
from ..git_ops import (
    push as git_push,
)
from ..log import get_logger, log_exception
from ..orchestration import (
    AgentRunner,
    AutoresearchLoop,
    Evaluator,
    MissionRunner,
    RalphLoop,
    RoleProposal,
    WorkflowRunner,
    casting_objective,
    is_baseline,
    load_workflow,
    register_role,
    remove_role,
    render_roster_summary,
)
from ..pricing import format_usd
from .dashboard import DashboardState
from .theme import _format_tokens

logger = get_logger(__name__)


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
                    asub = AutoresearchLoop(self.paths, self.runner, self.evaluator, max_iterations=1)
                    asub.run()
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
                from ..providers import available_providers as _ap
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
            "--- session snapshot ---"
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
            except Exception as _exc:
                logger.debug("output pump stopped: %s", _exc)
        t1 = _t.Thread(target=_pump, args=(proc.stdout, out_lines, "INFO"), daemon=True)
        t2 = _t.Thread(target=_pump, args=(proc.stderr, err_lines, "WARN"), daemon=True)
        t1.start()
        t2.start()
        rc = proc.wait()
        t1.join(timeout=1)
        t2.join(timeout=1)
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
            from ..config import load_clk_config as _lcc
            from ..config import load_providers_config as _lpc
            from ..providers import available_providers
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
            except Exception as _exc:
                logger.debug("could not persist tutorial marker: %s", _exc)
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
        ws_parent = (
            kickoff_dir.parent
            if kickoff_dir.parent.name == "workspace"
            else (kickoff_dir / ".." / "..").resolve() / "workspace"
        )
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
            except Exception as _exc:
                logger.debug("could not refresh unpushed-commit counter: %s", _exc)
        except Exception as exc:
            log_exception("tui.Worker._maybe_commit", exc)

