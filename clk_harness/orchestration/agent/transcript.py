"""Agent run records and response transcript processing.

The dataclasses that describe a dispatch (:class:`AgentSpec`,
:class:`AgentRun`), the observer hook, and the mixin that parses the
provider response transcript — POST blocks, PROPOSE blocks, ACTION
blocks — and persists the run history.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Dict, FrozenSet, List, Optional

from ...git_ops import add_all, has_changes, head_sha, is_repo
from ...git_ops import commit as git_commit
from ...log import get_logger, log_exception
from ...providers import AgentResponse
from ...utils.activity_log import log_event
from .. import actions as _actions
from .. import blackboard as _blackboard
from .. import casting as _casting
from .. import todos as _todos

if TYPE_CHECKING:
    import threading

    from ...config import Paths

logger = get_logger(__name__)


@dataclass
class AgentSpec:
    name: str
    prompt_file: str
    provider: Optional[str] = None
    role: str = ""
    capabilities: List[str] = field(default_factory=list)

    @classmethod
    def from_config(cls, name: str, cfg: Dict[str, Any]) -> "AgentSpec":
        return cls(
            name=name,
            prompt_file=cfg.get("prompt") or f"{name}.md",
            provider=cfg.get("provider"),
            role=cfg.get("role", ""),
            capabilities=list(cfg.get("capabilities") or []),
        )


@dataclass
class AgentRun:
    agent: str
    objective: str
    response: AgentResponse
    started_at: str
    finished_at: str
    files_written: List[str] = field(default_factory=list)
    # Number of file-mutating ACTIONs (write/edit/append/delete) the harness
    # actually applied for this run. Used by the no-op guard: a producing
    # stage that applied zero mutations is re-dispatched with an escalating
    # repair preamble (descriptions alone do nothing).
    file_mutations_applied: int = 0
    # True when the runner already created a git commit for this run's
    # actions; downstream consumers (WorkflowRunner._commit) skip in
    # that case to avoid double-committing the same diff.
    committed: bool = False
    # Blackboard post ids written by this run via POST blocks. The
    # workflow runner uses these to verify ``WorkflowStage.outputs``
    # contracts and to drive review-stage digests.
    posts: List[str] = field(default_factory=list)


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


class TranscriptMixin:
    """Response-transcript processing mixed into ``AgentRunner``.

    Parses POST / PROPOSE_ROLE / PROPOSE_WORKFLOW / ACTION blocks out of
    the provider response, applies their effects, and persists the run
    history under ``.clk/runs`` and ``agent_memory.jsonl``.
    """

    paths: "Paths"
    agents_cfg: Dict[str, Any]
    clk_cfg: Dict[str, Any]
    observer: Optional[AgentObserver]
    _proposals_lock: "threading.RLock"
    _todos_lock: "threading.Lock"
    _META_PHASES: FrozenSet[str]

    if TYPE_CHECKING:
        # Provided by AgentRunner (runner.py); declared here so annotated
        # mixin methods type-check without a runtime import cycle.
        def _dispatch_once(
            self,
            agent_name: str,
            objective: str,
            *,
            extra: Optional[Dict[str, Any]] = None,
            dry_run: Optional[bool] = None,
        ) -> "AgentRun": ...

        def _observer_log(self, line: str) -> None: ...

    # -- casting -----------------------------------------------------------

    def _apply_proposals(self, run: AgentRun) -> None:
        text = (run.response.text or "")
        if not text or ("PROPOSE_ROLE" not in text and "PROPOSE_WORKFLOW" not in text):
            return
        cap = int(((self.clk_cfg.get("casting") or {}).get("max_dynamic_roles"))
                  or _casting.DEFAULT_MAX_DYNAMIC_ROLES)
        observer = self.observer

        def _on_change(name: str, status: str) -> None:
            logger.info(f"casting :: {name} :: {status}")
            if observer is not None:
                try:
                    observer.roster_changed(name, status)
                except Exception as exc:
                    log_exception("orchestration.agent.observer.roster_changed", exc)

        with self._proposals_lock:
            result = _casting.apply_response_proposals(
                self.paths,
                text,
                agents_cfg=self.agents_cfg,
                max_dynamic=cap,
                source_agent=run.agent,
                on_change=_on_change,
            )
        if not result.is_empty():
            logger.info(f"casting from {run.agent}: {result.summary()}")

    def _apply_posts(self, run: AgentRun, extra: Dict[str, Any]) -> None:
        """Persist any POST blocks the agent emitted onto the blackboard.

        Stage_id / workflow are taken from ``extra`` so a post made
        during a workflow stage records its provenance and the workflow
        runner can verify the stage's declared outputs against it.

        When a question post carries a ``target_agent`` and
        ``urgency=blocking``, the harness dispatches the named agent to
        answer the question synchronously before this run returns.
        That makes the asker's worker effectively block on the answer,
        which gets posted back to the blackboard with ``post_type:
        answer`` and ``consumes: [<question_id>]``.

        A context-isolated DELEGATE child runs with ``suppress_posts`` (and
        ``phase == "delegate"``): its own POST blocks are NOT persisted to the
        shared board — only its distilled result returns to the caller as a
        single ``delegate_result`` post created by ``_apply_delegate``.
        """
        if str(extra.get("phase") or "") == "delegate" or extra.get("suppress_posts"):
            return
        text = (run.response.text or "")
        if not text or "POST:" not in text:
            return
        try:
            posted = _blackboard.apply_post_blocks(
                self.paths,
                text,
                author=run.agent,
                stage_id=str(extra.get("stage_id") or ""),
                workflow=str(extra.get("workflow") or ""),
            )
        except Exception as exc:
            log_exception("orchestration.agent._apply_posts", exc)
            return
        for p in posted:
            if p.id and p.id not in run.posts:
                run.posts.append(p.id)
        # Route blocking questions: dispatch the target agent inline so
        # the asker effectively sees the answer in subsequent rounds.
        try:
            self._route_blocking_questions(run, posted, extra)
        except Exception as exc:
            log_exception("orchestration.agent._apply_posts.route_qa", exc)

    def _route_blocking_questions(
        self,
        run: AgentRun,
        posted: List["_blackboard.Post"],
        extra: Dict[str, Any],
    ) -> None:
        """Dispatch peer agents to answer ``POST: question`` blocks.

        Skipped entirely when there are no question posts targeted at a
        peer, when we're already inside a Q&A chain that has exhausted
        its depth budget, or when the dispatcher is itself in a meta-
        phase (consensus, recovery, etc.).
        """
        questions = [
            p for p in posted
            if (p.post_type or "").lower() == "question"
            and (p.target_agent or "").strip()
            and (p.urgency or "blocking").lower() == "blocking"
        ]
        if not questions:
            return
        if str(extra.get("phase") or "") in self._META_PHASES:
            return
        tel = extra.get("telemetry")
        cfg = self.clk_cfg.get("robustness") or {}
        max_depth = int(cfg.get("max_qa_depth") or 3)
        chain: List[str] = list(extra.get("qa_chain") or [])
        if len(chain) >= max_depth:
            log_event(
                self.paths,
                "qa_chain_capped",
                agent=run.agent,
                depth=len(chain),
                max_depth=max_depth,
                chain=list(chain),
            )
            return
        agents_known = set((self.agents_cfg.get("agents") or {}).keys())
        for q in questions:
            target = q.target_agent.strip()
            if not target or target not in agents_known:
                log_event(
                    self.paths,
                    "qa_target_unknown",
                    agent=run.agent,
                    target=target,
                    question_id=q.id,
                )
                continue
            if target == run.agent:
                # Self-questions don't need routing.
                continue
            if target in chain:
                log_event(
                    self.paths,
                    "qa_chain_cycle",
                    agent=run.agent,
                    target=target,
                    chain=list(chain),
                )
                continue
            next_chain = chain + [run.agent]
            answer_objective = (
                f"Peer question routed by the harness.\n\n"
                f"Asker: `{run.agent}` (stage `{extra.get('stage_id') or '?'}`)\n"
                f"Question id: `{q.id}`\n\n"
                f"Question:\n{q.body}\n\n"
                "Answer this directly. Emit a POST: answer block whose\n"
                f"CONSUMES list contains `{q.id}`. Keep the body focused "
                "on what the asker needs to make progress — do not start "
                "a new sub-thread of questions of your own."
            )
            log_event(
                self.paths,
                "qa_dispatch",
                agent=run.agent,
                target=target,
                question_id=q.id,
                chain=next_chain,
                urgency=q.urgency or "blocking",
            )
            self._observer_log(
                f"qa :: {run.agent} → {target} :: {q.id[:32]}"
            )
            if tel is not None:
                try:
                    tel.add_qa_exchange()
                except Exception as _exc:
                    logger.debug("telemetry add_qa_exchange failed: %s", _exc)
            self._dispatch_once(
                target,
                answer_objective,
                extra={
                    "phase": "qa_answer",
                    "qa_chain": next_chain,
                    "qa_question_id": q.id,
                    "qa_asker": run.agent,
                    "stage_id": extra.get("stage_id"),
                    "workflow": extra.get("workflow"),
                },
                dry_run=self.clk_cfg.get("dry_run", False),
            )

    def _apply_todos(self, run: AgentRun, extra: Dict[str, Any]) -> None:
        """Persist a ``TODOS:`` block the agent emitted as its live checklist.

        The checklist is mutable and per-author: the latest block overwrites
        this author's previous list (last-write-wins), and is re-injected into
        this author's next prompt via the ``$todos`` placeholder. Skipped for
        meta phases (consensus / qa_answer / delegate / etc.) so a subtask's
        stray block can't clobber the driving agent's checklist.
        """
        if str(extra.get("phase") or "") in self._META_PHASES:
            return
        text = run.response.text or ""
        if "TODOS:" not in text:
            return
        try:
            with self._todos_lock:
                _todos.apply_todos_blocks(
                    self.paths,
                    text,
                    author=run.agent,
                    stage_id=str(extra.get("stage_id") or ""),
                    workflow=str(extra.get("workflow") or ""),
                )
        except Exception as exc:
            log_exception("orchestration.agent._apply_todos", exc)

    def _apply_delegate(self, run: AgentRun, extra: Dict[str, Any]) -> None:
        """Spawn context-isolated DELEGATE children for a bounded subtask.

        Each ``DELEGATE:`` block dispatches the named target once, with the
        caller's blackboard withheld (``delegate_isolated``) and the child's
        own POST blocks suppressed. The child MAY do real work — its ACTION
        blocks execute and commit under its own name. The child's distilled
        result returns to the caller as a single ``delegate_result`` post it
        sees on its next turn.

        Skipped from any meta phase (so a child cannot itself delegate) and
        bounded by ``max_delegate_depth`` (default 1), a per-turn cap, and
        unknown-target / self / cycle guards — mirroring the blocking-Q&A
        routing in ``_route_blocking_questions``.
        """
        text = run.response.text or ""
        if "DELEGATE:" not in text:
            return
        if str(extra.get("phase") or "") in self._META_PHASES:
            return
        props = _casting.parse_delegate_proposals(text)
        if not props:
            return
        cfg = self.clk_cfg.get("robustness") or {}
        max_depth = int(cfg.get("max_delegate_depth") or 1)
        chain: List[str] = list(extra.get("delegate_chain") or [])
        if len(chain) >= max_depth:
            log_event(
                self.paths,
                "delegate_chain_capped",
                agent=run.agent,
                depth=len(chain),
                max_depth=max_depth,
                chain=list(chain),
            )
            return
        max_per_turn = int(cfg.get("max_delegates_per_turn") or 2)
        result_cap = int(cfg.get("delegate_result_max_chars") or 2000)
        known = set((self.agents_cfg.get("agents") or {}).keys())
        next_chain = chain + [run.agent]
        for prop in props[:max_per_turn]:
            target = prop.target
            if not target or target not in known:
                log_event(
                    self.paths,
                    "delegate_target_unknown",
                    agent=run.agent,
                    target=target,
                    name=prop.name,
                )
                continue
            if target == run.agent or target in chain:
                log_event(
                    self.paths,
                    "delegate_chain_cycle",
                    agent=run.agent,
                    target=target,
                    chain=list(chain),
                )
                continue
            req_id = (
                f"deleg-{run.agent}-"
                f"{datetime.now().strftime('%Y%m%dT%H%M%S%f')}-{prop.name}"
            )
            child_obj = (
                "Delegated, context-isolated subtask.\n\n"
                f"Requested by `{run.agent}`. You do NOT see the caller's "
                "blackboard — work only from what is written here. You MAY do "
                "real work (emit ACTION blocks to change files); they will be "
                "committed under your name. When done, end with a concise, "
                "self-contained summary of the result the caller needs — that "
                "summary is all that is returned to them.\n"
            )
            if prop.context:
                child_obj += f"\nContext:\n{prop.context}\n"
            child_obj += f"\nTask:\n{prop.objective}\n"
            log_event(
                self.paths,
                "delegate_dispatch",
                agent=run.agent,
                target=target,
                name=prop.name,
                req_id=req_id,
                chain=next_chain,
            )
            self._observer_log(f"delegate :: {run.agent} → {target} :: {prop.name}")
            try:
                child = self._dispatch_once(
                    target,
                    child_obj,
                    extra={
                        "phase": "delegate",
                        "delegate_chain": next_chain,
                        "delegate_isolated": True,
                        "suppress_posts": True,
                        "agent": target,
                        "stage_id": extra.get("stage_id"),
                        "workflow": extra.get("workflow"),
                    },
                    dry_run=self.clk_cfg.get("dry_run", False),
                )
            except Exception as exc:
                log_exception("orchestration.agent._apply_delegate.dispatch", exc)
                continue
            body = ""
            if child is not None and child.response is not None:
                body = (child.response.text or "").strip()
            if len(body) > result_cap:
                body = body[:result_cap].rstrip() + " …"
            try:
                p = _blackboard.post(
                    self.paths,
                    author=run.agent,
                    body=body or "(delegate produced no output)",
                    post_type="delegate_result",
                    consumes=[req_id],
                    produces=[f"delegate:{prop.name}"],
                    stage_id=str(extra.get("stage_id") or ""),
                    workflow=str(extra.get("workflow") or ""),
                    slug_hint=f"delegate-{prop.name}",
                )
                if p.id and p.id not in run.posts:
                    run.posts.append(p.id)
            except Exception as exc:
                log_exception("orchestration.agent._apply_delegate.post", exc)

    def _apply_actions(self, run: AgentRun, extra: Optional[Dict[str, Any]] = None) -> None:
        """Execute ACTION blocks; merge harness-written files back into the run."""
        extra = extra or {}
        tel = extra.get("telemetry")
        text = (run.response.text or "")
        if not text or "ACTION:" not in text and "ACTION :" not in text:
            return
        result = _actions.apply_actions(
            self.paths,
            text,
            agent_name=run.agent,
            clk_cfg=self.clk_cfg,
        )
        # Record how many file mutations actually landed (drives the no-op
        # guard even when ACTION blocks were present but all skipped).
        run.file_mutations_applied = len(result.files_written) + len(result.files_deleted)
        if tel is not None:
            try:
                tel.add_actions(len(result.files_written) + len(result.files_deleted)
                                + len(result.commands_run))
                tel.add_files(len(result.files_written))
            except Exception as _exc:
                logger.debug("telemetry add_actions/add_files failed: %s", _exc)
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
        logger.info(f"actions from {run.agent}: {result.summary()}")
        # Annotate the response so the TUI can show it in the log pane:
        # we tack a short summary onto the text preview path.
        if result.commands_run or result.errors:
            for cmd, out in zip(result.commands_run, result.command_outputs):
                logger.info(f"actions[{run.agent}] $ {cmd}")
                if out.strip():
                    for line in out.strip().splitlines()[:6]:
                        logger.info(f"actions[{run.agent}]   {line[:200]}")
            for err in result.errors:
                logger.warning(f"actions[{run.agent}] !! {err}")
        # Auto-commit any file changes from this batch so the git log
        # has a per-agent-run granularity. Only fires when this run
        # actually wrote (or deleted) files.
        if (result.files_written or result.files_deleted) and self.clk_cfg.get("auto_commit", True):
            self._commit_action_batch(run, result, extra)

    def _commit_action_batch(
        self,
        run: AgentRun,
        result: "_actions.ActionResult",
        extra: Optional[Dict[str, Any]] = None,
    ) -> None:
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
                tel = (extra or {}).get("telemetry")
                if tel is not None:
                    try:
                        tel.add_commit()
                    except Exception as _exc:
                        logger.debug("telemetry add_commit failed: %s", _exc)
                logger.info(
                    f"commit: [{run.agent}] {len(result.files_written)} files, "
                    f"{len(result.files_deleted)} deletes"
                )
                log_event(
                    self.paths,
                    "git_commit",
                    agent=run.agent,
                    objective=run.objective[:200],
                    sha=head_sha(self.paths.root),
                    files_written=list(result.files_written),
                    files_deleted=list(result.files_deleted),
                    commands_run=list(result.commands_run),
                    tokens_total=tok_total,
                )
        except Exception as exc:
            log_exception("orchestration.agent._commit_action_batch", exc)

    def _record(self, run: AgentRun, prompt: str, provider_desc: str) -> None:
        try:
            self.paths.state.mkdir(parents=True, exist_ok=True)
            mem = self.paths.state / "agent_memory.jsonl"
            payload = {
                "agent": run.agent,
                "run_id": f"{run.started_at.replace(':','-')}-{run.agent}",
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
