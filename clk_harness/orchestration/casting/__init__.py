"""Dynamic agent casting.

The chief authors the project's roster of agents and the workflows that
wire them together. Decomposed package; this ``__init__`` preserves the
public surface of the former ``clk_harness/orchestration/casting.py``:

* :mod:`.director` — dynamic team casting logic: parsing
  ``PROPOSE_*`` blocks, applying them, and the casting protocol /
  objective handed to the chief.
* :mod:`.roster` — role/agent definitions: baseline agents,
  duplicate detection, prompt scaffolding, and registry mutators.
"""

from .director import (
    CASTING_PROTOCOL,
    CharterProposal,
    ConsensusProposal,
    DelegateProposal,
    PlanProposal,
    RoleProposal,
    WorkflowProposal,
    _parse_simple_phase_list,
    _split_items,
    apply_response_proposals,
    casting_objective,
    parse_charter_proposal,
    parse_consensus_proposals,
    parse_delegate_proposals,
    parse_plan_proposal,
    parse_role_proposals,
    parse_workflow_proposals,
)
from .roster import (
    _PROTOCOL_MARKER,
    BASELINE_AGENTS,
    DEFAULT_MAX_DYNAMIC_ROLES,
    DEFAULT_PROMPT_SIM_THRESHOLD,
    CastingResult,
    _ensure_prompt_file,
    _harness_protocol_suffix,
    _log_casting_event,
    _name_key,
    _normalize_name,
    _prompt_similarity,
    _prompt_tokens,
    _similar_existing_name,
    _similar_existing_prompt,
    is_baseline,
    list_roles,
    register_role,
    remove_role,
    render_roster_summary,
    write_workflow,
)

__all__ = [
    "BASELINE_AGENTS",
    "CASTING_PROTOCOL",
    "CastingResult",
    "CharterProposal",
    "ConsensusProposal",
    "DelegateProposal",
    "DEFAULT_MAX_DYNAMIC_ROLES",
    "DEFAULT_PROMPT_SIM_THRESHOLD",
    "PlanProposal",
    "RoleProposal",
    "WorkflowProposal",
    "_PROTOCOL_MARKER",
    "_ensure_prompt_file",
    "_harness_protocol_suffix",
    "_log_casting_event",
    "_name_key",
    "_normalize_name",
    "_parse_simple_phase_list",
    "_prompt_similarity",
    "_prompt_tokens",
    "_similar_existing_name",
    "_similar_existing_prompt",
    "_split_items",
    "apply_response_proposals",
    "casting_objective",
    "is_baseline",
    "list_roles",
    "parse_charter_proposal",
    "parse_consensus_proposals",
    "parse_delegate_proposals",
    "parse_plan_proposal",
    "parse_role_proposals",
    "parse_workflow_proposals",
    "register_role",
    "remove_role",
    "render_roster_summary",
    "write_workflow",
]
