"""Agent runner.

Loads a prompt template, renders it against the current state, and
invokes the configured provider. The runner is intentionally thin -
heavier orchestration lives in :mod:`workflow` and the loops.
"""

from __future__ import annotations

import json
import sys
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from string import Template
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes, head_sha, is_repo
from ..providers import AgentProvider, AgentRequest, AgentResponse, load_provider
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from . import casting as _casting
from . import actions as _actions
from . import blackboard as _blackboard
from . import response_quality as _response_quality


def _read_recent_casting_rejections(paths: Paths, *, limit: int = 8) -> str:
    """Render the most recent role/workflow rejections into a short feedback
    block. Lets the chief see "you tried to create X but Y already exists"
    without us hardening the prompt further.
    """
    log_path = paths.state / "casting.log"
    if not log_path.exists():
        return ""
    try:
        raw_lines = log_path.read_text(encoding="utf-8").strip().splitlines()
    except Exception:
        return ""
    rows: List[Dict[str, Any]] = []
    for line in reversed(raw_lines):
        try:
            obj = json.loads(line)
        except Exception:
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
        # Serialises agents_cfg mutations from _apply_proposals so parallel
        # workflow stages don't race when both emit PROPOSE_ROLE blocks.
        # RLock so consensus coalescing (which calls run() recursively) works.
        self._proposals_lock = threading.RLock()
        # Lock around meta-prompt cache reads/writes so parallel stages
        # racing to draft the same dispatch prompt don't corrupt the file.
        self._meta_cache_lock = threading.Lock()

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

    # Phases whose dispatches must never re-trigger the auto-consensus or
    # quality-retry layers. Otherwise consensus coalescing, checkpoint
    # verdicts, recovery dispatches, and the critic-judge inner loop
    # would all recurse into themselves.
    _META_PHASES = frozenset({
        "consensus_sample",
        "consensus",
        "checkpoint",
        "recovery",
        "draft_dispatch_prompt",
        "draft_role_prompt",
        "qa_answer",
        "refine_critic",
        "refine_worker",
    })

    def run(
        self,
        agent_name: str,
        objective: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        dry_run: Optional[bool] = None,
    ) -> AgentRun:
        """Public dispatch entry point.

        Wraps :meth:`_dispatch_once` with two robustness layers:

        * **Proactive auto-consensus** (`robustness.auto_consensus`) —
          stages marked ``careful: true`` (or all stages, when set to
          ``"always"``) fan into N stochastic samples and a chief
          coalescing pass instead of a single dispatch.
        * **Quality-driven re-dispatch** — after a normal dispatch, the
          response is scored against ``response_quality``; recoverable
          failures (empty, malformed, contract-missing, low-confidence)
          trigger a re-run with a repair preamble, escalating to
          consensus on the final retry.

        Both layers are gated by ``clk.config.json::robustness`` and
        bypassed for dispatches whose ``extra.phase`` indicates a
        meta-path (consensus coalescing, recovery, checkpoint, etc.) so
        the harness never loops on itself.
        """
        extra_dict: Dict[str, Any] = dict(extra or {})
        phase = str(extra_dict.get("phase") or "")
        in_meta = phase in self._META_PHASES
        is_dry = self.clk_cfg.get("dry_run", False) if dry_run is None else dry_run

        if not in_meta and not is_dry and self._should_auto_consensus(agent_name, extra_dict):
            return self._dispatch_auto_consensus(
                agent_name,
                objective,
                extra=extra_dict,
                dry_run=dry_run,
                reason="auto_consensus_proactive",
            )

        if in_meta or is_dry:
            return self._dispatch_once(agent_name, objective, extra=extra_dict, dry_run=dry_run)

        return self._dispatch_with_quality_loop(
            agent_name, objective, extra=extra_dict, dry_run=dry_run
        )

    def _dispatch_with_quality_loop(
        self,
        agent_name: str,
        objective: str,
        *,
        extra: Dict[str, Any],
        dry_run: Optional[bool],
    ) -> AgentRun:
        """Quality-validated dispatch wrapper.

        Runs :meth:`_dispatch_once`, scores the response, and re-runs
        the worker with a repair preamble when the verdict is
        recoverable. Escalates to ``_dispatch_auto_consensus`` on the
        final retry when ``auto_consensus`` is not ``"off"``.
        """
        cfg = self.clk_cfg.get("robustness") or {}
        max_retries = int(cfg.get("max_quality_retries") or 0)
        min_chars = int(cfg.get("min_response_chars") or 40)
        auto_consensus_mode = str(cfg.get("auto_consensus") or "off").lower()
        expected_outputs = list(extra.get("stage_outputs") or [])

        attempt = 0
        current_objective = objective
        last_run: Optional[AgentRun] = None
        while True:
            attempt += 1
            attempt_extra = dict(extra)
            attempt_extra["quality_attempt"] = attempt
            run = self._dispatch_once(
                agent_name, current_objective, extra=attempt_extra, dry_run=dry_run
            )
            last_run = run
            if not run.response.ok:
                return run
            try:
                q = _response_quality.score(
                    run.response.text,
                    min_chars=min_chars,
                    expected_outputs=expected_outputs,
                )
            except Exception as exc:
                log_exception("orchestration.agent._dispatch_with_quality_loop.score", exc)
                return run
            if q.ok or not q.recoverable or attempt > max_retries:
                if not q.ok:
                    log_event(
                        self.paths,
                        "agent_quality_final",
                        agent=agent_name,
                        attempt=attempt,
                        ok=q.ok,
                        recoverable=q.recoverable,
                        flags=list(q.flags),
                        reasons=list(q.reasons),
                        score=q.score,
                        confidence=q.confidence,
                        needs_review=q.needs_review,
                    )
                return run
            log_event(
                self.paths,
                "agent_quality_retry",
                agent=agent_name,
                attempt=attempt,
                next_attempt=attempt + 1,
                max_attempts=max_retries + 1,
                flags=list(q.flags),
                reasons=list(q.reasons),
                score=q.score,
                confidence=q.confidence,
                needs_review=q.needs_review,
            )
            self._observer_log(
                f"quality :: {agent_name} :: retry {attempt}/{max_retries} "
                f"flags={','.join(q.flags) or '?'} score={q.score:.2f}"
            )
            # On the final retry, optionally escalate to a consensus
            # fan-out rather than another single-shot retry — that way
            # we get sub-sub-agents on actually-shaky outputs even when
            # the stage isn't marked careful.
            if attempt == max_retries and auto_consensus_mode != "off":
                return self._dispatch_auto_consensus(
                    agent_name,
                    objective,
                    extra=extra,
                    dry_run=dry_run,
                    reason=f"quality_escalation:{','.join(q.flags)}",
                )
            current_objective = q.repair_hint() + "\n\nOriginal objective:\n" + objective
        return last_run  # unreachable

    def _should_auto_consensus(self, agent_name: str, extra: Dict[str, Any]) -> bool:
        """Proactive auto-consensus trigger check."""
        cfg = self.clk_cfg.get("robustness") or {}
        mode = str(cfg.get("auto_consensus") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        # Never fan-out the chief on its own meta-paths.
        if agent_name == "chief":
            return False
        if mode == "always":
            return True
        # on_careful: only when the stage explicitly opted in.
        if mode == "on_careful":
            return bool(extra.get("careful"))
        return False

    def _dispatch_auto_consensus(
        self,
        agent_name: str,
        objective: str,
        *,
        extra: Dict[str, Any],
        dry_run: Optional[bool],
        reason: str = "auto_consensus",
    ) -> AgentRun:
        """Fan-out a single dispatch into N stochastic samples + coalesce.

        Reuses :meth:`_run_consensus_sample` (same code path as
        ``PROPOSE_CONSENSUS``) so the sampling, logging, and parallelism
        behavior is identical. The chief is invoked to coalesce.
        """
        cfg = self.clk_cfg.get("consensus") or {}
        sample_count = max(1, min(int(cfg.get("max_samples") or 3), 6))
        max_parallel = max(1, int(cfg.get("max_parallel") or 4))
        name = f"auto_{agent_name}_{datetime.now().strftime('%H%M%S%f')}"
        log_event(
            self.paths,
            "consensus_started",
            agent=agent_name,
            name=name,
            objective=objective,
            agents=[agent_name] * sample_count,
            samples=sample_count,
            max_parallel=max_parallel,
            trigger=reason,
        )
        self._observer_log(
            f"consensus :: auto/{agent_name} :: starting {sample_count} samples "
            f"(reason={reason})"
        )
        results: List[Dict[str, Any]] = []
        with ThreadPoolExecutor(max_workers=min(max_parallel, sample_count)) as pool:
            futs = {
                pool.submit(self._run_consensus_sample, name, idx + 1, agent_name, objective): idx + 1
                for idx in range(sample_count)
            }
            for fut in as_completed(futs):
                idx = futs[fut]
                try:
                    results.append(fut.result())
                except Exception as exc:
                    log_exception("orchestration.agent._dispatch_auto_consensus.sample", exc)
                    results.append({
                        "sample": idx, "agent": agent_name, "ok": False,
                        "error": str(exc), "text": "",
                    })
        results.sort(key=lambda r: int(r.get("sample") or 0))
        log_event(
            self.paths,
            "consensus_samples_completed",
            agent=agent_name,
            name=name,
            results=results,
            trigger=reason,
        )
        coalesce_objective = self._consensus_coalesce_objective(name, objective, results)
        coalesced = self._dispatch_once(
            "chief",
            coalesce_objective,
            extra={
                "phase": "consensus",
                "consensus_name": name,
                "consensus_trigger": reason,
                "stage_id": extra.get("stage_id"),
                "workflow": extra.get("workflow"),
            },
            dry_run=dry_run,
        )
        log_event(
            self.paths,
            "consensus_coalesced",
            agent="chief",
            name=name,
            ok=coalesced.response.ok,
            response_text=coalesced.response.text or "",
            error=coalesced.response.error,
            trigger=reason,
        )
        # Re-label so downstream logging shows the auto path, not "chief".
        coalesced.agent = agent_name
        return coalesced

    def _dispatch_once(
        self,
        agent_name: str,
        objective: str,
        *,
        extra: Optional[Dict[str, Any]] = None,
        dry_run: Optional[bool] = None,
    ) -> AgentRun:
        """Single provider dispatch with provider-level retry only.

        This was the body of :meth:`run` before the robustness layers
        wrapped it. Keep it self-contained so consensus / refine /
        recovery paths can call it without re-entering the wrappers.
        """
        agent = self.get_agent(agent_name)
        provider = self.get_provider(agent.provider)
        prompt = self.render_prompt(agent, objective, extra)
        is_dry = self.clk_cfg.get("dry_run", False) if dry_run is None else dry_run

        observer = self.observer
        paths = self.paths

        def _on_progress(kind: str, message: str) -> None:
            # Log the provider subprocess stream verbatim. This log is
            # intended for post-run forensics, so detail is more useful
            # than compactness here.
            try:
                extra: Dict[str, Any] = {}
                if kind == "command":
                    try:
                        parsed = json.loads(message)
                        if isinstance(parsed, dict):
                            extra = parsed
                    except Exception:
                        extra = {}
                log_event(
                    paths,
                    ("http_" + kind[5:] if kind.startswith("http_") else "subprocess_" + kind),
                    agent=agent.name,
                    message=message,
                    message_chars=len(message or ""),
                    **extra,
                )
            except Exception:
                pass
            if observer is None:
                return
            try:
                observer.progress(agent.name, kind, message)
            except Exception as exc:
                log_exception("orchestration.agent.observer.progress", exc)

        timeout_s = int((self.clk_cfg.get("provider_timeout_s") or 300))
        no_output_timeout_s = int((self.clk_cfg.get("provider_no_output_timeout_s") or 0))
        retry_cfg = self.clk_cfg.get("provider_retry") or {}
        max_retries = int(retry_cfg.get("max_retries", self.clk_cfg.get("provider_max_retries", 1)) or 0)
        backoff_s = float(retry_cfg.get("backoff_s", self.clk_cfg.get("provider_retry_backoff_s", 5)) or 0)
        req = AgentRequest(
            agent=agent.name,
            prompt=prompt,
            workdir=self.paths.root,
            dry_run=bool(is_dry),
            timeout_s=timeout_s,
            no_output_timeout_s=no_output_timeout_s,
            on_progress=_on_progress,
            capabilities=list(agent.capabilities or []),
        )
        started = datetime.now().isoformat(timespec="seconds")
        run_id = f"{started.replace(':','-')}-{agent.name}"
        run_dir_rel = f".clk/runs/{run_id}"
        log_event(
            self.paths,
            "agent_dispatch",
            agent=agent.name,
            action="dispatch",
            objective=objective,
            objective_chars=len(objective or ""),
            workflow=(extra or {}).get("workflow"),
            stage_id=(extra or {}).get("stage_id"),
            iteration=(extra or {}).get("iteration"),
            phase=(extra or {}).get("phase"),
            provider=provider.describe(),
            dry_run=bool(is_dry),
            timeout_s=timeout_s,
            no_output_timeout_s=no_output_timeout_s,
            prompt_file=agent.prompt_file,
            role=agent.role,
            capabilities=list(agent.capabilities or []),
            run_id=run_id,
            max_retries=max_retries,
            retry_backoff_s=backoff_s,
        )
        log_event(
            self.paths,
            "prompt_sent",
            agent=agent.name,
            action="prompt_sent",
            prompt_chars=len(prompt),
            prompt_path=f"{run_dir_rel}/prompt.txt",
            prompt=prompt,
            run_id=run_id,
        )
        if self.observer is not None:
            try:
                self.observer.begin(agent.name, objective)
            except Exception as exc:
                log_exception("orchestration.agent.observer.begin", exc)
            try:
                self.observer.prompt_sent(agent.name, prompt)
            except Exception as exc:
                log_exception("orchestration.agent.observer.prompt_sent", exc)
        resp = AgentResponse(ok=False, error="provider_not_invoked")
        attempt = 0
        while True:
            attempt += 1
            log_event(
                self.paths,
                "provider_attempt",
                agent=agent.name,
                run_id=run_id,
                attempt=attempt,
                max_attempts=max_retries + 1,
                provider=provider.describe(),
            )
            try:
                resp = provider.invoke(req)
            except Exception as exc:
                log_exception(f"orchestration.agent.run[{agent_name}]", exc)
                resp = AgentResponse(ok=False, error=str(exc))
            if resp.ok or not self._should_retry_provider(resp.error or "") or attempt > max_retries:
                break
            log_event(
                self.paths,
                "provider_retry",
                agent=agent.name,
                run_id=run_id,
                attempt=attempt,
                next_attempt=attempt + 1,
                backoff_s=backoff_s,
                error=resp.error,
            )
            _on_progress(
                "retry",
                f"provider error '{resp.error}'; killed stalled process if present; "
                f"backing off {backoff_s:.1f}s then reissuing attempt {attempt + 1}/{max_retries + 1}",
            )
            if backoff_s > 0:
                time.sleep(backoff_s * (2 ** (attempt - 1)))
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
        log_event(
            self.paths,
            "agent_response",
            agent=agent.name,
            action="response_received",
            ok=run.response.ok,
            error=run.response.error,
            response_chars=len(run.response.text or ""),
            response_path=f"{run_dir_rel}/response.txt",
            response_text=run.response.text or "",
            tokens_total=int((run.response.usage or {}).get("total_tokens") or 0),
            tokens_in=int((run.response.usage or {}).get("input_tokens") or 0),
            tokens_out=int((run.response.usage or {}).get("output_tokens") or 0),
            usage_source=(run.response.usage or {}).get("source"),
            files_reported=list(run.files_written or []),
            run_id=run_id,
        )
        # Persist POST blocks to the blackboard before the rest of the
        # apply hooks. Posting is cheap and uncommitted, so it happens
        # even for dry-runs to keep the digest accurate during planning.
        self._apply_posts(run, extra or {})
        # Apply any PROPOSE_ROLE / PROPOSE_WORKFLOW blocks the agent
        # emitted. Mutates ``self.agents_cfg`` in place so the very next
        # stage that names a freshly-proposed role can dispatch to it.
        self._apply_proposals(run)
        self._apply_consensus(run, extra or {})
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

    def _should_retry_provider(self, error: str) -> bool:
        msg = (error or "").lower()
        retryable = [
            "no output for",
            "timeout after",
            "operation was aborted",
            # OpenRouter can report this routing/policy text transiently
            # even when a later identical request succeeds.
            "no endpoints available",
            "guardrail restrictions",
            "data policy",
            "connection reset",
            "temporarily unavailable",
            "try again",
            # HTTP 429 rate-limiting and HTTP 404 (OpenRouter: no endpoints temporarily available)
            "http 429",
            "http 404",
        ]
        non_retryable = [
            "api key",
            "authentication",
            "unauthorized",
            "forbidden",
            "cli not found",
        ]
        return any(s in msg for s in retryable) and not any(s in msg for s in non_retryable)

    def _observer_log(self, line: str) -> None:
        log(line)
        if self.observer is not None:
            try:
                self.observer.log(line)
            except Exception as exc:
                log_exception("orchestration.agent.observer.log", exc)

    def _apply_consensus(self, run: AgentRun, extra: Dict[str, Any]) -> None:
        text = run.response.text or ""
        if not text or "PROPOSE_CONSENSUS" not in text:
            return
        if str(extra.get("phase") or "") == "consensus":
            return
        proposals = _casting.parse_consensus_proposals(text)
        if not proposals:
            return
        cfg = self.clk_cfg.get("consensus") or {}
        max_samples = int(cfg.get("max_samples") or 6)
        max_parallel = int(cfg.get("max_parallel") or 4)
        for prop in proposals:
            agents = [a for a in prop.agents if a in (self.agents_cfg.get("agents") or {})]
            if not agents:
                agents = [run.agent]
            sample_count = min(max_samples, max(1, int(prop.copies or 3)))
            assignments = [agents[i % len(agents)] for i in range(sample_count)]
            log_event(
                self.paths,
                "consensus_started",
                agent=run.agent,
                name=prop.name,
                objective=prop.objective,
                agents=list(assignments),
                samples=sample_count,
                max_parallel=max_parallel,
            )
            self._observer_log(
                f"consensus :: {prop.name} :: starting {sample_count} samples "
                f"across {', '.join(sorted(set(assignments)))}"
            )
            results: List[Dict[str, Any]] = []
            with ThreadPoolExecutor(max_workers=max(1, min(max_parallel, sample_count))) as pool:
                futs = {
                    pool.submit(self._run_consensus_sample, prop.name, idx + 1, agent_name, prop.objective): (
                        idx + 1,
                        agent_name,
                    )
                    for idx, agent_name in enumerate(assignments)
                }
                for fut in as_completed(futs):
                    idx, agent_name = futs[fut]
                    try:
                        results.append(fut.result())
                    except Exception as exc:
                        log_exception("orchestration.agent._apply_consensus.sample", exc)
                        results.append({"sample": idx, "agent": agent_name, "ok": False, "error": str(exc), "text": ""})
            results.sort(key=lambda r: int(r.get("sample") or 0))
            log_event(
                self.paths,
                "consensus_samples_completed",
                agent=run.agent,
                name=prop.name,
                results=results,
            )
            self._observer_log(f"consensus :: {prop.name} :: samples complete; coalescing with chief")
            coalesce = self._consensus_coalesce_objective(prop.name, prop.objective, results)
            coalesced = self.run(
                "chief",
                coalesce,
                extra={"phase": "consensus", "consensus_name": prop.name},
            )
            log_event(
                self.paths,
                "consensus_coalesced",
                agent="chief",
                name=prop.name,
                ok=coalesced.response.ok,
                response_text=coalesced.response.text or "",
                error=coalesced.response.error,
            )
            self._observer_log(f"consensus :: {prop.name} :: coalesced by chief")

    def _run_consensus_sample(self, name: str, sample: int, agent_name: str, objective: str) -> Dict[str, Any]:
        label = f"{agent_name}#consensus{sample}"
        agent = self.get_agent(agent_name)
        provider = self.get_provider(agent.provider)
        sample_objective = (
            f"Stochastic consensus sample `{name}` #{sample}.\n\n"
            "Answer independently. Do not coordinate with other samples.\n\n"
            f"Consensus objective:\n{objective}"
        )
        prompt = self.render_prompt(agent, sample_objective, {"phase": "consensus_sample", "agent": agent_name})
        started = datetime.now().isoformat(timespec="seconds")
        run_id = f"{started.replace(':','-')}-{label}"
        timeout_s = int((self.clk_cfg.get("provider_timeout_s") or 300))
        no_output_timeout_s = int((self.clk_cfg.get("provider_no_output_timeout_s") or 0))

        def _progress(kind: str, message: str) -> None:
            log_event(
                self.paths,
                ("http_" + kind[5:] if kind.startswith("http_") else "subprocess_" + kind),
                agent=label,
                consensus=name,
                sample=sample,
                message=message,
                message_chars=len(message or ""),
            )
            if self.observer is not None:
                try:
                    self.observer.progress(label, kind, message)
                except Exception:
                    pass

        log_event(
            self.paths,
            "consensus_sample_dispatch",
            agent=label,
            base_agent=agent_name,
            consensus=name,
            sample=sample,
            objective=objective,
            provider=provider.describe(),
            run_id=run_id,
        )
        self._observer_log(
            f"consensus :: {name} :: sample #{sample} dispatching ({agent_name})"
        )
        if self.observer is not None:
            self.observer.begin(label, sample_objective)
            self.observer.prompt_sent(label, prompt)
        req = AgentRequest(
            agent=label,
            prompt=prompt,
            workdir=self.paths.root,
            dry_run=bool(self.clk_cfg.get("dry_run", False)),
            timeout_s=timeout_s,
            no_output_timeout_s=no_output_timeout_s,
            on_progress=_progress,
        )
        try:
            resp = provider.invoke(req)
        except Exception as exc:
            resp = AgentResponse(ok=False, error=str(exc))
        finished = datetime.now().isoformat(timespec="seconds")
        arun = AgentRun(agent=label, objective=sample_objective, response=resp, started_at=started, finished_at=finished)
        self._record(arun, prompt, provider.describe())
        if self.observer is not None:
            self.observer.end(label, arun)
        self._observer_log(
            f"consensus :: {name} :: sample #{sample} done "
            f"({'ok' if resp.ok else 'error: ' + (resp.error or '?')})"
        )
        log_event(
            self.paths,
            "consensus_sample_response",
            agent=label,
            base_agent=agent_name,
            consensus=name,
            sample=sample,
            ok=resp.ok,
            error=resp.error,
            response_text=resp.text or "",
        )
        return {"sample": sample, "agent": agent_name, "label": label, "ok": resp.ok, "error": resp.error, "text": resp.text or ""}

    def _consensus_coalesce_objective(self, name: str, objective: str, results: List[Dict[str, Any]]) -> str:
        parts = [
            f"Coalesce stochastic consensus `{name}` into one coherent response.",
            "",
            "Original consensus objective:",
            objective,
            "",
            "Samples:",
        ]
        for r in results:
            parts.append(f"\n--- sample {r.get('sample')} agent={r.get('agent')} ok={r.get('ok')} error={r.get('error') or ''} ---")
            parts.append((r.get("text") or "").strip() or "(no response)")
        parts.append("\nReturn a unified answer with agreements, disagreements, and the recommended decision.")
        return "\n".join(parts)

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
                    except Exception:
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
        key = self._meta_key("dispatch", agent_name, base_objective, inputs_key)
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
            objective = (
                f"Draft a tighter task prompt for the `{agent_name}` agent for the\n"
                f"objective below. Output ONLY the new objective text — no preamble,\n"
                f"no commentary. Keep it focused, concrete, and at most 6 sentences.\n"
                f"Reference any relevant blackboard posts the worker should consult.\n\n"
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
                "compared with existing roles."
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
            log(f"casting from {run.agent}: {result.summary()}")

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
        """
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
            "current_roster": roster_text,
            "blackboard_digest": bb_digest,
            "casting_feedback": casting_feedback or "(none)",
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
