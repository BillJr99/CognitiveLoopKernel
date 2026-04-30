"""Orchestration primitives for CLK."""

from .agent import AgentObserver, AgentRunner
from .workflow import Workflow, WorkflowRunner, is_provider_failure, load_workflow
from .ralph_loop import RalphLoop
from .autoresearch_loop import AutoresearchLoop
from .evaluator import Evaluator, EvalResult
from .scheduler import Scheduler
from .casting import (
    BASELINE_AGENTS,
    ConsensusProposal,
    CastingResult,
    RoleProposal,
    WorkflowProposal,
    apply_response_proposals,
    casting_objective,
    is_baseline,
    list_roles,
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
    "Scheduler",
    "BASELINE_AGENTS",
    "ConsensusProposal",
    "CastingResult",
    "RoleProposal",
    "WorkflowProposal",
    "apply_response_proposals",
    "casting_objective",
    "is_baseline",
    "list_roles",
    "parse_role_proposals",
    "parse_consensus_proposals",
    "parse_workflow_proposals",
    "register_role",
    "remove_role",
    "render_roster_summary",
    "write_workflow",
]
