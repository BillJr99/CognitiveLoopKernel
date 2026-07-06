"""Casting director: dynamic team casting logic.

Parses ``PROPOSE_ROLE`` / ``PROPOSE_WORKFLOW`` / ``PROPOSE_CONSENSUS`` /
``PROPOSE_CHARTER`` / ``PROPOSE_PLAN`` blocks out of any agent's
response text, applies them to the roster, and produces the casting
protocol / objective handed to the chief.

Format the chief (or any agent) emits for a role proposal::

    PROPOSE_ROLE: data_steward
    ROLE: ensure data integrity and schema versioning
    PROVIDER: claude
    PROMPT:
    You are the **Data Steward** agent.
    Objective: $objective
    State: $state_summary
    ...
    END_ROLE

And for a workflow::

    PROPOSE_WORKFLOW: engineering
    DESCRIPTION: Custom development cycle
    YAML:
    name: engineering
    description: Custom development cycle
    stages:
      - id: decompose
        agent: chief
        objective: ...
      - id: implement
        agent: data_steward
        objective: ...
        depends_on: [decompose]
        commit: true
    END_WORKFLOW

Both blocks may appear multiple times in the same response; ``apply``
returns counts so callers can log / surface the changes.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from ...config import Paths
from ...log import get_logger, log_exception
from ...utils.activity_log import log_event
from .roster import (
    DEFAULT_MAX_DYNAMIC_ROLES,
    CastingResult,
    _normalize_name,
    register_role,
    write_workflow,
)

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class RoleProposal:
    name: str
    role: str = ""
    provider: Optional[str] = None
    capabilities: List[str] = field(default_factory=list)
    prompt: str = ""


@dataclass
class WorkflowProposal:
    name: str
    description: str = ""
    yaml_body: str = ""


@dataclass
class ConsensusProposal:
    name: str
    agents: List[str] = field(default_factory=list)
    copies: int = 3
    objective: str = ""


@dataclass
class DelegateProposal:
    """A context-isolated sub-task handed to an ephemeral child agent.

    The child does NOT inherit the caller's blackboard; it runs a bounded
    ``objective`` and its distilled result returns to the caller as a single
    ``delegate_result`` post.
    """
    name: str
    target: str = ""       # agent/role that runs the subtask
    context: str = ""      # optional one-line context handed in
    objective: str = ""    # the bounded task (TASK: body)


@dataclass
class CharterProposal:
    """A chief-authored mission charter (the up-front commitment).

    The plan and the done-gate are derived from this, so "done" is judged
    against what the chief committed to rather than drifting.
    """
    mission: str = ""
    scope: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    success: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)


@dataclass
class PlanProposal:
    """An ordered list of lifecycle phases for a mission."""
    phases: List[Dict[str, Any]] = field(default_factory=list)


_ROLE_HEAD_RE = re.compile(r"^\s*PROPOSE_ROLE\s*:\s*([A-Za-z][A-Za-z0-9_\-]*)\s*$", re.MULTILINE)
_ROLE_FIELD_RE = re.compile(r"^(ROLE|PROVIDER|CAPABILITIES)\s*:\s*(.*)$", re.IGNORECASE)
_ROLE_PROMPT_RE = re.compile(r"^\s*PROMPT\s*:\s*$", re.IGNORECASE)
_ROLE_END_RE = re.compile(r"^\s*END_ROLE\s*$", re.IGNORECASE)

_WF_HEAD_RE = re.compile(r"^\s*PROPOSE_WORKFLOW\s*:\s*([A-Za-z][A-Za-z0-9_\-]*)\s*$", re.MULTILINE)
_WF_FIELD_RE = re.compile(r"^(DESCRIPTION)\s*:\s*(.*)$", re.IGNORECASE)
_WF_YAML_RE = re.compile(r"^\s*YAML\s*:\s*$", re.IGNORECASE)
_WF_END_RE = re.compile(r"^\s*END_WORKFLOW\s*$", re.IGNORECASE)

_CONS_HEAD_RE = re.compile(r"^\s*PROPOSE_CONSENSUS\s*:\s*([A-Za-z][A-Za-z0-9_\-]*)\s*$", re.MULTILINE)
_CONS_FIELD_RE = re.compile(r"^(AGENTS?|COPIES)\s*:\s*(.*)$", re.IGNORECASE)
_CONS_OBJECTIVE_RE = re.compile(r"^\s*OBJECTIVE\s*:\s*$", re.IGNORECASE)
_CONS_END_RE = re.compile(r"^\s*END_CONSENSUS\s*$", re.IGNORECASE)

_DELEG_HEAD_RE = re.compile(r"^\s*DELEGATE\s*:\s*([A-Za-z][A-Za-z0-9_\-]*)\s*$", re.MULTILINE)
_DELEG_FIELD_RE = re.compile(r"^(TO|CONTEXT)\s*:\s*(.*)$", re.IGNORECASE)
_DELEG_TASK_RE = re.compile(r"^\s*TASK\s*:\s*$", re.IGNORECASE)
_DELEG_END_RE = re.compile(r"^\s*END_DELEGATE\s*$", re.IGNORECASE)

_CHARTER_HEAD_RE = re.compile(r"^\s*PROPOSE_CHARTER\s*:?\s*$", re.IGNORECASE)
_CHARTER_END_RE = re.compile(r"^\s*END_CHARTER\s*$", re.IGNORECASE)
_CHARTER_FIELD_RE = re.compile(
    r"^(MISSION|SCOPE|NON_GOALS|NONGOALS|SUCCESS|SUCCESS_CRITERIA|CONSTRAINTS|ASSUMPTIONS)\s*:\s*(.*)$",
    re.IGNORECASE,
)

_PLAN_HEAD_RE = re.compile(r"^\s*PROPOSE_PLAN\s*:?\s*$", re.IGNORECASE)
_PLAN_PHASES_RE = re.compile(r"^\s*PHASES\s*:\s*$", re.IGNORECASE)
_PLAN_END_RE = re.compile(r"^\s*END_PLAN\s*$", re.IGNORECASE)


def _split_items(value: str) -> List[str]:
    """Split a charter field value into items.

    Items may be separated by ``;`` (preferred, since success criteria can
    contain commas) or, when no semicolon is present, by commas. A leading
    ``[`` / trailing ``]`` is tolerated.
    """
    v = (value or "").strip()
    if v.startswith("[") and v.endswith("]"):
        v = v[1:-1]
    if not v:
        return []
    sep = ";" if ";" in v else ","
    return [item.strip().strip("-").strip() for item in v.split(sep) if item.strip().strip("-").strip()]


def parse_charter_proposal(text: str) -> Optional[CharterProposal]:
    """Extract a single ``PROPOSE_CHARTER`` block from ``text`` (or None)."""
    if not text or "PROPOSE_CHARTER" not in text.upper():
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not _CHARTER_HEAD_RE.match(lines[i]):
            i += 1
            continue
        prop = CharterProposal()
        i += 1
        while i < len(lines):
            line = lines[i]
            if _CHARTER_END_RE.match(line):
                i += 1
                break
            fm = _CHARTER_FIELD_RE.match(line)
            if fm:
                key = fm.group(1).upper()
                val = fm.group(2).strip()
                if key == "MISSION":
                    prop.mission = val
                elif key == "SCOPE":
                    prop.scope = _split_items(val)
                elif key in ("NON_GOALS", "NONGOALS"):
                    prop.non_goals = _split_items(val)
                elif key in ("SUCCESS", "SUCCESS_CRITERIA"):
                    prop.success = _split_items(val)
                elif key == "CONSTRAINTS":
                    prop.constraints = _split_items(val)
                elif key == "ASSUMPTIONS":
                    prop.assumptions = _split_items(val)
            i += 1
        if prop.mission or prop.success or prop.scope:
            return prop
    return None


def _parse_simple_phase_list(body: str) -> List[Dict[str, Any]]:
    """Dependency-free parser for the PHASES YAML list.

    Handles the documented shape: a list of ``- key: value`` blocks where
    values are scalars or ``[a, b]`` inline lists. Used as a fallback when
    PyYAML is unavailable.
    """
    phases: List[Dict[str, Any]] = []
    current: Optional[Dict[str, Any]] = None
    for raw in body.splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        stripped = line.lstrip()
        is_item = stripped.startswith("- ")
        if is_item:
            stripped = stripped[2:].lstrip()
            current = {}
            phases.append(current)
        if current is None:
            current = {}
            phases.append(current)
        if ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        key = key.strip()
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            inner = value[1:-1]
            current[key] = [x.strip().strip('"\'') for x in inner.split(",") if x.strip()]
        elif value:
            current[key] = value.strip('"\'')
        else:
            current[key] = ""
    return [p for p in phases if p.get("id")]


def parse_plan_proposal(text: str) -> Optional[PlanProposal]:
    """Extract a single ``PROPOSE_PLAN`` block (phases list) from ``text``."""
    if not text or "PROPOSE_PLAN" not in text.upper():
        return None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        if not _PLAN_HEAD_RE.match(lines[i]):
            i += 1
            continue
        i += 1
        # skip to PHASES:
        body_lines: List[str] = []
        seen_phases = False
        while i < len(lines):
            line = lines[i]
            if _PLAN_END_RE.match(line):
                i += 1
                break
            if not seen_phases:
                if _PLAN_PHASES_RE.match(line):
                    seen_phases = True
                i += 1
                continue
            body_lines.append(line)
            i += 1
        body = "\n".join(body_lines).strip("\n")
        if not body:
            continue
        phases: List[Dict[str, Any]] = []
        try:
            import yaml as _yaml  # type: ignore
            parsed = _yaml.safe_load(body)
            if isinstance(parsed, list):
                phases = [p for p in parsed if isinstance(p, dict) and p.get("id")]
        except Exception:
            phases = []
        if not phases:
            phases = _parse_simple_phase_list(body)
        if phases:
            return PlanProposal(phases=phases)
    return None


def parse_role_proposals(text: str) -> List[RoleProposal]:
    """Extract every PROPOSE_ROLE block from ``text``.

    Returns an empty list (never raises) when nothing matches or input
    is malformed; that way callers can apply this to *any* agent
    response without guarding it.
    """
    if not text:
        return []
    out: List[RoleProposal] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _ROLE_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        prop = RoleProposal(name=m.group(1).strip())
        i += 1
        prompt_lines: List[str] = []
        in_prompt = False
        while i < len(lines):
            line = lines[i]
            if _ROLE_END_RE.match(line):
                i += 1
                break
            if not in_prompt:
                fm = _ROLE_FIELD_RE.match(line)
                if fm:
                    key = fm.group(1).upper()
                    val = fm.group(2).strip()
                    if key == "ROLE":
                        prop.role = val
                    elif key == "PROVIDER":
                        prop.provider = val or None
                    elif key == "CAPABILITIES":
                        prop.capabilities = [
                            c.strip() for c in re.split(r"[,\s]+", val) if c.strip()
                        ]
                    i += 1
                    continue
                if _ROLE_PROMPT_RE.match(line):
                    in_prompt = True
                    i += 1
                    continue
                # tolerate blank or stray lines inside the header section
                i += 1
                continue
            # in_prompt
            prompt_lines.append(line)
            i += 1
        prop.prompt = "\n".join(prompt_lines).strip("\n")
        if prop.name:
            out.append(prop)
    return out


def parse_workflow_proposals(text: str) -> List[WorkflowProposal]:
    if not text:
        return []
    out: List[WorkflowProposal] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _WF_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        prop = WorkflowProposal(name=m.group(1).strip())
        i += 1
        yaml_lines: List[str] = []
        in_yaml = False
        while i < len(lines):
            line = lines[i]
            if _WF_END_RE.match(line):
                i += 1
                break
            if not in_yaml:
                fm = _WF_FIELD_RE.match(line)
                if fm:
                    if fm.group(1).upper() == "DESCRIPTION":
                        prop.description = fm.group(2).strip()
                    i += 1
                    continue
                if _WF_YAML_RE.match(line):
                    in_yaml = True
                    i += 1
                    continue
                i += 1
                continue
            yaml_lines.append(line)
            i += 1
        prop.yaml_body = "\n".join(yaml_lines).strip("\n")
        if prop.name and prop.yaml_body:
            out.append(prop)
    return out


def parse_consensus_proposals(text: str) -> List[ConsensusProposal]:
    if not text:
        return []
    out: List[ConsensusProposal] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _CONS_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        prop = ConsensusProposal(name=m.group(1).strip())
        i += 1
        objective_lines: List[str] = []
        in_objective = False
        while i < len(lines):
            line = lines[i]
            if _CONS_END_RE.match(line):
                i += 1
                break
            if not in_objective:
                fm = _CONS_FIELD_RE.match(line)
                if fm:
                    key = fm.group(1).upper()
                    val = fm.group(2).strip()
                    if key in ("AGENT", "AGENTS"):
                        prop.agents = [
                            _normalize_name(a)
                            for a in re.split(r"[, ]+", val)
                            if _normalize_name(a)
                        ]
                    elif key == "COPIES":
                        try:
                            prop.copies = max(1, int(val))
                        except ValueError:
                            prop.copies = 3
                    i += 1
                    continue
                if _CONS_OBJECTIVE_RE.match(line):
                    in_objective = True
                    i += 1
                    continue
                i += 1
                continue
            objective_lines.append(line)
            i += 1
        prop.objective = "\n".join(objective_lines).strip()
        if prop.name and prop.objective:
            out.append(prop)
    return out


def parse_delegate_proposals(text: str) -> List[DelegateProposal]:
    """Extract ``DELEGATE:`` blocks from an agent's response.

    Block grammar (mirrors PROPOSE_CONSENSUS)::

        DELEGATE: <short_name>
        TO: <agent_or_role>
        CONTEXT: <optional one-liner>
        TASK:
        <multi-line bounded objective>
        END_DELEGATE

    A proposal is only emitted when it names a target and a task.
    """
    if not text:
        return []
    out: List[DelegateProposal] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _DELEG_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        prop = DelegateProposal(name=m.group(1).strip())
        i += 1
        task_lines: List[str] = []
        in_task = False
        while i < len(lines):
            line = lines[i]
            if _DELEG_END_RE.match(line):
                i += 1
                break
            if not in_task:
                fm = _DELEG_FIELD_RE.match(line)
                if fm:
                    key = fm.group(1).upper()
                    val = fm.group(2).strip()
                    if key == "TO":
                        prop.target = _normalize_name(val)
                    elif key == "CONTEXT":
                        prop.context = val
                    i += 1
                    continue
                if _DELEG_TASK_RE.match(line):
                    in_task = True
                    i += 1
                    continue
                i += 1
                continue
            task_lines.append(line)
            i += 1
        prop.objective = "\n".join(task_lines).strip()
        if prop.name and prop.target and prop.objective:
            out.append(prop)
    return out


def apply_response_proposals(
    paths: Paths,
    response_text: str,
    *,
    agents_cfg: Optional[Dict[str, Any]] = None,
    max_dynamic: int = DEFAULT_MAX_DYNAMIC_ROLES,
    source_agent: str = "",
    on_change: Optional[Callable[[str, str], None]] = None,
) -> CastingResult:
    """Scan an agent's response, apply every proposal it contains.

    Safe to call after every agent run. Returns a result describing what
    actually changed; callers can use ``result.is_empty()`` to skip
    further work cheaply.
    """
    result = CastingResult()
    if not response_text:
        return result
    try:
        for prop in parse_role_proposals(response_text):
            ok, status = register_role(
                paths,
                prop,
                agents_cfg=agents_cfg,
                max_dynamic=max_dynamic,
                source_agent=source_agent,
                on_change=on_change,
            )
            if not ok:
                result.roles_skipped.append(f"{prop.name}:{status}")
                log_event(
                    paths,
                    "role_skipped",
                    agent=source_agent,
                    name=prop.name,
                    role=prop.role,
                    provider=prop.provider,
                    prompt_length=len(prop.prompt or ""),
                    prompt=prop.prompt or "",
                    reason=status,
                )
            elif status == "added":
                result.roles_added.append(prop.name)
            else:
                result.roles_updated.append(prop.name)
        for wf in parse_workflow_proposals(response_text):
            ok, status = write_workflow(paths, wf, source_agent=source_agent, on_change=on_change)
            if ok:
                result.workflows_written.append(wf.name)
            else:
                result.workflows_skipped.append(f"{wf.name}:{status}")
                log_event(
                    paths,
                    "workflow_skipped",
                    agent=source_agent,
                    name=wf.name,
                    description=wf.description,
                    yaml_length=len(wf.yaml_body or ""),
                    yaml=wf.yaml_body or "",
                    reason=status,
                )
    except Exception as exc:
        log_exception("orchestration.casting.apply_response_proposals", exc)
        result.errors.append(str(exc))
    return result


# ---------------------------------------------------------------------------
# Casting prompt helpers
# ---------------------------------------------------------------------------


CASTING_PROTOCOL = """\
ROLE-CASTING PROTOCOL (machine-parsed, follow exactly)

