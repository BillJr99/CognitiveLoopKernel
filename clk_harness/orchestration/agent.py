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
from ..providers import AgentProvider, AgentRequest, AgentResponse, load_provider
from ..utils.logging_utils import log, log_exception


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


class AgentRunner:
    """Render prompts, invoke providers, persist outputs."""

    def __init__(
        self,
        paths: Paths,
        agents_cfg: Dict[str, Any],
        providers_cfg: Dict[str, Any],
        clk_cfg: Dict[str, Any],
    ) -> None:
        self.paths = paths
        self.agents_cfg = agents_cfg
        self.providers_cfg = providers_cfg
        self.clk_cfg = clk_cfg

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
        req = AgentRequest(
            agent=agent.name,
            prompt=prompt,
            workdir=self.paths.root,
            dry_run=bool(is_dry),
        )
        started = datetime.now().isoformat(timespec="seconds")
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
        return run

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

        ctx = {
            "agent": extra.get("agent", ""),
            "objective": objective,
            "project_name": self.clk_cfg.get("project_name") or self.paths.root.name,
            "project_root": str(self.paths.root),
            "state_summary": "\n".join(state_summary_lines) or "(no state yet)",
            "idea_title": (idea.get("title") if isinstance(idea, dict) else "") or "",
            "idea_statement": (idea.get("statement") if isinstance(idea, dict) else "") or "",
            "iteration": str(extra.get("iteration", "")),
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
