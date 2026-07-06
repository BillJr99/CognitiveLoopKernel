"""Prompt assembly for the agent runner.

Template loading, context collection, safe substitution, and the
meta-prompting layer (chief-drafted dispatch / role prompts with an
on-disk cache).
"""

from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from string import Template
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from ...log import get_logger, log_exception
from ...utils.activity_log import log_event
from .. import blackboard as _blackboard
from .. import casting as _casting
from .. import todos as _todos

if TYPE_CHECKING:
    import threading

    from ...config import Paths
    from .transcript import AgentRun, AgentSpec

logger = get_logger(__name__)


def _read_recent_casting_rejections(paths: "Paths", *, limit: int = 8) -> str:
    """Render the most recent role/workflow rejections into a short feedback
    block. Lets the chief see "you tried to create X but Y already exists"
    without us hardening the prompt further.
    """
    log_path = paths.state / "casting.log"
    if not log_path.exists():
        return ""
    try:
        raw_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    except Exception as _exc:
        logger.debug("could not read casting log %s: %s", log_path, _exc)
        return ""
    rows: List[Dict[str, Any]] = []
    for line in reversed(raw_lines):
        try:
            obj = json.loads(line)
        except Exception as _exc:
            logger.debug("skipping unparseable casting-log line: %s", _exc)
            continue
        if not isinstance(obj, dict):
            continue
        event = str(obj.get("event") or "")
        if event not in ("role_skipped", "workflow_skipped"):
            continue
        rows.append(obj)
        if len(rows) >= limit:
            break
    if not rows:
        return ""
    out: List[str] = ["Recent casting rejections (learn from these — reuse the existing role instead):"]
    for r in reversed(rows):
        kind = "role" if r.get("event") == "role_skipped" else "workflow"
        name = r.get("name") or "?"
        reason = r.get("reason") or "?"
        ts = r.get("timestamp") or ""
        out.append(f"- {ts} {kind} `{name}` rejected: {reason}")
    return "\n".join(out)