You may emit any number of these blocks in your response. The harness
parses them and updates `.clk/config/agents.json`, `.clk/prompts/`, and
`.clk/config/workflows/` immediately.

Add or refresh a role:

  PROPOSE_ROLE: <snake_case_name>
  ROLE: <one-line description>
  PROVIDER: <optional provider name, omit to inherit default>
  CAPABILITIES: <optional comma-separated list of capability hints>
  PROMPT:
  <full prompt body. Use $$idea_title, $$idea_statement, $$project_name,
   $$project_root, $$state_summary, $$objective, $$iteration as placeholders.>
  END_ROLE

Capability hints (CAPABILITIES field)
  Omit the field entirely to use provider defaults (tools on, standard thinking).
  Combine as needed: "no-tools, thinking-off"

  no-tools          Disable all tools. Best for research / doc-writing agents
                    that only need to produce text — avoids extra model round
                    trips and cuts latency significantly.
  no-builtin-tools  Disable built-in tools but keep custom extensions.
  thinking-off      Skip chain-of-thought. Best for formatting, summarising,
                    or any task that does not require deep reasoning.
  thinking-low      Minimal thinking — slightly faster than the default.
  thinking-medium   Standard thinking level (provider default).
  thinking-high     Deep reasoning — use for architecture, debugging, or tasks
                    where correctness matters more than speed.
  thinking-xhigh    Maximum thinking — slowest, most thorough.

