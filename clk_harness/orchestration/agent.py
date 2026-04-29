"""Agent runner.

Loads a prompt template, renders it against the current state, and
invokes the configured provider. The runner is intentionally thin -
heavier orchestration lives in :mod:`workflow` and the loops.
"""

from __future__ import annotations

import json
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes, is_repo
from ..providers import AgentProvider, AgentRequest, AgentResponse, load_provider
from ..utils.logging_utils import log, log_exception
from . import casting as _casting
from . import actions as _actions


@dataclass
class AgentSpec:
    name: str
    prompt_file: str
    provider: Optional[str] = None
    role: str = ""

    @classmethod
    def from_config(cls, name: str, cfg: Dict[str, Any]) -> "AgentSpec":
        return cls(
            name=name,
            prompt_file=cfg.get("prompt") or f"{name}.md",
            provider=cfg.get("provider"),
            role=cfg.get("role", ""),
        )


@dataclass
class AgentRun:
    agent: str
    objective: str
    response: AgentResponse
    started_at: str
    finished_at: str
    files_written: List[str] = field(default_factory=list)
    # True when the runner already created a git commit for this run's
    # actions; downstream consumers (WorkflowRunner._commit) skip in
    # that case to avoid double-committing the same diff.
    committed: bool = False


class AgentObserver:
    """Optional hook invoked by :class:`AgentRunner` around every agent call.

    Subclasses override the methods they care about. All methods are wrapped
    in try/except inside the runner so an observer bug never breaks a run.
    """

    def begin(self, agent: str, objective: str) -> None:  # pragma: no cover
        pass

    def prompt_sent(self, agent: str, prompt: str) -> None:  # pragma: no cover
        """Called after the prompt has been rendered, before provider invocation."""
        pass

    def end(self, agent: str, run: "AgentRun") -> None:  # pragma: no cover
        pass

    def progress(self, agent: str, kind: str, message: str) -> None:  # pragma: no cover
        """Streaming progress from a CLI provider's subprocess.

        ``kind`` is one of: ``"start"`` (subprocess launched, message
        carries pid + cmd), ``"stdout_line"`` / ``"stderr_line"`` (each
        line emitted), ``"end"`` (subprocess exited, message carries
        rc), ``"timeout"`` (killed). The TUI uses this to show the user
        what the underlying CLI is actually doing rather than letting
        them stare at a stalled spinner.
        """
        pass

    def log(self, line: str) -> None:  # pragma: no cover
        pass

    def roster_changed(self, name: str, status: str) -> None:  # pragma: no cover
        """Called when a dynamic role is added / updated / removed.

        ``status`` is one of ``"added"``, ``"updated"``, ``"removed"``,
        ``"workflow_written"``, ``"prompt_updated"``.
        """
        pass