class PromptsMixin:
    """Prompt assembly + meta-prompting methods mixed into ``AgentRunner``."""

    paths: "Paths"
    agents_cfg: Dict[str, Any]
    clk_cfg: Dict[str, Any]
    _meta_cache_lock: "threading.Lock"

    if TYPE_CHECKING:
        # Provided by AgentRunner (runner.py); declared here so annotated
        # mixin methods type-check without a runtime import cycle.
        def get_agent(self, name: str) -> "AgentSpec": ...

        def run(
            self,
            agent_name: str,
            objective: str,
            *,
            extra: Optional[Dict[str, Any]] = None,
            dry_run: Optional[bool] = None,
        ) -> "AgentRun": ...

        def _observer_log(self, line: str) -> None: ...

    def render_prompt(self, agent: "AgentSpec", objective: str, extra: Optional[Dict[str, Any]] = None) -> str:
        try:
            template = self._load_prompt_template(agent.prompt_file)
            # Thread the dispatched agent's name into context so the per-author
            # $todos checklist (and $agent) resolve on the normal dispatch path,
            # where ``extra`` would otherwise carry no agent name.
            merged = {**(extra or {}), "agent": (extra or {}).get("agent") or agent.name}
            ctx = self._collect_context(objective, merged)
            return self._safe_substitute(template, ctx)
        except Exception as exc:
            log_exception("orchestration.agent.render_prompt", exc)
            return objective

    # -- meta-prompting ----------------------------------------------------

    def _meta_cache_path(self) -> Path:
        return self.paths.cache / "meta_prompts.jsonl"

    def _meta_cache_lookup(self, key: str) -> Optional[str]:
        path = self._meta_cache_path()
        if not path.exists():
            return None
        try:
            with self._meta_cache_lock, path.open("r", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        obj = json.loads(line)
                    except Exception as _exc:
                        logger.debug("skipping unparseable meta-cache line: %s", _exc)
                        continue
                    if isinstance(obj, dict) and obj.get("key") == key:
                        return obj.get("value") or ""
        except Exception as exc:
            log_exception("orchestration.agent._meta_cache_lookup", exc)
        return None

    def _meta_cache_store(self, key: str, value: str, *, kind: str) -> None:
        path = self._meta_cache_path()
        try:
            with self._meta_cache_lock:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps({
                        "key": key,
                        "kind": kind,
                        "value": value,
                        "ts": datetime.now().isoformat(timespec="seconds"),
                    }) + "\n")
        except Exception as exc:
            log_exception("orchestration.agent._meta_cache_store", exc)

    @staticmethod
    def _meta_key(*parts: str) -> str:
        import hashlib
        h = hashlib.sha256()
        for p in parts:
            h.update(b"\x00")
            h.update((p or "").encode("utf-8"))
        return h.hexdigest()

    def meta_draft_dispatch_prompt(
        self,
        *,
        agent_name: str,
        base_objective: str,
        blackboard_inputs: Optional[List[str]] = None,
        stage_outputs: Optional[List[str]] = None,
    ) -> Optional[str]:
        """Ask the chief to draft a tighter task prompt for ``agent_name``.

        Returns the drafted objective, or ``None`` when meta-prompting
        is disabled or the chief produced no usable text. Cached on disk
        so repeated dispatches with identical inputs cost nothing.
        """
        cfg = self.clk_cfg.get("meta_prompt") or {}
        mode = str(cfg.get("dispatch") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return None
        inputs_key = ",".join(sorted(blackboard_inputs or []))
        outputs_key = ",".join(sorted(stage_outputs or []))
        key = self._meta_key("dispatch", agent_name, base_objective, inputs_key, outputs_key)
        cached = self._meta_cache_lookup(key)
        if cached:
            return cached
        try:
            agent_spec = self.get_agent(agent_name)
            role_line = agent_spec.role or ""
            prompt_path = self.paths.prompts / agent_spec.prompt_file
            system_preview = ""
            if prompt_path.exists():
                system_preview = " ".join(prompt_path.read_text(encoding="utf-8").strip().split())[:600]
            contract_lines = ""
            if stage_outputs:
                produces = ", ".join(stage_outputs)
                contract_lines = (
                    f"\nThis stage declares an outputs contract: {produces}.\n"
                    "Your drafted prompt MUST explicitly instruct the worker to end\n"
                    "with a POST block whose PRODUCES line lists exactly these keys\n"
                    f"(`PRODUCES: {produces}`) — the harness rejects responses that\n"
                    "miss any key, so spell it out rather than assuming the worker\n"
                    "infers it.\n"
                )
            objective = (
                f"Draft a tighter task prompt for the `{agent_name}` agent for the\n"
                f"objective below. Output ONLY the new objective text — no preamble,\n"
                f"no commentary. Keep it focused, concrete, and at most 8 sentences.\n"
                f"Reference any relevant blackboard posts the worker should consult.\n"
                f"{contract_lines}\n"
                "Compliance requirements your drafted prompt must convey:\n"
                "- Deliverables are FILES written via ACTION blocks; prose alone\n"
                "  does not count and the work will be considered missing.\n"
                "- Say concretely what 'done' looks like (which files exist, what\n"
                "  they contain, what validation passes).\n\n"
                f"Worker role line: {role_line or '(none)'}\n"
                f"Worker system prompt preview: {system_preview or '(missing)'}\n\n"
                f"Original objective:\n{base_objective}\n"
            )
            self._observer_log(
                f"meta :: drafting dispatch prompt for {agent_name} via chief"
            )
            run = self.run(
                "chief",
                objective,
                extra={
                    "phase": "draft_dispatch_prompt",
                    "target_agent": agent_name,
                    "blackboard_inputs": list(blackboard_inputs or []),
                },
            )
        except Exception as exc:
            log_exception("orchestration.agent.meta_draft_dispatch_prompt", exc)
            return None
        text = (run.response.text or "").strip()
        if not text:
            return None
        self._meta_cache_store(key, text, kind="dispatch")
        log_event(
            self.paths,
            "meta_prompt_drafted",
            agent="chief",
            target_agent=agent_name,
            kind="dispatch",
            chars=len(text),
        )
        return text

    def meta_draft_role_prompt(
        self,
        *,
        role_name: str,
        role_line: str,
        hint: str = "",
    ) -> Optional[str]:
        """Ask the chief to draft a system prompt body for a new role.

        Returns the drafted prompt text or ``None`` when disabled.
        Cached on disk by ``(role_name, role_line, hint)``. Callers
        typically use this after ``register_role`` produced a scaffold
        prompt and the chief should write a real one.
        """
        cfg = self.clk_cfg.get("meta_prompt") or {}
        mode = str(cfg.get("role") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return None
        key = self._meta_key("role", role_name, role_line, hint)
        cached = self._meta_cache_lookup(key)
        if cached:
            return cached
        try:
            objective = (
                f"Draft a system prompt for a new agent named `{role_name}`.\n"
                f"Role line: {role_line or '(none)'}\n"
                + (f"Additional hint: {hint}\n" if hint else "")
                + "\nThe prompt body must be self-contained and use these placeholders:\n"
                "$$objective $$state_summary $$idea_title $$idea_statement\n"
                "$$project_name $$project_root $$iteration\n"
                "Output ONLY the prompt body — no PROPOSE_ROLE wrapper, no commentary.\n"
                "Keep it under 50 lines. Make the role's distinct ownership explicit\n"
                "compared with existing roles.\n\n"
                "The harness automatically appends the ACTION/POST protocol blocks\n"
                "to every role prompt, so do NOT restate them. Instead, your draft\n"
                "MUST include an Output section that tells the agent:\n"
                "- deliverables are files written via ACTION blocks (prose alone\n"
                "  is not a deliverable and the harness treats it as missing work);\n"
                "- to end with a POST block summarising the result, with PRODUCES\n"
                "  listing any contract keys its stage declares;\n"
                "- what a complete, verifiable result looks like for this role.\n"
                "Before emitting, re-read your draft as if you were a small local\n"
                "model: remove ambiguity, prefer imperative checklists over essays."
            )
            self._observer_log(
                f"meta :: drafting role prompt for {role_name} via chief"
            )
            run = self.run(
                "chief",
                objective,
                extra={"phase": "draft_role_prompt", "target_role": role_name},
            )
        except Exception as exc:
            log_exception("orchestration.agent.meta_draft_role_prompt", exc)
            return None
        text = (run.response.text or "").strip()
        if not text:
            return None
        self._meta_cache_store(key, text, kind="role")
        log_event(
            self.paths,
            "meta_prompt_drafted",
            agent="chief",
            target_role=role_name,
            kind="role",
            chars=len(text),
        )
        return text

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
            template = path.read_text(encoding="utf-8")
        except Exception as exc:
            log_exception("orchestration.agent._load_prompt_template", exc)
            return "Objective:\n$objective\n"
        # Dispatch-time healing: prompts written before the protocol suffix
        # existed (or hand-edited ones) lack the ACTION/POST grammar, which
        # makes the agent emit prose instead of parseable blocks. Append it
        # in-memory so every dispatch carries the protocol even when the
        # file on disk is stale. Prompts that already carry the base footer
        # were assembled deliberately from the templates (e.g. critic.md
        # carries only the footer because it never emits actions) — leave
        # those alone to avoid duplicating shared blocks.
        if (
            _casting._PROTOCOL_MARKER not in template
            and "Self-assessment footer" not in template
        ):
            suffix = _casting._harness_protocol_suffix()
            if suffix:
                template = template.rstrip() + suffix + "\n"
        return template

    def _collect_context(self, objective: str, extra: Dict[str, Any]) -> Dict[str, Any]:
        idea_path = self.paths.state / "idea.json"
        brief_path = self.paths.state / "system_brief.md"
        # Agents write PRD.json, PROGRESS.md, and DECISIONS.md to the project
        # root (they cannot write to .clk/state/ via ACTIONs).  Check those
        # paths first; fall back to the legacy .clk/state/ location.
        prd_path = (
            self.paths.root / "PRD.json"
            if (self.paths.root / "PRD.json").exists()
            else self.paths.state / "prd.json"
        )
        progress_path = (
            self.paths.root / "PROGRESS.md"
            if (self.paths.root / "PROGRESS.md").exists()
            else self.paths.state / "progress.md"
        )
        decisions_path = (
            self.paths.root / "DECISIONS.md"
            if (self.paths.root / "DECISIONS.md").exists()
            else self.paths.state / "decisions.md"
        )

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
            prompt_file = cfg.get("prompt") or f"{n}.md"
            prompt_preview = ""
            try:
                prompt_path = self.paths.prompts / prompt_file
                if prompt_path.exists():
                    prompt_preview = " ".join(prompt_path.read_text(encoding="utf-8").strip().split())[:220]
            except Exception as exc:
                log_exception(f"orchestration.agent._collect_context.roster_prompt.{n}", exc)
            roster_lines.append(
                f"- {marker} {n} :: {role} "
                f"(prompt={prompt_file}; prompt_preview={prompt_preview or '(missing)'})"
            )
        roster_text = "\n".join(roster_lines) or "(no agents registered yet)"

        # Blackboard digest: filter by the stage's declared `inputs` when
        # provided, otherwise show the most recent global posts so the
        # agent has at least some peer context. Capped to keep prompts
        # bounded; tunable via clk.config.json::blackboard.
        # Context isolation for DELEGATE children: a delegated subtask must NOT
        # inherit the caller's blackboard. Note an empty selector list does
        # NOT isolate (digest() returns recent global posts when selectors are
        # falsy), so this needs its own explicit branch.
        if extra.get("delegate_isolated") or str(extra.get("phase") or "") == "delegate":
            bb_digest = (
                "Blackboard digest: (isolated — this is a delegated subtask; "
                "peer context is intentionally withheld. Work only from the "
                "task described in your objective.)"
            )
        else:
            bb_cfg = (self.clk_cfg.get("blackboard") or {})
            bb_inputs = list(extra.get("blackboard_inputs") or [])
            # Allow the chief to widen the digest via stage metadata flag
            # ``include_full_blackboard`` (carried through ``extra``).
            if extra.get("include_full_blackboard"):
                bb_inputs = []
            try:
                bb_digest = _blackboard.digest(
                    self.paths,
                    selectors=bb_inputs,
                    max_posts=int(bb_cfg.get("digest_max_posts") or 20),
                    max_chars_per_post=int(bb_cfg.get("digest_max_chars_per_post") or 800),
                )
            except Exception as exc:
                log_exception("orchestration.agent._collect_context.blackboard", exc)
                bb_digest = "Blackboard digest: (unavailable)"

        # Casting-rejection feedback: surface duplicate-prevention misses
        # so the chief learns from them on the next dispatch.
        try:
            casting_feedback = _read_recent_casting_rejections(self.paths)
        except Exception as exc:
            log_exception("orchestration.agent._collect_context.casting_feedback", exc)
            casting_feedback = ""

        # Cross-iteration scratchpad: inject PROGRESS.md content as "notes"
        notes = ""
        notes_path = self.paths.root / "PROGRESS.md"
        if notes_path.exists():
            try:
                raw_notes = notes_path.read_text(encoding="utf-8")
                if len(raw_notes) > 3000:
                    raw_notes = raw_notes[-3000:]
                notes = raw_notes
            except Exception as exc:
                log_exception("orchestration.agent._collect_context.notes", exc)

        # Working checklist: inject THIS author's own mutable TODOS list so it
        # can review and re-emit an updated checklist this turn. Per-author, so
        # peers' lists stay out of the way (that is what the blackboard is for).
        try:
            todos = _todos.render_todos(
                _todos.todos_for(self.paths, str(extra.get("agent") or ""))
            )
        except Exception as exc:
            log_exception("orchestration.agent._collect_context.todos", exc)
            todos = "(no todos yet)"

        # Outputs contract: convert the stage_outputs list into a concrete,
        # agent-visible instruction block so workers know BEFORE they write
        # their first response which POST PRODUCES keys are required. Without
        # this the agent only learns about the contract through a rejection.
        stage_outputs_list = list(extra.get("stage_outputs") or [])
        if stage_outputs_list:
            produces_line = ", ".join(stage_outputs_list)
            outputs_contract = (
                f"REQUIRED OUTPUT CONTRACT — you MUST satisfy these keys:\n"
                f"  {produces_line}\n"
                f"Each key must appear in at least one POST block's PRODUCES "
                f"line. Exact format:\n"
                f"  POST: finding\n"
                f"  PRODUCES: {produces_line}\n"
                f"  BODY:\n"
                f"  <your summary here>\n"
                f"  END_POST\n"
                "Omitting this causes the harness to reject your response and "
                "re-dispatch you."
            )
        else:
            outputs_contract = ""

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
            "cycle_context": str(extra.get("cycle_context") or ""),
            "stop_when": str(extra.get("stop_when") or ""),
            "current_roster": roster_text,
            "blackboard_digest": bb_digest,
            "casting_feedback": casting_feedback or "(none)",
            "notes": notes,
            "todos": todos,
            "outputs_contract": outputs_contract,
        }
        # ``telemetry`` is a live counter object threaded through ``extra`` for
        # the dispatch hooks — it is not a template variable, so keep it out of
        # the prompt context (otherwise it would be str()'d into the prompt).
        ctx.update({
            k: v for k, v in extra.items()
            if k not in ctx and k != "telemetry"
        })
        return ctx

    def _safe_substitute(self, template_text: str, ctx: Dict[str, Any]) -> str:
        try:
            return Template(template_text).safe_substitute({k: str(v) for k, v in ctx.items()})
        except Exception as exc:
            log_exception("orchestration.agent._safe_substitute", exc)
            return template_text