Author or replace a workflow (the harness will save it as
`.clk/config/workflows/<name>.yaml`):

  PROPOSE_WORKFLOW: <name>
  DESCRIPTION: <one line>
  YAML:
  name: <name>
  description: <one line>
  stages:
    - id: <id>
      agent: <agent name>
      objective: <objective>
      depends_on: [other_id, ...]   # optional
      validation: "<shell command>" # optional, exit 0 = pass
      commit: true                  # optional, default true
  END_WORKFLOW

Rules
- Roster cap is enforced (default 12 dynamic roles + the 3 baseline roles).
  Drop or merge before adding past the cap.
- Baseline roles (chief, ralph, qa) cannot be removed but you may refresh
  their prompt bodies.
- engineer is the canonical implementation agent. It is NOT a default
  baseline — you must create it with PROPOSE_ROLE: engineer when a project
  needs an implementer. The name is reserved: NEVER create `engineering`,
  `engineers`, `coder`, `developer`, `programmer`, `implementer`, or any
  other variant. The harness will explicitly deny such proposals and report
  back to you via $casting_feedback. If engineer already exists, use it.
- ralph is the iterative refinement and autoresearch driver. Always
  include at least one ralph stage in engineering workflows so the output
  gets iteratively improved before delivery. Do NOT create a separate
  autoresearch agent — ralph handles both modes.
