"""Agent dispatch and provider invocation.

``AgentRunner.run`` is the public dispatch entry point; the robustness
layers (quality-driven re-dispatch, proactive auto-consensus) and the
consensus sampling machinery live here too. Prompt assembly is mixed in
from :mod:`.prompts`; response-transcript processing from
:mod:`.transcript`.
"""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Any, Dict, List, Optional

from ...config import Paths
from ...log import get_logger, log_exception
from ...providers import AgentProvider, AgentRequest, AgentResponse, load_provider
from ...utils.activity_log import log_event
from .. import casting as _casting
from .. import noop_guard as _noop_guard
from .. import response_quality as _response_quality
from .prompts import PromptsMixin
from .transcript import AgentObserver, AgentRun, AgentSpec, TranscriptMixin

logger = get_logger(__name__)


class AgentRunner(PromptsMixin, TranscriptMixin):
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
        import os as _os

        from ...config import DEFAULT_PROVIDERS
        # CLK_PROVIDER (set in the global .env) is an explicit, authoritative
        # choice, so it overrides the workspace's saved `active`. Precedence:
        # per-agent pin > env CLK_PROVIDER > providers.json active >
        # clk.config default_provider > shell.
        env_provider = (_os.environ.get("CLK_PROVIDER") or "").strip()
        target = (
            name
            or env_provider
            or self.providers_cfg.get("active")
            or self.clk_cfg.get("default_provider")
            or "shell"
        )
        prov_cfg = (self.providers_cfg.get("providers") or {}).get(target)
        if prov_cfg is None:
            # No saved block (e.g. an env-selected provider, or a dropped
            # block) -> use the built-in default block for known providers so
            # we still call the real provider instead of silently echoing.
            prov_cfg = (DEFAULT_PROVIDERS.get("providers") or {}).get(target)
        if prov_cfg is None:
            logger.warning(f"unknown provider '{target}', falling back to shell")
            target = "shell"
            prov_cfg = (self.providers_cfg.get("providers") or {}).get("shell") or {"type": "shell"}
        return load_provider(target, prov_cfg)

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
        # Mission-level chief dispatches: single-shot planning / gating that
        # must not recurse into consensus or the quality-retry loop.
        "charter",
        "mission_plan",
        "phase_gate",
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
        tel = extra.get("telemetry")

        # FM1 no-op guard: a producing stage that applies zero file mutations
        # is re-dispatched with an escalating repair preamble (descriptions
        # alone do nothing). Independent budget from the quality retries.
        expect_mutation = _noop_guard.is_mutation_expected(
            agent_name,
            outputs=expected_outputs,
            commit=bool(extra.get("commit")),
            cfg=self.clk_cfg,
        )
        max_noop = _noop_guard.max_redispatch(self.clk_cfg)
        noop_redispatches = 0

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
            # No-op check: a producing stage that returned substantive prose
            # but changed no files. (An empty/near-empty response is left to
            # the quality "empty" flag below — that is a different failure.)
            substantive = len((run.response.text or "").strip()) >= min_chars
            if (
                expect_mutation
                and substantive
                and run.file_mutations_applied == 0
                and noop_redispatches < max_noop
            ):
                noop_redispatches += 1
                if tel is not None:
                    try:
                        tel.add_noop_redispatch()
                    except Exception as _exc:
                        logger.debug("telemetry add_noop_redispatch failed: %s", _exc)
                log_event(
                    self.paths,
                    "agent_noop_redispatch",
                    agent=agent_name,
                    attempt=noop_redispatches,
                    max_attempts=max_noop,
                    stage_id=extra.get("stage_id"),
                    workflow=extra.get("workflow"),
                )
                self._observer_log(
                    f"noop :: {agent_name} :: changed no files; "
                    f"re-dispatch {noop_redispatches}/{max_noop}"
                )
                preamble = _noop_guard.repair_preamble(
                    noop_redispatches, target=str(extra.get("expected_path") or "")
                )
                current_objective = preamble + "\n\nOriginal objective:\n" + objective
                continue
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
            if tel is not None:
                try:
                    tel.add_quality_retry()
                except Exception as _exc:
                    logger.debug("telemetry add_quality_retry failed: %s", _exc)
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
        tel = extra.get("telemetry")
        if tel is not None:
            try:
                tel.add_consensus_run()
            except Exception as _exc:
                logger.debug("telemetry add_consensus_run failed: %s", _exc)
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
        # The coalesced output is the only result the workflow ever sees,
        # so it must clear the same quality bar as a direct dispatch. One
        # repair pass: re-score, and if the verdict is recoverable, ask the
        # chief to re-emit with the specific issues quoted back.
        if coalesced.response.ok:
            try:
                cfg_r = self.clk_cfg.get("robustness") or {}
                q = _response_quality.score(
                    coalesced.response.text,
                    min_chars=int(cfg_r.get("min_response_chars") or 40),
                    expected_outputs=list(extra.get("stage_outputs") or []),
                )
                if not q.ok and q.recoverable:
                    log_event(
                        self.paths,
                        "consensus_coalesce_retry",
                        agent=agent_name,
                        name=name,
                        flags=list(q.flags),
                        reasons=list(q.reasons),
                        score=q.score,
                        trigger=reason,
                    )
                    self._observer_log(
                        f"consensus :: {name} :: coalesce rejected "
                        f"(flags={','.join(q.flags)}); re-dispatching chief"
                    )
                    repaired = self._dispatch_once(
                        "chief",
                        q.repair_hint() + "\n\nOriginal coalescing task:\n" + coalesce_objective,
                        extra={
                            "phase": "consensus",
                            "consensus_name": name,
                            "consensus_trigger": f"{reason}:coalesce_repair",
                            "stage_id": extra.get("stage_id"),
                            "workflow": extra.get("workflow"),
                        },
                        dry_run=dry_run,
                    )
                    if repaired.response.ok:
                        coalesced = repaired
            except Exception as exc:
                log_exception("orchestration.agent._dispatch_auto_consensus.rescore", exc)
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
            except Exception as _exc:
                logger.debug("activity log_event failed: %s", _exc)
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
            self._apply_actions(run, extra or {})
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
        logger.info(line)
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
                except Exception as _exc:
                    logger.debug("observer progress failed: %s", _exc)

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
        arun = AgentRun(
            agent=label, objective=sample_objective, response=resp, started_at=started, finished_at=finished
        )
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
        return {
            "sample": sample,
            "agent": agent_name,
            "label": label,
            "ok": resp.ok,
            "error": resp.error,
            "text": resp.text or "",
        }

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
            parts.append(
                f"\n--- sample {r.get('sample')} agent={r.get('agent')} "
                f"ok={r.get('ok')} error={r.get('error') or ''} ---"
            )
            parts.append((r.get("text") or "").strip() or "(no response)")
        parts.append("\nReturn a unified answer with agreements, disagreements, and the recommended decision.")
        return "\n".join(parts)
