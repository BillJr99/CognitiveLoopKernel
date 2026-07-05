"""Review behaviors for the workflow runner.

Chief-review prompt synthesis, per-stage checkpoints, the critic-judge
refinement loop, and the adversarial debate panel.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import TYPE_CHECKING, Dict, List, Optional, Tuple

from ...log import get_logger, log_exception
from ...utils.activity_log import log_event
from .. import blackboard as _blackboard

if TYPE_CHECKING:
    from ...config import Paths
    from ..agent import AgentRun, AgentRunner
    from ..telemetry import CycleTelemetry
    from .stages import StageResult, Workflow, WorkflowStage

logger = get_logger(__name__)


class ReviewMixin:
    """Review / refinement / checkpoint methods mixed into ``WorkflowRunner``."""

    paths: "Paths"
    runner: "AgentRunner"
    telemetry: Optional["CycleTelemetry"]

    def _build_review_objective(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        result_by_id: Dict[str, "StageResult"],
    ) -> str:
        """Render the chief-review prompt for ``stage`` using upstream stages'
        actual posts so the chief reads the real artifacts, not the
        worker's self-report.
        """
        try:
            all_posts = _blackboard.list_posts(self.paths)
        except Exception:
            all_posts = []
        upstream_ids = list(stage.depends_on)
        sections: List[str] = [
            f"Review dispatch for workflow `{workflow.name}` stage `{stage.id}`.",
            "",
            "You are reviewing the output of these upstream stages:",
        ]
        for sid in upstream_ids:
            sr = result_by_id.get(sid)
            if sr is None:
                sections.append(f"- `{sid}`: (no result on record)")
                continue
            agent = sr.stage.agent
            ok = sr.run.response.ok
            v_ok = sr.validated
            reason = sr.failure_reason or ""
            sections.append(
                f"- `{sid}` (agent {agent}): ok={ok} validated={v_ok} "
                + (f"reason={reason}" if reason else "no failure")
            )
        sections.append("")
        sections.append("Blackboard posts produced by these stages:")
        any_posts = False
        for p in all_posts:
            if p.stage_id in upstream_ids:
                any_posts = True
                body = (p.body or "").strip()
                if len(body) > 1200:
                    body = body[:1200].rstrip() + " …"
                sections.append(
                    f"\n--- post id={p.id} author={p.author} type={p.post_type} "
                    f"stage={p.stage_id} produces={','.join(p.produces) or '-'} ---"
                )
                sections.append(body or "(empty body)")
        if not any_posts:
            sections.append("(no blackboard posts from upstream — workers may have skipped POST.)")
        sections.append("")
        sections.append(
            "Decide one of:\n"
            "  (a) ACTION:done with REASON — the user's prompt is fully addressed.\n"
            "  (b) PROPOSE_WORKFLOW with a refined next iteration (always include\n"
            "      a final supervise stage so the loop continues).\n"
            "  (c) PROPOSE_CONSENSUS to re-sample a specific decision when the\n"
            "      upstream results disagree or seem unreliable.\n"
            "Also emit a brief POST: review block summarizing what passed, what\n"
            "needs more work, and the chosen path."
        )
        if stage.objective:
            sections.append("")
            sections.append("Review-stage author's objective (from the workflow YAML):")
            sections.append(stage.objective)
        return "\n".join(sections)

    @property
    def _checkpoint_default_per_stage(self) -> bool:
        cfg = (self.runner.clk_cfg.get("review") or {})
        return bool(cfg.get("per_stage", False))

    def _checkpoint_enabled(self, stage: "WorkflowStage") -> bool:
        if stage.careful:
            return True
        return self._checkpoint_default_per_stage

    def _meta_dispatch_enabled(self, stage: "WorkflowStage") -> bool:
        cfg = (self.runner.clk_cfg.get("meta_prompt") or {})
        mode = str(cfg.get("dispatch") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "always":
            return True
        # default mode "careful_only"
        return bool(stage.careful)

    # -- critic-judge refinement (Layer 3 robustness loop) ---------------

    def _refine_enabled(self, stage: "WorkflowStage") -> bool:
        """Decide whether the critic-judge refinement loop should run.

        Explicit ``refine:`` on the stage always wins. Otherwise we
        fall back to ``robustness.auto_refine`` (off | careful_only |
        all). ``chief`` and ``qa`` agents are skipped to avoid the
        critic critiquing its own coalescing output or the validator.
        """
        if stage.agent in ("chief", "qa", "critic"):
            return False
        if stage.refine is not None:
            return True
        cfg = (self.runner.clk_cfg.get("robustness") or {})
        mode = str(cfg.get("auto_refine") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "all":
            return True
        # default mode "careful_only"
        return bool(stage.careful)

    def _refine_loop(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        first_run: "AgentRun",
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> "AgentRun":
        """Run draft → critic → revise until accept or max_rounds.

        Reuses the runner's existing dispatch path for both the critic
        and the revised worker. The critic is dispatched in a ``phase:
        refine_critic`` extra so the wrapper's auto-consensus and
        quality-retry layers don't recurse.

        Returns the final worker run — either the revised one or the
        original when the critic accepts immediately.
        """
        defaults = (self.runner.clk_cfg.get("robustness") or {})
        cfg = dict(stage.refine or {})
        critic_name = str(cfg.get("critic") or "critic")
        try:
            max_rounds = int(cfg.get("max_rounds") or defaults.get("refine_max_rounds") or 3)
        except (TypeError, ValueError):
            max_rounds = 3
        try:
            threshold = float(cfg.get("accept_threshold") or defaults.get("refine_accept_threshold") or 0.8)
        except (TypeError, ValueError):
            threshold = 0.8

        # If the named critic isn't in the roster, fall back to the
        # `critic` baseline; if even that is missing, skip silently.
        agents_cfg = (self.runner.agents_cfg.get("agents") or {})
        if critic_name not in agents_cfg:
            critic_name = "critic" if "critic" in agents_cfg else ""
        if not critic_name:
            return first_run

        current_run = first_run
        for round_idx in range(1, max_rounds + 1):
            if self.telemetry is not None:
                try:
                    self.telemetry.add_refine_round()
                except Exception as _exc:
                    logger.debug("telemetry add_refine_round failed: %s", _exc)
            verdict, judge_score, feedback = self._dispatch_critic(
                workflow, stage, current_run, critic_name, round_idx, max_rounds, dry_run,
            )
            log_event(
                self.paths,
                "refine_critic_verdict",
                agent=stage.agent,
                critic=critic_name,
                workflow=workflow.name,
                stage_id=stage.id,
                round=round_idx,
                max_rounds=max_rounds,
                verdict=verdict,
                score=judge_score,
                accept_threshold=threshold,
            )
            self.runner._observer_log(
                f"refine :: {stage.id} :: round {round_idx}/{max_rounds} "
                f"{critic_name}→ verdict={verdict} score={judge_score:.2f}"
            )
            if verdict == "accept" or judge_score >= threshold:
                return current_run
            if round_idx == max_rounds:
                # Out of budget — keep the latest worker output even
                # though the critic isn't satisfied.
                return current_run
            revise_objective = (
                f"Refinement round {round_idx + 1}/{max_rounds} of stage "
                f"`{stage.id}`. The critic (`{critic_name}`) scored your "
                f"previous response {judge_score:.2f}/1.0 and asked for "
                "revisions:\n\n"
                f"{feedback}\n\n"
                "Revise the response so the critic's points are addressed. "
                "Keep what already works; rewrite only what was flagged. "
                "Re-emit POST and ACTION blocks the same way you did the "
                "first time so the harness can record the updated work.\n\n"
                f"Original objective:\n{stage.objective}"
            )
            current_run = self.runner.run(
                stage.agent,
                revise_objective,
                extra={
                    "phase": "refine_worker",
                    "stage_id": stage.id,
                    "workflow": workflow.name,
                    "cycle_context": cycle_context,
                    "blackboard_inputs": list(stage.inputs),
                    "stage_outputs": list(stage.outputs),
                    "refine_round": round_idx + 1,
                    "refine_max_rounds": max_rounds,
                    "telemetry": self.telemetry,
                },
                dry_run=dry_run,
            )
            if not current_run.response.ok:
                return current_run
        return current_run

    _REFINE_VERDICT_RE = re.compile(
        r"^\s*VERDICT\s*:\s*(accept|revise|reject)\b", re.IGNORECASE | re.MULTILINE,
    )
    _REFINE_SCORE_RE = re.compile(
        r"^\s*SCORE\s*:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE | re.MULTILINE,
    )

    def _dispatch_critic(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        worker_run: "AgentRun",
        critic_name: str,
        round_idx: int,
        max_rounds: int,
        dry_run: Optional[bool],
    ) -> Tuple[str, float, str]:
        """Run one critic pass; return ``(verdict, score, feedback)``.

        ``verdict`` is normalised to ``"accept"`` or ``"revise"``.
        ``score`` is parsed from the critic's ``SCORE: <0..1>`` line and
        defaults to 0.0 (i.e. "revise") when missing.
        ``feedback`` is the critic's full response text, used verbatim
        in the revision objective.
        """
        worker_text = (worker_run.response.text or "").strip()
        if len(worker_text) > 4000:
            worker_text = worker_text[:4000].rstrip() + "\n…(truncated)"
        outputs_text = (
            ", ".join(stage.outputs) if stage.outputs else "(no declared outputs)"
        )
        critic_objective = (
            f"Refinement-loop critic pass for workflow `{workflow.name}` "
            f"stage `{stage.id}` (round {round_idx}/{max_rounds}).\n\n"
            f"Worker: `{stage.agent}`\n"
            f"Worker's objective:\n{stage.objective}\n\n"
            f"Declared output contract keys: {outputs_text}\n\n"
            f"Worker's response:\n---\n{worker_text}\n---\n\n"
            "Score the response 0..1 against the objective and the "
            "declared output contract. List concrete, specific "
            "revisions the worker should make. Be brief — three to six "
            "bullets is plenty. End your response with exactly two "
            "lines:\n"
            "VERDICT: accept   # or `revise` if any item must change\n"
            "SCORE: <0..1>\n"
        )
        critic_run = self.runner.run(
            critic_name,
            critic_objective,
            extra={
                "phase": "refine_critic",
                "stage_id": stage.id,
                "workflow": workflow.name,
                "refine_round": round_idx,
            },
            dry_run=dry_run,
        )
        text = critic_run.response.text or ""
        verdict_m = self._REFINE_VERDICT_RE.search(text)
        verdict = (verdict_m.group(1).lower() if verdict_m else "revise")
        if verdict not in ("accept", "revise"):
            verdict = "revise"
        score_m = self._REFINE_SCORE_RE.search(text)
        try:
            score_val = float(score_m.group(1)) if score_m else 0.0
        except (TypeError, ValueError):
            score_val = 0.0
        score_val = max(0.0, min(1.0, score_val))
        # When the critic accepted but didn't post a score, treat it as
        # a confident pass; when it asked to revise but didn't score,
        # treat as a moderate-low score so the loop continues.
        if score_m is None:
            score_val = 1.0 if verdict == "accept" else 0.4
        return verdict, score_val, text.strip()

    # -- adversarial debate panel (multi-critic refinement) --------------

    _DEBATE_LENS_GUIDANCE: Dict[str, str] = {
        "correctness": "logic errors, wrong outputs, unhandled edge cases, broken contracts or APIs.",
        "security": "injection, unsafe input handling, secret/credential leakage, unsafe shell/file operations.",
        "simplicity": "needless complexity, duplication, dead code, and simpler equivalent designs.",
        "performance": "obvious inefficiency, redundant work, N+1 patterns, unbounded loops or memory.",
        "robustness": "failure modes, missing error handling, flaky assumptions, and race conditions.",
        "tests": "missing or weak tests, untested branches, and assertions that don't actually verify behavior.",
        "ux": "confusing interfaces, poor error messages, and undocumented behavior.",
    }

    def _debate_enabled(self, stage: "WorkflowStage") -> bool:
        """Whether the adversarial debate panel should run for this stage.

        Explicit ``refine: {mode: debate}`` always wins; otherwise the
        ``robustness.debate`` policy (off | careful_only | all) decides.
        chief / qa / critic stages are skipped.
        """
        if stage.agent in ("chief", "qa", "critic"):
            return False
        if isinstance(stage.refine, dict) and str(stage.refine.get("mode") or "").lower() == "debate":
            return True
        cfg = (self.runner.clk_cfg.get("robustness") or {})
        mode = str(cfg.get("debate") or "off").lower()
        if mode in ("", "off", "false", "0"):
            return False
        if mode == "all":
            return True
        return bool(stage.careful)  # careful_only

    def _debate_lenses(self, stage: "WorkflowStage") -> List[str]:
        if isinstance(stage.refine, dict) and stage.refine.get("critics"):
            lenses = [str(x).strip().lower() for x in stage.refine["critics"] if str(x).strip()]
        else:
            cfg = (self.runner.clk_cfg.get("robustness") or {})
            lenses = [str(x).strip().lower() for x in (cfg.get("debate_lenses") or []) if str(x).strip()]
        return lenses or ["correctness", "security", "simplicity"]

    def _dispatch_lens_critic(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        worker_run: "AgentRun",
        critic_name: str,
        lens: str,
        round_idx: int,
        max_rounds: int,
        peer_transcript: str,
        dry_run: Optional[bool],
    ) -> Tuple[str, str, float, str]:
        """One adversarial critic pass for a single lens.

        Returns ``(lens, verdict, score, feedback)``. The critic is told to
        try to *break* the work from its lens and, in later rounds, to engage
        with peers' critiques (reinforce / refute / concede).
        """
        worker_text = (worker_run.response.text or "").strip()
        if len(worker_text) > 3500:
            worker_text = worker_text[:3500].rstrip() + "\n…(truncated)"
        guidance = self._DEBATE_LENS_GUIDANCE.get(
            lens, f"weaknesses from the {lens} perspective."
        )
        peer_block = ""
        if peer_transcript.strip():
            peer_block = (
                "\nYour fellow panelists said (engage with them — reinforce, "
                "refute, or concede explicitly):\n"
                f"{peer_transcript}\n"
            )
        objective = (
            f"ADVERSARIAL DEBATE — you are the **{lens}** critic on a review "
            f"panel for stage `{stage.id}` (round {round_idx}/{max_rounds}).\n\n"
            f"Your lens: hunt for {guidance}\n"
            "Try hard to BREAK this work from your lens. Be specific and "
            "concrete; cite the exact place. Default to skepticism — only "
            "accept if you genuinely cannot find a real problem.\n\n"
            f"Worker `{stage.agent}` objective:\n{stage.objective}\n\n"
            f"Worker's response:\n---\n{worker_text}\n---\n"
            f"{peer_block}\n"
            "Keep it to 2-5 concrete bullets. End with exactly two lines:\n"
            "VERDICT: accept   # or `revise` if any real issue remains\n"
            "SCORE: <0..1>\n"
        )
        critic_run = self.runner.run(
            critic_name,
            objective,
            extra={
                "phase": "refine_critic",
                "stage_id": stage.id,
                "workflow": workflow.name,
                "refine_round": round_idx,
                "debate_lens": lens,
            },
            dry_run=dry_run,
        )
        text = critic_run.response.text or ""
        verdict_m = self._REFINE_VERDICT_RE.search(text)
        verdict = (verdict_m.group(1).lower() if verdict_m else "revise")
        if verdict not in ("accept", "revise"):
            verdict = "revise"
        score_m = self._REFINE_SCORE_RE.search(text)
        try:
            score_val = float(score_m.group(1)) if score_m else (1.0 if verdict == "accept" else 0.4)
        except (TypeError, ValueError):
            score_val = 0.4
        score_val = max(0.0, min(1.0, score_val))
        return lens, verdict, score_val, text.strip()

    def _debate_loop(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        first_run: "AgentRun",
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> "AgentRun":
        """Run an adversarial debate panel: N lens-critics → worker revision.

        Each round fans out one critic per lens in parallel; the worker is
        kept only if a majority of lenses accept (or the mean score clears the
        threshold). Otherwise the combined critiques drive a revision, and the
        next round's critics see the prior panel transcript so they can debate
        each other. Bounded by ``debate_max_rounds``.
        """
        defaults = (self.runner.clk_cfg.get("robustness") or {})
        cfg = dict(stage.refine or {}) if isinstance(stage.refine, dict) else {}
        try:
            max_rounds = int(cfg.get("max_rounds") or defaults.get("debate_max_rounds") or 2)
        except (TypeError, ValueError):
            max_rounds = 2
        try:
            threshold = float(cfg.get("accept_threshold") or defaults.get("refine_accept_threshold") or 0.8)
        except (TypeError, ValueError):
            threshold = 0.8

        agents_cfg = (self.runner.agents_cfg.get("agents") or {})
        critic_name = "critic" if "critic" in agents_cfg else ""
        if not critic_name:
            # No critic in the roster — fall back to the single-critic loop
            # (which itself no-ops when no critic exists).
            return self._refine_loop(workflow, stage, first_run, cycle_context, dry_run)

        lenses = self._debate_lenses(stage)
        max_parallel = max(1, int((self.runner.clk_cfg.get("consensus") or {}).get("max_parallel") or 4))
        current_run = first_run
        peer_transcript = ""

        for round_idx in range(1, max_rounds + 1):
            if self.telemetry is not None:
                try:
                    self.telemetry.add_refine_round()
                except Exception as _exc:
                    logger.debug("telemetry add_refine_round failed: %s", _exc)
            verdicts: List[Tuple[str, str, float, str]] = []
            with ThreadPoolExecutor(max_workers=min(max_parallel, len(lenses))) as pool:
                futs = {
                    pool.submit(
                        self._dispatch_lens_critic, workflow, stage, current_run,
                        critic_name, lens, round_idx, max_rounds, peer_transcript, dry_run,
                    ): lens
                    for lens in lenses
                }
                for fut in as_completed(futs):
                    try:
                        verdicts.append(fut.result())
                    except Exception as exc:
                        log_exception("orchestration.workflow._debate_loop.critic", exc)
            if not verdicts:
                return current_run
            revise_votes = sum(1 for (_l, v, _s, _f) in verdicts if v == "revise")
            scores = [s for (_l, _v, s, _f) in verdicts]
            avg_score = sum(scores) / len(scores) if scores else 0.0
            transcript = "\n".join(
                f"[{lens}] verdict={v} score={s:.2f}\n{fb}" for (lens, v, s, fb) in verdicts
            )
            peer_transcript = transcript
            log_event(
                self.paths, "debate_round",
                agent=stage.agent, workflow=workflow.name, stage_id=stage.id,
                round=round_idx, max_rounds=max_rounds,
                lenses=[lens for (lens, *_r) in verdicts],
                revise_votes=revise_votes, avg_score=round(avg_score, 3),
                accept_threshold=threshold,
            )
            self.runner._observer_log(
                f"debate :: {stage.id} :: round {round_idx}/{max_rounds} "
                f"{len(lenses)} critics, {revise_votes} revise, avg={avg_score:.2f}"
            )
            try:
                _blackboard.post(
                    self.paths, author="critic-panel", body=transcript[:4000],
                    post_type="debate", stage_id=stage.id, workflow=workflow.name,
                    slug_hint=f"debate-{stage.id}-r{round_idx}",
                )
            except Exception as exc:
                log_exception("orchestration.workflow._debate_loop.post", exc)

            # Panel accepts when a majority accept AND the mean clears the bar.
            if revise_votes * 2 <= len(verdicts) and avg_score >= threshold:
                return current_run
            if round_idx == max_rounds:
                return current_run

            revise_objective = (
                f"Debate round {round_idx + 1}/{max_rounds} of stage `{stage.id}`. "
                f"An adversarial review panel ({', '.join(lenses)}) found issues "
                f"(mean score {avg_score:.2f}/1.0). Address every concrete point "
                "below; keep what already works. Re-emit POST and ACTION blocks "
                "the same way so the harness records the updated work.\n\n"
                f"Panel critiques:\n{transcript}\n\n"
                f"Original objective:\n{stage.objective}"
            )
            current_run = self.runner.run(
                stage.agent,
                revise_objective,
                extra={
                    "phase": "refine_worker",
                    "stage_id": stage.id,
                    "workflow": workflow.name,
                    "cycle_context": cycle_context,
                    "blackboard_inputs": list(stage.inputs),
                    "stage_outputs": list(stage.outputs),
                    "refine_round": round_idx + 1,
                    "refine_max_rounds": max_rounds,
                    "telemetry": self.telemetry,
                },
                dry_run=dry_run,
            )
            if not current_run.response.ok:
                return current_run
        return current_run

    def _dispatch_checkpoint(
        self,
        workflow: "Workflow",
        stage: "WorkflowStage",
        result: "StageResult",
        cycle_context: str,
        dry_run: Optional[bool],
    ) -> None:
        """Light-weight chief checkpoint after a sensitive stage.

        Cost-bounded: a small prompt with the stage's posts and a
        request for a CONTINUE / REDIRECT / ABORT verdict. The chief
        emits ACTION:done if the project is finished, or PROPOSE_WORKFLOW
        if the plan should change. Otherwise we just log the verdict and
        let the workflow proceed.
        """
        try:
            posts = _blackboard.list_posts(self.paths)
        except Exception:
            posts = []
        produced = [p for p in posts if p.stage_id == stage.id]
        snapshot = "\n".join(
            f"- {p.id} type={p.post_type} produces={','.join(p.produces) or '-'} "
            f"body_chars={len(p.body or '')}"
            for p in produced[-10:]
        ) or "(no posts from this stage)"
        objective = (
            f"Chief checkpoint after stage `{stage.id}` (agent {stage.agent}, "
            f"workflow `{workflow.name}`).\n\n"
            f"Stage objective:\n{stage.objective}\n\n"
            f"Posts produced by this stage:\n{snapshot}\n\n"
            "Reply with one of:\n"
            "  CHECKPOINT: continue — let the workflow proceed as planned.\n"
            "  CHECKPOINT: redirect — emit PROPOSE_WORKFLOW with a revised plan.\n"
            "  CHECKPOINT: abort — emit ACTION:done if the project is finished.\n"
            "Keep the response short — this is a verification, not a redo."
        )
        logger.info(f"workflow {workflow.name}: checkpoint after stage {stage.id}")
        self.runner.run(
            "chief",
            objective,
            extra={
                "phase": "checkpoint",
                "workflow": workflow.name,
                "stage_id": stage.id,
                "cycle_context": cycle_context,
                "blackboard_inputs": [f"stage:{stage.id}"],
            },
            dry_run=dry_run,
        )
