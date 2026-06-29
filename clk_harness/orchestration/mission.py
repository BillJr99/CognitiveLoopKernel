"""Autonomous mission orchestrator.

One objective in, a genuinely-complete result out — no human follow-up. The
MissionRunner is the macro plan->execute->evaluate->refine->iterate loop that
wraps the existing per-workflow engines:

1. CHARTER  — the chief writes the up-front commitment (see ``charter.py``).
2. PLAN     — the chief authors an ordered phase plan (PROPOSE_PLAN), persisted
              as a living artifact (``.clk/state/mission.json``).
3. For each phase: run its engine (WorkflowRunner / RalphLoop / Autoresearch),
   evaluate a chief PHASE_GATE, and iterate until the gate passes — re-planning
   when the gate says the plan itself is wrong.
4. The mission only stops on a machine-checkable done-gate (``done_gate.py``);
   any agent's ACTION:done is a *request*, not a command.

Every boundary is trace-committed so the git log is the execution trail, and a
per-cycle telemetry line makes the loop observable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Paths, save_json, load_json
from ..git_ops import commit_trace
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception
from . import blackboard as _blackboard
from . import charter as _charter
from . import casting as _casting
from . import deliberation as _deliberation
from . import done_gate as _done_gate
from .agent import AgentRunner
from .evaluator import Evaluator
from .telemetry import CycleTelemetry
from .workflow import WorkflowRunner, load_workflow
from .ralph_loop import RalphLoop
from .autoresearch_loop import AutoresearchLoop


_GATE_RE = re.compile(r"^\s*GATE\s*:\s*(pass|repeat|revise|done)\b", re.IGNORECASE | re.MULTILINE)
_DEFAULT_PHASES = ["discovery", "product", "engineering", "validation", "deployment"]


@dataclass
class PhaseSpec:
    id: str
    title: str = ""
    workflow: str = ""
    engine: str = "workflow"  # workflow | ralph | autoresearch
    exit_criteria: List[str] = field(default_factory=list)
    produced_keys: List[str] = field(default_factory=list)
    status: str = "pending"  # pending | running | done | skipped | failed
    order: int = 0
    iterations_used: int = 0
    max_iterations: int = 0
    gate_history: List[Dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "workflow": self.workflow or self.id,
            "engine": self.engine,
            "exit_criteria": list(self.exit_criteria),
            "produced_keys": list(self.produced_keys),
            "status": self.status,
            "order": self.order,
            "iterations_used": self.iterations_used,
            "max_iterations": self.max_iterations,
            "gate_history": list(self.gate_history),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], order: int = 0) -> "PhaseSpec":
        raw = raw or {}
        return cls(
            id=str(raw.get("id") or f"phase{order}"),
            title=str(raw.get("title") or ""),
            workflow=str(raw.get("workflow") or raw.get("id") or ""),
            engine=str(raw.get("engine") or "workflow").lower(),
            exit_criteria=[str(x) for x in (raw.get("exit_criteria") or [])],
            produced_keys=[str(x) for x in (raw.get("produced_keys") or [])],
            status=str(raw.get("status") or "pending"),
            order=int(raw.get("order") or order),
            iterations_used=int(raw.get("iterations_used") or 0),
            max_iterations=int(raw.get("max_iterations") or 0),
            gate_history=list(raw.get("gate_history") or []),
        )


@dataclass
class MissionPlan:
    objective: str = ""
    title: str = ""
    created_at: str = ""
    updated_at: str = ""
    status: str = "planning"  # planning | running | done | stalled | aborted
    current_phase_id: str = ""
    total_cycles_used: int = 0
    phases: List[PhaseSpec] = field(default_factory=list)
    cycles: List[Dict[str, Any]] = field(default_factory=list)
    done_gate_last: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "objective": self.objective,
            "title": self.title,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "status": self.status,
            "current_phase_id": self.current_phase_id,
            "total_cycles_used": self.total_cycles_used,
            "phases": [p.to_dict() for p in self.phases],
            "cycles": list(self.cycles),
            "done_gate_last": dict(self.done_gate_last),
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "MissionPlan":
        raw = raw or {}
        phases = [PhaseSpec.from_dict(p, i) for i, p in enumerate(raw.get("phases") or [])]
        return cls(
            objective=str(raw.get("objective") or ""),
            title=str(raw.get("title") or ""),
            created_at=str(raw.get("created_at") or ""),
            updated_at=str(raw.get("updated_at") or ""),
            status=str(raw.get("status") or "planning"),
            current_phase_id=str(raw.get("current_phase_id") or ""),
            total_cycles_used=int(raw.get("total_cycles_used") or 0),
            phases=phases,
            cycles=list(raw.get("cycles") or []),
            done_gate_last=dict(raw.get("done_gate_last") or {}),
        )

    def next_pending(self) -> Optional[PhaseSpec]:
        for p in sorted(self.phases, key=lambda x: x.order):
            if p.status in ("pending", "running"):
                return p
        return None

    def all_done(self) -> bool:
        return bool(self.phases) and all(
            p.status in ("done", "skipped") for p in self.phases
        )

    def render_md(self) -> str:
        lines = [f"# Mission: {self.title or self.objective}", "",
                 f"**Status:** {self.status}  ", f"**Cycles used:** {self.total_cycles_used}", "",
                 "## Phases", ""]
        for p in sorted(self.phases, key=lambda x: x.order):
            mark = {"done": "x", "running": ">", "failed": "!", "skipped": "-"}.get(p.status, " ")
            lines.append(f"- [{mark}] **{p.id}** ({p.engine}) — {p.title or ''} "
                         f"[{p.status}, {p.iterations_used} it]")
            for c in p.exit_criteria:
                lines.append(f"    - exit: {c}")
        return "\n".join(lines) + "\n"


def plan_path(paths: Paths) -> Path:
    return paths.state / "mission.json"


def mission_md_path(paths: Paths) -> Path:
    return paths.state / "MISSION.md"


def load_plan(paths: Paths) -> Optional[MissionPlan]:
    p = plan_path(paths)
    if not p.exists():
        return None
    raw = load_json(p, {})
    if not raw:
        return None
    return MissionPlan.from_dict(raw)


def save_plan(paths: Paths, plan: MissionPlan) -> None:
    plan.updated_at = datetime.now().isoformat(timespec="seconds")
    paths.state.mkdir(parents=True, exist_ok=True)
    save_json(plan_path(paths), plan.to_dict())
    try:
        mission_md_path(paths).write_text(plan.render_md(), encoding="utf-8")
    except Exception as exc:
        log_exception("orchestration.mission.save_plan.md", exc)


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")[:40] or "mission"


class MissionRunner:
    """Drives one objective to a code-gated done with no human follow-up."""

    def __init__(self, paths: Paths, runner: AgentRunner, evaluator: Evaluator) -> None:
        self.paths = paths
        self.runner = runner
        self.evaluator = evaluator

    # -- config accessors --------------------------------------------------

    def _cfg(self) -> Dict[str, Any]:
        return (self.runner.clk_cfg.get("mission") or {})

    @property
    def max_phases(self) -> int:
        return int(self._cfg().get("max_phases", 12) or 12)

    @property
    def max_iterations_per_phase(self) -> int:
        return int(self._cfg().get("max_iterations_per_phase", 3) or 3)

    @property
    def max_total_cycles(self) -> int:
        return int(self._cfg().get("max_total_cycles", 60) or 60)

    @property
    def phase_gate_enabled(self) -> bool:
        return bool(self._cfg().get("phase_gate", True))

    @property
    def charter_first(self) -> bool:
        return bool(self._cfg().get("charter_first", True))

    @property
    def telemetry_stdout(self) -> bool:
        return bool(self._cfg().get("telemetry_stdout", True))

    @property
    def on_budget_exhausted(self) -> str:
        return str(self._cfg().get("on_budget_exhausted", "advance")).lower()

    # -- public ------------------------------------------------------------

    def run(self, objective: Optional[str] = None, *, resume: bool = False,
            dry_run: bool = False) -> MissionPlan:
        objective = objective or self._idea_objective()
        if not objective:
            log("mission: no objective and no captured idea; nothing to do", level="WARN")
            return MissionPlan(status="aborted")

        charter = None
        plan: Optional[MissionPlan] = load_plan(self.paths) if resume else None
        if plan is None:
            if self.charter_first:
                charter = _charter.bootstrap_charter(self.paths, self.runner, objective, dry_run=dry_run)
            plan = self._bootstrap_plan(objective, charter, dry_run=dry_run)
        else:
            charter = _charter.load_charter(self.paths)
            log(f"mission: resuming plan with {len(plan.phases)} phases "
                f"({sum(1 for p in plan.phases if p.status == 'done')} done)")

        plan.status = "running"
        save_plan(self.paths, plan)

        total = plan.total_cycles_used
        for _outer in range(self.max_phases):
            if self._mission_complete(plan):
                break
            phase = plan.next_pending()
            if phase is None:
                break
            phase.status = "running"
            plan.current_phase_id = phase.id
            save_plan(self.paths, plan)
            commit_trace(self.paths.root, kind="phase-start", summary=f"phase {phase.id}",
                         meta={"engine": phase.engine})

            advanced = False
            budget = phase.max_iterations or self.max_iterations_per_phase
            for _it in range(budget):
                if total >= self.max_total_cycles:
                    log(f"mission: total-cycle budget exhausted ({self.max_total_cycles})", level="WARN")
                    plan.status = "stalled"
                    break
                total += 1
                plan.total_cycles_used = total
                tel = CycleTelemetry(n=total, max_cycles=self.max_total_cycles,
                                     phase=phase.id, workflow=phase.workflow or phase.id)
                results = self._run_phase(phase, tel, dry_run)
                phase.iterations_used += 1

                eval_result, weak = self._evaluate(dry_run)
                tel.record_eval(eval_result, weak=weak)

                verdict = self._phase_gate(plan, phase, results, eval_result, dry_run)
                phase.gate_history.append({
                    "at": datetime.now().isoformat(timespec="seconds"),
                    "verdict": verdict,
                })
                self._post_phase_summary(plan, phase, verdict)
                tel.progress = bool(results)
                tel.emit(self.paths, to_stdout=self.telemetry_stdout)
                plan.cycles.append(tel.as_dict())
                save_plan(self.paths, plan)
                commit_trace(self.paths.root, kind=f"phase-gate:{verdict}",
                             summary=f"{phase.id} cycle {total}", meta={"phase": phase.id})

                if verdict == "pass":
                    phase.status = "done"
                    advanced = True
                    break
                if verdict == "done":
                    gate = self._maybe_finish(plan, charter, eval_result)
                    tel.record_done_gate(gate)
                    if gate.passed:
                        phase.status = "done"
                        plan.status = "done"
                        save_plan(self.paths, plan)
                        return plan
                    # rejected: keep iterating this phase
                    continue
                if verdict == "revise":
                    self._refine_plan(plan, phase, results, dry_run)
                    advanced = True
                    break
                # "repeat": loop again on the same phase
            else:
                # exhausted the per-phase budget without a pass
                if self.on_budget_exhausted == "fail":
                    phase.status = "failed"
                else:
                    phase.status = "done"
                advanced = True

            if not advanced and plan.status == "stalled":
                break
            save_plan(self.paths, plan)

        # Final attempt: if every phase is done, run the done-gate once.
        if plan.status not in ("done", "stalled"):
            if plan.all_done():
                gate = self._maybe_finish(plan, charter, self._evaluate(dry_run)[0])
                plan.status = "done" if gate.passed else "stalled"
            else:
                plan.status = "stalled"
        save_plan(self.paths, plan)
        log(f"mission: finished with status={plan.status} after {plan.total_cycles_used} cycles")
        return plan

    # -- internals ---------------------------------------------------------

    def _idea_objective(self) -> str:
        idea_path = self.paths.state / "idea.json"
        if not idea_path.exists():
            return ""
        try:
            raw = json.loads(idea_path.read_text(encoding="utf-8"))
            return (raw.get("statement") or raw.get("title") or "").strip()
        except Exception:
            return ""

    def _mission_complete(self, plan: MissionPlan) -> bool:
        return (self.paths.state / "done_granted.md").exists() and plan.all_done()

    def _bootstrap_plan(self, objective: str, charter, *, dry_run: bool) -> MissionPlan:
        now = datetime.now().isoformat(timespec="seconds")
        plan = MissionPlan(
            objective=objective,
            title=(charter.mission_statement if charter else objective)[:80],
            created_at=now,
            updated_at=now,
            status="planning",
        )
        phases: List[PhaseSpec] = []
        if not dry_run:
            try:
                run = self.runner.run(
                    "chief",
                    self._plan_objective(objective, charter),
                    extra={"phase": "mission_plan"},
                    dry_run=dry_run,
                )
                prop = _casting.parse_plan_proposal(run.response.text or "")
                if prop and prop.phases:
                    phases = [PhaseSpec.from_dict(p, i) for i, p in enumerate(prop.phases)]
            except Exception as exc:
                log_exception("orchestration.mission._bootstrap_plan", exc)
        if not phases:
            default = self._cfg().get("default_phases") or _DEFAULT_PHASES
            phases = [
                PhaseSpec(id=name, workflow=name, engine="workflow", order=i)
                for i, name in enumerate(default)
            ]
        for i, p in enumerate(phases):
            p.order = i
            if not p.workflow:
                p.workflow = p.id
        plan.phases = phases
        save_plan(self.paths, plan)
        log_event(self.paths, "mission_plan", objective=objective,
                  phases=[p.id for p in phases])
        try:
            _blackboard.post(self.paths, author="mission", body=plan.render_md(),
                             post_type="plan", produces=["mission_plan"], slug_hint="plan")
        except Exception as exc:
            log_exception("orchestration.mission._bootstrap_plan.post", exc)
        commit_trace(self.paths.root, kind="plan", summary=f"{len(phases)} phases",
                     meta={"phases": [p.id for p in phases]})
        return plan

    def _run_phase(self, phase: PhaseSpec, tel: CycleTelemetry, dry_run: bool) -> List[Any]:
        engine = (phase.engine or "workflow").lower()
        if engine == "ralph":
            loop = RalphLoop(self.paths, self.runner, self.evaluator,
                             max_iterations=phase.max_iterations or 1)
            try:
                return list(loop.run(dry_run=dry_run))
            except Exception as exc:
                log_exception("orchestration.mission._run_phase.ralph", exc)
                return []
        if engine == "autoresearch":
            loop = AutoresearchLoop(self.paths, self.runner, self.evaluator,
                                    max_iterations=phase.max_iterations or 1)
            try:
                return list(loop.run(dry_run=dry_run))
            except Exception as exc:
                log_exception("orchestration.mission._run_phase.autoresearch", exc)
                return []
        # default: workflow engine
        wf_name = phase.workflow or phase.id
        wf_path = self.paths.workflows / f"{wf_name}.yaml"
        if not wf_path.exists():
            wf_path = self.paths.workflows / "engineering.yaml"
        try:
            wf = load_workflow(wf_path)
        except Exception as exc:
            log_exception("orchestration.mission._run_phase.load", exc)
            return []
        wf_runner = WorkflowRunner(self.paths, self.runner)
        wf_runner.mission_mode = True
        wf_runner.supervise_cycles_override = 1  # mission owns the outer loop
        wf_runner.telemetry = tel
        try:
            return list(wf_runner.run(wf, dry_run=dry_run))
        except Exception as exc:
            log_exception("orchestration.mission._run_phase.run", exc)
            return []

    def _evaluate(self, dry_run: bool):
        if dry_run:
            from .evaluator import EvalResult
            return EvalResult(ok=True, checks=[]), False
        try:
            result = self.evaluator.run()
            return result, bool(getattr(result, "weak", False))
        except Exception as exc:
            log_exception("orchestration.mission._evaluate", exc)
            from .evaluator import EvalResult
            return EvalResult(ok=False, checks=[], weak=True), True

    def _phase_gate(self, plan: MissionPlan, phase: PhaseSpec, results: List[Any],
                    eval_result, dry_run: bool) -> str:
        if dry_run or not self.phase_gate_enabled:
            return "pass"
        # Deliberation: a phase cannot pass while a blocking question is open.
        if _deliberation.require_open_questions_resolved(self.runner.clk_cfg):
            open_q = _deliberation.unresolved_blocking_questions(self.paths)
            if open_q:
                log(f"mission: phase {phase.id} has {len(open_q)} unresolved blocking "
                    "question(s); repeating", level="INFO")
                return "repeat"
        try:
            run = self.runner.run(
                "chief",
                self._gate_objective(plan, phase, eval_result),
                extra={"phase": "phase_gate"},
                dry_run=dry_run,
            )
            m = _GATE_RE.search(run.response.text or "")
            if m:
                return m.group(1).lower()
        except Exception as exc:
            log_exception("orchestration.mission._phase_gate", exc)
        # Conservative default: keep working on this phase.
        return "repeat"

    def _maybe_finish(self, plan: MissionPlan, charter, eval_result) -> "_done_gate.DoneGateVerdict":
        extra_criteria = _charter.derive_done_criteria(charter) if charter else []
        verdict = _done_gate.evaluate_done_gate(
            self.paths, self.runner.clk_cfg, eval_result, extra_criteria=extra_criteria,
        )
        plan.done_gate_last = {"passed": verdict.passed, "failures": list(verdict.failures)}
        if verdict.passed:
            try:
                (self.paths.state / "done_granted.md").write_text(
                    "# Mission complete\n\n" + verdict.summary() + "\n", encoding="utf-8")
            except Exception as exc:
                log_exception("orchestration.mission._maybe_finish.write", exc)
            log_event(self.paths, "done_gate_granted", checked=verdict.checked, scope="mission")
            commit_trace(self.paths.root, kind="done", summary="mission done-gate granted",
                         meta={"checked": list(verdict.checked.keys())})
        else:
            log(f"mission: done requested but gate REJECTED — unmet: "
                f"{', '.join(verdict.failures) or '?'}", level="WARN")
            log_event(self.paths, "done_gate_rejected", failures=list(verdict.failures),
                      checked=verdict.checked, scope="mission")
        return verdict

    def _refine_plan(self, plan: MissionPlan, phase: PhaseSpec, results: List[Any],
                     dry_run: bool) -> None:
        """Re-plan the not-yet-done phases when the gate says the plan is wrong."""
        if dry_run:
            return
        try:
            run = self.runner.run(
                "chief",
                self._replan_objective(plan, phase),
                extra={"phase": "mission_plan"},
                dry_run=dry_run,
            )
            prop = _casting.parse_plan_proposal(run.response.text or "")
        except Exception as exc:
            log_exception("orchestration.mission._refine_plan", exc)
            prop = None
        if not prop or not prop.phases:
            return
        done = [p for p in plan.phases if p.status in ("done", "skipped")]
        done_ids = {p.id for p in done}
        new_phases = list(done)
        order = len(done)
        for raw in prop.phases:
            ps = PhaseSpec.from_dict(raw, order)
            if ps.id in done_ids:
                continue
            ps.order = order
            if not ps.workflow:
                ps.workflow = ps.id
            new_phases.append(ps)
            order += 1
        plan.phases = new_phases
        save_plan(self.paths, plan)
        log_event(self.paths, "mission_replan", phases=[p.id for p in new_phases])
        commit_trace(self.paths.root, kind="replan", summary=f"{len(new_phases)} phases",
                     meta={"phases": [p.id for p in new_phases]})

    def _post_phase_summary(self, plan: MissionPlan, phase: PhaseSpec, verdict: str) -> None:
        try:
            _blackboard.post(
                self.paths,
                author="mission",
                body=f"Phase `{phase.id}` gate verdict: {verdict}. "
                     f"Exit criteria: {'; '.join(phase.exit_criteria) or '(none)'}.",
                post_type="phase",
                produces=[f"phase:{phase.id}"],
                stage_id=phase.id,
                slug_hint=f"phase-{phase.id}",
            )
        except Exception as exc:
            log_exception("orchestration.mission._post_phase_summary", exc)

    # -- prompts -----------------------------------------------------------

    def _plan_objective(self, objective: str, charter) -> str:
        charter_block = ""
        if charter is not None:
            charter_block = (
                "\nCharter (derive the plan from this):\n"
                f"- Mission: {charter.mission_statement}\n"
                f"- Scope: {'; '.join(charter.scope) or '(none)'}\n"
                f"- Success: {'; '.join(charter.success_criteria) or '(none)'}\n"
            )
        return (
            "MISSION PLAN MODE. Author the ordered lifecycle plan for this "
            "autonomous mission. There will be no human input — plan the whole "
            "path to a complete, production-ready result.\n\n"
            f"Mission objective:\n{objective}\n"
            f"{charter_block}\n"
            "Emit exactly one PROPOSE_PLAN block (machine-parsed):\n\n"
            "  PROPOSE_PLAN\n"
            "  PHASES:\n"
            "  - id: discovery\n"
            "    title: <short>\n"
            "    workflow: discovery\n"
            "    engine: workflow\n"
            "    exit_criteria: [<criterion>, <criterion>]\n"
            "  - id: engineering\n"
            "    title: <short>\n"
            "    workflow: engineering\n"
            "    engine: workflow\n"
            "    exit_criteria: [<criterion>]\n"
            "  END_PLAN\n\n"
            "Default to the lifecycle discovery -> product -> engineering -> "
            "validation -> deployment, but PRUNE phases this idea does not need "
            "(e.g. skip deployment for a pure-research idea). The engineering "
            "phase MUST use a workflow that contains a ralph refine stage and a "
            "qa stage. You may also emit PROPOSE_ROLE / PROPOSE_WORKFLOW blocks "
            "in this same response to define any roles or custom workflows the "
            "phases reference."
        )

    def _replan_objective(self, plan: MissionPlan, phase: PhaseSpec) -> str:
        return (
            "MISSION PLAN MODE (revision). The phase gate verdict indicated the "
            "plan itself needs to change. Re-author the remaining phases.\n\n"
            f"Mission objective:\n{plan.objective}\n\n"
            f"Phases already completed: "
            f"{', '.join(p.id for p in plan.phases if p.status == 'done') or '(none)'}\n"
            f"Current phase that triggered the revision: {phase.id}\n\n"
            "Emit a PROPOSE_PLAN block with the NEW set of remaining phases "
            "(do not repeat completed phases). Same grammar as before."
        )

    def _gate_objective(self, plan: MissionPlan, phase: PhaseSpec, eval_result) -> str:
        try:
            eval_summary = eval_result.summary() if eval_result else "(no evaluation)"
        except Exception:
            eval_summary = "(no evaluation)"
        criteria = "\n".join(f"  - {c}" for c in phase.exit_criteria) or "  (none declared)"
        digest = _blackboard.digest(self.paths, selectors=[f"stage:{phase.id}"], max_posts=8)
        return (
            "PHASE GATE MODE. Decide whether the current phase is complete enough "
            "to advance the mission. Be honest: a low bar to keep working, a high "
            "bar to advance.\n\n"
            f"Mission objective:\n{plan.objective}\n\n"
            f"Phase: {phase.id} ({phase.engine})\n"
            f"Exit criteria:\n{criteria}\n\n"
            f"Latest evaluation:\n{eval_summary}\n\n"
            f"Recent blackboard for this phase:\n{digest}\n\n"
            "Respond with exactly one line:\n"
            "  GATE: pass    — exit criteria met; advance to the next phase\n"
            "  GATE: repeat  — close, but this phase needs another iteration\n"
            "  GATE: revise  — the PLAN itself is wrong; re-plan remaining phases\n"
            "  GATE: done    — this is the FINAL phase and the whole mission is "
            "complete (the harness still verifies the done-gate mechanically)\n"
            "Then a one-line REASON:. You may also emit a POST: phase_gate block."
        )