class AgentRunner:
    """Render prompts, invoke providers, persist outputs."""

    def __init__(
        self,
        paths: Paths,
        agents_cfg: Dict[str, Any],
        providers_cfg: Dict[str, Any],
        clk_cfg: Dict[str, Any],
        observer: Optional[AgentObserver] = None,
    ) -> None:
        self.paths = paths
        self.agents_cfg = agents_cfg
        self.providers_cfg = providers_cfg
        self.clk_cfg = clk_cfg
        self.observer = observer

    # -- public ------------------------------------------------------------

    def get_agent(self, name: str) -> AgentSpec:
        cfg = (self.agents_cfg.get("agents") or {}).get(name)
        if cfg is None:
            cfg = {"prompt": f"{name}.md", "provider": None, "role": ""}
        return AgentSpec.from_config(name, cfg)

    def get_provider(self, name: Optional[str]) -> AgentProvider:
        target = name or self.providers_cfg.get("active") or self.clk_cfg.get("default_provider") or "shell"
        prov_cfg = (self.providers_cfg.get("providers") or {}).get(target)
        if prov_cfg is None:
            log(f"unknown provider '{target}', falling back to shell", level="WARN")
            target = "shell"
            prov_cfg = (self.providers_cfg.get("providers") or {}).get("shell") or {"type": "shell"}
        return load_provider(target, prov_cfg)

    def render_prompt(self, agent: AgentSpec, objective: str, extra: Optional[Dict[str, Any]] = None) -> str:
        try:
            template = self._load_prompt_template(agent.prompt_file)
            ctx = self._collect_context(objective, extra or {})
            return self._safe_substitute(template, ctx)
        except Exception as exc:
            log_exception("orchestration.agent.render_prompt", exc)
            return objective

    def run(
        self,
        agent_name: str,
        objective: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        dry_run: Optional[bool] = None,
    ) -> AgentRun:
        agent = self.get_agent(agent_name)
        provider = self.get_provider(agent.provider)
        prompt = self.render_prompt(agent, objective, extra)
        is_dry = self.clk_cfg.get("dry_run", False) if dry_run is None else dry_run

        observer = self.observer

        def _on_progress(kind: str, message: str) -> None:
            if observer is None:
                return
            try:
                observer.progress(agent.name, kind, message)
            except Exception as exc:
                log_exception("orchestration.agent.observer.progress", exc)

        timeout_s = int((self.clk_cfg.get("provider_timeout_s") or 300))
        req = AgentRequest(
            agent=agent.name,
            prompt=prompt,
            workdir=self.paths.root,
            dry_run=bool(is_dry),
            timeout_s=timeout_s,
            on_progress=_on_progress,
        )
        started = datetime.now().isoformat(timespec="seconds")
        if self.observer is not None:
            try:
                self.observer.begin(agent.name, objective)
            except Exception as exc:
                log_exception("orchestration.agent.observer.begin", exc)
            try:
                self.observer.prompt_sent(agent.name, prompt)
            except Exception as exc:
                log_exception("orchestration.agent.observer.prompt_sent", exc)
        try:
            resp = provider.invoke(req)
        except Exception as exc:
            log_exception(f"orchestration.agent.run[{agent_name}]", exc)
            resp = AgentResponse(ok=False, error=str(exc))
        finished = datetime.now().isoformat(timespec="seconds")
        run = AgentRun(
            agent=agent.name,
            objective=objective,
            response=resp,
            started_at=started,
            finished_at=finished,
            files_written=list(resp.files_written or []),
        )
        self._record(run, prompt, provider.describe())
        # Apply any PROPOSE_ROLE / PROPOSE_WORKFLOW blocks the agent
        # emitted. Mutates ``self.agents_cfg`` in place so the very next
        # stage that names a freshly-proposed role can dispatch to it.
        self._apply_proposals(run)
        # Execute any ACTION blocks the agent emitted. Real file edits
        # / shell runs land here regardless of which provider produced
        # the response, so even non-tool-using providers can drive real
        # changes. We merge the harness-applied files into the run's
        # files_written list so the TUI / commit logic see them.
        if not is_dry:
            self._apply_actions(run)
        if self.observer is not None:
            try:
                self.observer.end(agent.name, run)
            except Exception as exc:
                log_exception("orchestration.agent.observer.end", exc)
        return run

    # -- casting -----------------------------------------------------------

    def _apply_proposals(self, run: AgentRun) -> None:
        text = (run.response.text or "")
        if not text or ("PROPOSE_ROLE" not in text and "PROPOSE_WORKFLOW" not in text):
            return
        cap = int(((self.clk_cfg.get("casting") or {}).get("max_dynamic_roles"))
                  or _casting.DEFAULT_MAX_DYNAMIC_ROLES)
        observer = self.observer

        def _on_change(name: str, status: str) -> None:
            log(f"casting :: {name} :: {status}")
            if observer is not None:
                try:
                    observer.roster_changed(name, status)
                except Exception as exc:
                    log_exception("orchestration.agent.observer.roster_changed", exc)

        result = _casting.apply_response_proposals(
            self.paths,
            text,
            agents_cfg=self.agents_cfg,
            max_dynamic=cap,
            on_change=_on_change,
        )
        if not result.is_empty():
            log(f"casting from {run.agent}: {result.summary()}")

    def _apply_actions(self, run: AgentRun) -> None:
        """Execute ACTION blocks; merge harness-written files back into the run."""
        text = (run.response.text or "")
        if not text or "ACTION:" not in text and "ACTION :" not in text:
            return
        result = _actions.apply_actions(
            self.paths,
            text,
            agent_name=run.agent,
            clk_cfg=self.clk_cfg,
        )
        if result.is_empty():
            return
        # Merge into run.files_written so downstream consumers (TUI,
        # commit step) reflect what actually happened.
        seen = set(run.files_written)
        for f in result.files_written:
            if f not in seen:
                run.files_written.append(f)
                seen.add(f)
        # Also surface deletes so they get attributed to this run.
        for f in result.files_deleted:
            run.files_written.append(f"deleted:{f}")
        log(f"actions from {run.agent}: {result.summary()}")
        # Annotate the response so the TUI can show it in the log pane:
        # we tack a short summary onto the text preview path.
        if result.commands_run or result.errors:
            for cmd, out in zip(result.commands_run, result.command_outputs):
                log(f"actions[{run.agent}] $ {cmd}")
                if out.strip():
                    for line in out.strip().splitlines()[:6]:
                        log(f"actions[{run.agent}]   {line[:200]}")
            for err in result.errors:
                log(f"actions[{run.agent}] !! {err}", level="WARN")
        # Auto-commit any file changes from this batch so the git log
        # has a per-agent-run granularity. Only fires when this run
        # actually wrote (or deleted) files.
        if (result.files_written or result.files_deleted) and self.clk_cfg.get("auto_commit", True):
            self._commit_action_batch(run, result)

    def _commit_action_batch(self, run: AgentRun, result: "_actions.ActionResult") -> None:
        try:
            if not is_repo(self.paths.root):
                return
            if not has_changes(self.paths.root):
                return
            if not add_all(self.paths.root):
                return
            usage = run.response.usage or {}
            tok_total = int(usage.get("total_tokens") or 0)
            tok_in = int(usage.get("input_tokens") or 0)
            tok_out = int(usage.get("output_tokens") or 0)
            extra_lines = []
            if result.commands_run:
                extra_lines.append("Commands run:")
                for c in result.commands_run[:8]:
                    extra_lines.append(f"  $ {c}")
            if result.skipped:
                extra_lines.append("")
                extra_lines.append("Skipped actions:")
                for s in result.skipped[:8]:
                    extra_lines.append(f"  - {s}")
            extra_lines.append("")
            extra_lines.append(
                f"Tokens: total={tok_total} in={tok_in} out={tok_out} "
                f"src={usage.get('source','?')}"
            )
            committed = git_commit(
                self.paths.root,
                agent=run.agent,
                objective=run.objective,
                files_changed=run.files_written,
                validation=result.summary(),
                next_step="continue iteration",
                body_extra="\n".join(extra_lines),
            )
            if committed:
                run.committed = True
                log(
                    f"commit: [{run.agent}] {len(result.files_written)} files, "
                    f"{len(result.files_deleted)} deletes"
                )
        except Exception as exc:
            log_exception("orchestration.agent._commit_action_batch", exc)

    # -- internals ---------------------------------------------------------

    def _load_prompt_template(self, prompt_file: str) -> str:
        path = self.paths.prompts / prompt_file
        if not path.exists():
            return (
                "You are the $agent agent.\n\n"
                "Objective:\n$objective\n\n"
                "Project: $project_name\n"
                "Working directory: $project_root\n"
                "Current state summary:\n$state_summary\n"
            )
        try:
            return path.read_text(encoding="utf-8")
        except Exception as exc:
            log_exception("orchestration.agent._load_prompt_template", exc)
            return "Objective:\n$objective\n"

    def _collect_context(self, objective: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        idea_path = self.paths.state / "idea.json"
        brief_path = self.paths.state / "system_brief.md"
        prd_path = self.paths.state / "prd.json"
        progress_path = self.paths.state / "progress.md"
        decisions_path = self.paths.state / "decisions.md"

        idea = {}
        if idea_path.exists():
            try:
                idea = json.loads(idea_path.read_text(encoding="utf-8"))
            except Exception as exc:
                log_exception("orchestration.agent._collect_context.idea", exc)

        state_summary_lines: List[str] = []
        if idea:
            state_summary_lines.append(f"idea: {idea.get('title','(untitled)')}")
            if idea.get("statement"):
                state_summary_lines.append(f"statement: {idea['statement']}")
        for label, path in [
            ("brief", brief_path),
            ("prd", prd_path),
            ("progress", progress_path),
            ("decisions", decisions_path),
        ]:
            if path.exists():
                try:
                    snippet = path.read_text(encoding="utf-8").strip().splitlines()[:5]
                    if snippet:
                        state_summary_lines.append(f"{label}: " + " | ".join(snippet))
                except Exception as exc:
                    log_exception(f"orchestration.agent._collect_context.{label}", exc)

        roster_lines: List[str] = []
        for n in sorted((self.agents_cfg.get("agents") or {}).keys()):
            cfg = (self.agents_cfg.get("agents") or {}).get(n) or {}
            marker = "[baseline]" if _casting.is_baseline(n) else "[dynamic]"
            role = (cfg.get("role") or "").strip()
            roster_lines.append(f"- {marker} {n} :: {role}")
        roster_text = "\n".join(roster_lines) or "(no agents registered yet)"

        ctx = {
            "agent": extra.get("agent", ""),
            "objective": objective,
            "project_name": self.clk_cfg.get("project_name") or self.paths.root.name,
            "project_root": str(self.paths.root),
            "workspace_root": str(self.paths.workspace),
            "state_summary": "\n".join(state_summary_lines) or "(no state yet)",
            "idea_title": (idea.get("title") if isinstance(idea, dict) else "") or "",
            "idea_statement": (idea.get("statement") if isinstance(idea, dict) else "") or "",
            "iteration": str(extra.get("iteration", "")),
            "current_roster": roster_text,
        }
        ctx.update({k: v for k, v in extra.items() if k not in ctx})
        return ctx

    def _safe_substitute(self, template_text: str, ctx: Dict[str, Any]) -> str:
        try:
            return Template(template_text).safe_substitute({k: str(v) for k, v in ctx.items()})
        except Exception as exc:
            log_exception("orchestration.agent._safe_substitute", exc)
            return template_text

    def _record(self, run: AgentRun, prompt: str, provider_desc: str) -> None:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            mem = self.paths.state / "agent_memory.jsonl"
            payload = {
                "agent": run.agent,
                "objective": run.objective,
                "ok": run.response.ok,
                "error": run.response.error,
                "files_written": run.files_written,
                "started_at": run.started_at,
                "finished_at": run.finished_at,
                "provider": provider_desc,
                "usage": dict(run.response.usage or {}),
                "text_preview": (run.response.text or "")[:500],
            }
            with mem.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(payload) + "\n")

            run_dir = self.paths.runs / f"{run.started_at.replace(':','-')}-{run.agent}"
            run_dir.mkdir(parents=True, exist_ok=True)
            (run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
            (run_dir / "response.txt").write_text(run.response.text or "", encoding="utf-8")
            (run_dir / "meta.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
        except Exception as exc:
            log_exception("orchestration.agent._record", exc)
