"""Orchestration primitives for CLK."""

from .agent import AgentObserver, AgentRunner
from .workflow import Workflow, WorkflowRunner, is_provider_failure, load_workflow
from .ralph_loop import RalphLoop
from .autoresearch_loop import AutoresearchLoop
from .evaluator import Evaluator, EvalResult, derive_validation
from .scheduler import Scheduler
from .telemetry import CycleTelemetry
from .done_gate import DoneGateVerdict, evaluate_done_gate
from .charter import Charter, bootstrap_charter, derive_done_criteria, load_charter
from .mission import MissionPlan, MissionRunner, PhaseSpec, load_plan
from .casting import (
    BASELINE_AGENTS,
    CharterProposal,
    ConsensusProposal,
    CastingResult,
    PlanProposal,
    RoleProposal,
    WorkflowProposal,
    apply_response_proposals,
    casting_objective,
    is_baseline,
    list_roles,
    parse_charter_proposal,
    parse_plan_proposal,
    parse_role_proposals,
    parse_consensus_proposals,
    parse_workflow_proposals,
    register_role,
    remove_role,
    render_roster_summary,
    write_workflow,
)

__all__ = [
    "AgentObserver",
    "AgentRunner",
    "Workflow",
    "WorkflowRunner",
    "is_provider_failure",
    "load_workflow",
    "RalphLoop",
    "AutoresearchLoop",
    "Evaluator",
    "EvalResult",
    "derive_validation",
    "Scheduler",
    "CycleTelemetry",
    "DoneGateVerdict",
    "evaluate_done_gate",
    "Charter",
    "bootstrap_charter",
    "derive_done_criteria",
    "load_charter",
    "MissionPlan",
    "MissionRunner",
    "PhaseSpec",
    "load_plan",
    "BASELINE_AGENTS",
    "CharterProposal",
    "ConsensusProposal",
    "CastingResult",
    "PlanProposal",
    "RoleProposal",
    "WorkflowProposal",
    "apply_response_proposals",
    "casting_objective",
    "is_baseline",
    "list_roles",
    "parse_charter_proposal",
    "parse_plan_proposal",
    "parse_role_proposals",
    "parse_consensus_proposals",
    "parse_workflow_proposals",
    "register_role",
    "remove_role",
    "render_roster_summary",
    "write_workflow",
]