- qa is the validation agent. Always include at least one qa stage in
  every engineering workflow, typically as the final stage before done.
- All other roles (analyst, researcher, architect, etc.) are dynamic —
  create them per project as needed.
- If a stage references an agent you have not defined yet, define it in
  the same response with a PROPOSE_ROLE block.
- Workflows may use any combination of baseline and dynamic agents.
- Prefer assigning work to an existing agent when its role already fits.
  Create or refresh a role when the need is distinct enough that an
  existing role would blur ownership or do materially worse work.
- Before emitting ANY PROPOSE_ROLE block, run this mandatory pre-flight:
    1. Read every agent's prompt_preview in the current roster.
    2. Ask: "Does any existing agent's prompt already describe this work?"
       If YES → use that agent. Do NOT emit PROPOSE_ROLE.
    3. Ask: "Is this name a synonym, plural, gerund, or department label
       of an existing name?" (e.g. `engineering` when `engineer` exists,
       `researchers` when `researcher` exists.) If YES → use the existing
       name. Do NOT emit PROPOSE_ROLE.
    4. Only emit PROPOSE_ROLE when both checks pass: no functional overlap
       AND a genuinely distinctive name.
- Functional overlap is the primary test — name similarity is secondary.
  An agent named differently is still a duplicate if its prompt describes
  the same work as an existing agent's prompt. The new role's PROMPT must
  state what it owns that no current agent's prompt already covers.
- New role prompts and role lines must state the distinct responsibility
  the role owns compared with the nearest existing agent.
"""


def casting_objective(idea_title: str, idea_statement: str) -> str:
    return (
        "You are the casting director for this project. Decide on the team of agents "
        "that will best serve the idea below, author each agent's prompt, and emit a "
        "workflow YAML that wires them into one engineering cycle.\n\n"
        f"Idea: {idea_title}\n{idea_statement}\n\n"
        "Use the role-casting protocol to add or refresh agents and to write the "
        "`engineering` workflow. Keep the roster small and specific to this project: "
        "reuse existing agents when they fit, drop generic roles that won't earn "
        "their keep, and invent specialists only when this particular system needs "
        "a distinct owner."
    )
