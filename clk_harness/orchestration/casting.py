"""Dynamic agent casting.

The chief authors the project's roster of agents and the workflows that
wire them together. This module:

  * defines the immutable BASELINE_AGENTS the chief cannot remove
  * parses ``PROPOSE_ROLE`` / ``PROPOSE_WORKFLOW`` blocks out of any
    agent's response text
  * registers / removes roles in ``.clk/config/agents.json`` and
    ``.clk/prompts/`` and updates an in-memory ``agents_cfg`` so a
    proposal made mid-workflow takes effect on the very next stage
  * produces the prompt context ("current roster") and the casting
    objective handed to the chief

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

import json
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..config import Paths, load_agents_config, save_json
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception


# ---------------------------------------------------------------------------
# Baseline (always-present) agents
# ---------------------------------------------------------------------------

# These names cannot be removed even if the chief asks. They are the
# minimal spine the harness assumes always exists:
#   * chief  - decomposition, casting, workflow authoring
#   * ralph  - iterative refinement loop driver
#   * qa     - output validation (must appear at least once in every workflow)
# The chief creates other dynamic roles (analyst, researcher, etc.) per
# project and is instructed to always include ralph and qa. Some names
# (e.g. "engineer") are reserved as similarity anchors in _SEED_ROLE_ANCHORS
# rather than baseline agents — they prevent near-duplicate variants like
# "engineering" from being registered, but are themselves proposable as
# dynamic roles.
BASELINE_AGENTS: Tuple[str, ...] = (
    "chief",
    "ralph",
    "qa",
)


# Default cap on how many *dynamic* (non-baseline) roles can coexist.
# Configurable via ``clk.config.json::casting.max_dynamic_roles``.
DEFAULT_MAX_DYNAMIC_ROLES = 12


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


# ---------------------------------------------------------------------------
# Registry mutators
# ---------------------------------------------------------------------------


# Reserved names that may never be assigned to a dynamic role.
_RESERVED_NAMES = set(BASELINE_AGENTS)

# Names that always act as similarity anchors even when absent from
# agents.json.  "autoresearch" was absorbed into ralph; keeping it here
# prevents the chief from accidentally re-creating it as a dynamic role.
# "engineer" is no longer a default baseline but its name is reserved so
# that variants like "engineering" or "coder" are always rejected.
_SEED_ROLE_ANCHORS: frozenset = frozenset({
    "autoresearch",
    "engineer",
})


def is_baseline(name: str) -> bool:
    return name in _RESERVED_NAMES


def list_roles(paths: Paths) -> Dict[str, Dict[str, Any]]:
    return dict((load_agents_config(paths).get("agents") or {}))


def _agents_path(paths: Paths) -> Path:
    return paths.config / "agents.json"


def _persist(paths: Paths, agents_cfg: Dict[str, Any]) -> None:
    save_json(_agents_path(paths), agents_cfg)


def _log_casting_event(paths: Paths, event: Dict[str, Any]) -> None:
    """Append a JSONL entry to ``.clk/state/casting.log`` AND mirror it
    into the consolidated activity log.

    casting.log stays focused on roster history (easier to analyze in
    isolation: which specialists each project needs); activity.jsonl
    is the chronological super-log of everything.
    """
    try:
        paths.state.mkdir(parents=True, exist_ok=True)
        target = paths.state / "casting.log"
        payload = {"timestamp": datetime.now().isoformat(timespec="seconds"), **event}
        with target.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload) + "\n")
        log_event(paths, event.get("event") or "casting", **{k: v for k, v in event.items() if k != "event"})
    except Exception as exc:
        log_exception("orchestration.casting._log_casting_event", exc)


def _normalize_name(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", (name or "").strip()).strip("_").lower()


# Curated synonym table — coarse, hand-maintained. Catches role-name
# pairs that are functionally identical even though their morphology is
# unrelated (``coder`` and ``engineer``). Both sides collapse to the
# canonical form on the right.
_NAME_SYNONYMS: Dict[str, str] = {
    "coder": "engineer",
    "developer": "engineer",
    "engineering": "engineer",  # gerund form — always a duplicate of the seed role
    "programmer": "engineer",
    "implementer": "engineer",
    "implementor": "engineer",
    "builder": "engineer",
    "tester": "qa",
    "quality": "qa",
    "validator": "qa",
    "auditor": "qa",
    "reviewer": "qa",
    "research": "researcher",
    "scientist": "researcher",
    "investigator": "researcher",
    "analysis": "analyst",
    "analytics": "analyst",
    "writer": "doc_writer",
    "documenter": "doc_writer",
    "scribe": "doc_writer",
    "operator": "operator",
    "ops": "operator",
    "devops": "operator",
    "deploy": "operator",
    "deployer": "operator",
    "ux": "ux_writer",
    "designer": "ux_writer",
}


# Suffix → reduction. Order matters (longest first). Each entry is
# (suffix, replacement). Rules apply when ``key`` ends in ``suffix`` and
# the remainder is at least 3 chars. The loop runs to a fixed point so
# ``engineers`` → ``engineer`` → ``engine`` collapses cleanly.
_NAME_SUFFIXES: List[Tuple[str, str]] = [
    ("ization", ""),
    ("ation", ""),
    ("ment", ""),
    ("ance", ""),
    ("ence", ""),
    ("ity", ""),
    ("ies", "y"),
    ("ing", ""),
    # Plural ``s``/``es`` come BEFORE ``er``/``or``/``ist`` so
    # ``developers`` first reduces to ``developer`` (then synonym maps
    # to ``engineer``, then ``-er`` strips to ``engine``). If we strip
    # ``ers`` here directly the chain skips the synonym table.
    ("es", ""),
    ("s", ""),
    ("er", ""),
    ("or", ""),
    ("ist", ""),
]


def _name_key(name: str) -> str:
    """Compact name used to catch near-duplicate agent names.

    Lower-cases, strips punctuation, applies synonym substitution
    (``coder`` → ``engineer``), then iteratively peels morphological
    suffixes (``-er``, ``-ing``, ``-ation``, ``-ity``, ...) to the
    smallest stable form. The output is not meant to be human-readable
    — only to compare equal across near-duplicates so
    ``_similar_existing_name`` can reject them.
    """
    key = re.sub(r"[^a-z0-9]+", "", _normalize_name(name))
    if not key:
        return key
    # Repeat (synonym → strip-one-suffix) until a fixed point. Capped
    # iteration count guards against accidental rule cycles.
    for _ in range(6):
        prev = key
        if key in _NAME_SYNONYMS:
            key = _NAME_SYNONYMS[key]
        for suffix, replacement in _NAME_SUFFIXES:
            if key.endswith(suffix) and len(key) - len(suffix) >= 3:
                key = key[: -len(suffix)] + replacement
                break
        if key == prev:
            break
    return key


def _similar_existing_name(name: str, agents: Dict[str, Any]) -> Optional[str]:
    key = _name_key(name)
    normalized = _normalize_name(name)
    # Always include seed anchors so the check fires even when agents.json
    # was manually trimmed to baseline-only and the seed role is absent.
    all_names = set(agents.keys()) | _SEED_ROLE_ANCHORS | _RESERVED_NAMES
    for existing in sorted(all_names):
        ex_key = _name_key(existing)
        if not key or not ex_key:
            continue
        # Creating the exact canonical name is always allowed — only aliases
        # are blocked. e.g. "engineer" vs seed anchor "engineer": OK.
        if normalized == existing:
            continue
        if key == ex_key:
            return existing
        if len(key) >= 6 and (key.startswith(ex_key) or ex_key.startswith(key)):
            return existing
    return None


# ---------------------------------------------------------------------------
# Prompt-body similarity (TF-IDF-ish, dependency-free)
# ---------------------------------------------------------------------------


_TOKEN_RE = re.compile(r"[a-z0-9]+")
# Throw out boilerplate footer text, role name itself, and overly common
# tokens that appear in nearly every prompt; without this every prompt
# similarity score floats around 0.5 just because of the shared scaffold.
_PROMPT_STOPWORDS: frozenset = frozenset({
    "the", "a", "an", "and", "or", "of", "to", "in", "on", "for",
    "you", "your", "is", "are", "be", "this", "that", "with", "at",
    "by", "as", "it", "its", "from", "into", "if", "when", "must",
    "do", "does", "not", "no", "any", "all", "one", "each", "per",
    "agent", "agents", "role", "roles", "objective", "state", "summary",
    "project", "name", "root", "workspace", "idea", "title", "statement",
    "iteration", "section", "output", "outputs", "input", "inputs",
    "validation", "commit", "validate", "validated",
    "action", "actions", "block", "blocks", "harness", "rules",
    "stay", "inside", "$project_name", "$project_root", "$objective",
    "$state_summary", "$idea_title", "$idea_statement",
})


def _prompt_tokens(text: str) -> List[str]:
    return [
        t for t in _TOKEN_RE.findall((text or "").lower())
        if t not in _PROMPT_STOPWORDS and len(t) > 2
    ]


def _prompt_similarity(a: str, b: str) -> float:
    """Jaccard similarity of token sets after stopword removal.

    Cheap, dependency-free, good enough to flag prompts that are
    near-duplicates of an existing role's prompt body. Returns a value
    in [0.0, 1.0]. Empty inputs return 0.0.
    """
    sa = set(_prompt_tokens(a))
    sb = set(_prompt_tokens(b))
    if not sa or not sb:
        return 0.0
    inter = len(sa & sb)
    union = len(sa | sb)
    return inter / union if union else 0.0


# Threshold above which a new prompt is considered a duplicate of an
# existing one. Tuned conservatively: well-distinct specialists score
# under ~0.4; deliberate copies score 0.7+.
DEFAULT_PROMPT_SIM_THRESHOLD = 0.45


def _similar_existing_prompt(
    paths: Paths,
    new_prompt: str,
    agents: Dict[str, Any],
    *,
    threshold: float = DEFAULT_PROMPT_SIM_THRESHOLD,
) -> Optional[Tuple[str, float]]:
    """Return ``(existing_name, score)`` when ``new_prompt`` is too close
    to an already-registered prompt body, else ``None``.

    Reads each existing prompt off disk (from ``paths.prompts``) so
    the comparison sees what the agent will actually be invoked with,
    not the cached config role line. Failures reading a prompt skip
    that comparison rather than crash registration.
    """
    if not (new_prompt or "").strip():
        return None
    best: Optional[Tuple[str, float]] = None
    for ex_name in sorted(agents.keys()):
        cfg = agents.get(ex_name) or {}
        prompt_file = cfg.get("prompt") or f"{ex_name}.md"
        ex_path = paths.prompts / prompt_file
        if not ex_path.exists():
            continue
        try:
            ex_body = ex_path.read_text(encoding="utf-8")
        except Exception:
            continue
        score = _prompt_similarity(new_prompt, ex_body)
        if score >= threshold and (best is None or score > best[1]):
            best = (ex_name, score)
    return best


def _ensure_prompt_file(paths: Paths, name: str, prompt_body: str, role_line: str) -> str:
    """Write ``.clk/prompts/<name>.md`` if missing or if a body was provided.

    Returns the prompt filename actually used.
    """
    fname = f"{name}.md"
    target = paths.prompts / fname
    paths.prompts.mkdir(parents=True, exist_ok=True)
    if prompt_body and prompt_body.strip():
        try:
            target.write_text(prompt_body.rstrip() + "\n", encoding="utf-8")
        except Exception as exc:
            log_exception("orchestration.casting._ensure_prompt_file", exc)
    elif not target.exists():
        # No body provided and no existing file: scaffold a generic one
        # so the agent at least has a coherent prompt.
        scaffold = (
            f"You are the **{name.replace('_',' ').title()}** agent.\n\n"
            f"Role: {role_line or '(no role description)'}\n\n"
            "Project: $project_name\n"
            "Working directory: $project_root\n"
            "Idea: $idea_title - $idea_statement\n\n"
            "Current state summary:\n$state_summary\n\n"
            "Blackboard digest (peer posts filtered to your stage's inputs):\n"
            "$blackboard_digest\n\n"
            "Objective:\n$objective\n\n"
            "Output\n"
            "- The deliverable for this objective.\n"
            "- A POST: <type> block summarising the headline result so the\n"
            "  blackboard reflects what you produced. If your stage YAML\n"
            "  declared `outputs: [...]`, include each declared key in the\n"
            "  POST's PRODUCES list, otherwise the runner will warn the\n"
            "  contract is unmet.\n"
            "- A `Validation` section: a shell command (or `none`) that proves the deliverable.\n"
            "- A `Commit` section: a one-sentence commit message.\n"
            "\n"
            "Operating constraints\n"
            "- Stay inside `$project_root`. The `.clk/` subtree is harness state\n"
            "  and the harness rejects any ACTION:write that targets it.\n"
            "- Do not install global packages or use sudo.\n"
            "- Prefer editing existing files over creating new ones when feasible.\n"
            "- Create files and directories only when they have a clear, distinct\n"
            "  purpose that is not already served by existing project structure.\n"
            "- Avoid duplicate files, duplicate directories, and alternate\n"
            "  implementations of the same thing.\n"
            "- If you spot work that should be owned by a role that doesn't exist, emit a\n"
            "  `PROPOSE_ROLE:` block per the casting protocol.\n"
        )
        try:
            target.write_text(scaffold, encoding="utf-8")
        except Exception as exc:
            log_exception("orchestration.casting._ensure_prompt_file.scaffold", exc)
    return fname


@dataclass
class CastingResult:
    roles_added: List[str] = field(default_factory=list)
    roles_updated: List[str] = field(default_factory=list)
    roles_skipped: List[str] = field(default_factory=list)
    workflows_written: List[str] = field(default_factory=list)
    workflows_skipped: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not (
            self.roles_added
            or self.roles_updated
            or self.workflows_written
            or self.errors
        )

    def summary(self) -> str:
        bits: List[str] = []
        if self.roles_added:
            bits.append(f"+roles={','.join(self.roles_added)}")
        if self.roles_updated:
            bits.append(f"~roles={','.join(self.roles_updated)}")
        if self.roles_skipped:
            bits.append(f"!skipped={','.join(self.roles_skipped)}")
        if self.workflows_written:
            bits.append(f"+wf={','.join(self.workflows_written)}")
        if self.workflows_skipped:
            bits.append(f"!wf-skipped={','.join(self.workflows_skipped)}")
        if self.errors:
            bits.append(f"errors={len(self.errors)}")
        return " ".join(bits) or "(no changes)"


def register_role(
    paths: Paths,
    proposal: RoleProposal,
    *,
    agents_cfg: Optional[Dict[str, Any]] = None,
    max_dynamic: int = DEFAULT_MAX_DYNAMIC_ROLES,
    source_agent: str = "",
    on_change: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    """Register or refresh a single dynamic role.

    Returns ``(applied, status)``. ``status`` is one of
    ``"added"``, ``"updated"``, or a short skip reason.

    ``agents_cfg``, when supplied, is mutated in place so a runner that
    holds the dict sees the new agent without reloading from disk.
    """
    name = _normalize_name(proposal.name)
    if not name:
        return False, "empty_name"
    if is_baseline(name):
        # Allow refreshing a baseline's prompt body but not removing it
        # or rebinding its role line aggressively. Treat as update.
        if proposal.prompt:
            _ensure_prompt_file(paths, name, proposal.prompt, proposal.role)
            _log_casting_event(paths, {
                "event": "baseline_prompt_refreshed",
                "agent": source_agent,
                "name": name,
                "role": proposal.role,
                "provider": proposal.provider,
                "prompt_length": len(proposal.prompt or ""),
                "prompt": proposal.prompt or "",
            })
            if on_change is not None:
                try:
                    on_change(name, "prompt_updated")
                except Exception:
                    pass
            return True, "baseline_prompt_refreshed"
        return False, "baseline_protected"

    cfg = agents_cfg if agents_cfg is not None else load_agents_config(paths)
    agents = cfg.setdefault("agents", {})

    existing = agents.get(name)
    is_update = existing is not None
    if not is_update:
        similar = _similar_existing_name(name, agents)
        if similar:
            # Emit an explicit denial for engineer/engineering variants so the
            # chief's $casting_feedback makes the rule unambiguous.
            if _name_key(name) == _name_key("engineer"):
                canonical = similar if similar != name else "engineer"
                return False, (
                    f"engineer_alias_denied:{canonical} — "
                    f"'{name}' is a reserved alias of '{canonical}'; "
                    f"use '{canonical}' directly and do not create variants"
                )
            return False, f"similar_to_existing:{similar}"
        # Prompt-body similarity check: catches the case where the chief
        # invents a distinct name but writes a body that overlaps an
        # existing role's prompt. Skipped when no body is provided (the
        # scaffolded prompt is generic by design, would false-positive).
        if proposal.prompt and proposal.prompt.strip():
            sim = _similar_existing_prompt(paths, proposal.prompt, agents)
            if sim is not None:
                return False, f"similar_prompt_to:{sim[0]}({sim[1]:.2f})"
    if not is_update:
        # Enforce roster cap on *additions* only.
        dynamic_count = sum(1 for k in agents if k not in _RESERVED_NAMES)
        if dynamic_count >= max_dynamic:
            return False, f"cap_reached:{dynamic_count}"

    fname = _ensure_prompt_file(paths, name, proposal.prompt, proposal.role)
    agents[name] = {
        "prompt": fname,
        "provider": proposal.provider or (existing or {}).get("provider"),
        "role": proposal.role or (existing or {}).get("role", ""),
        "capabilities": proposal.capabilities or (existing or {}).get("capabilities") or [],
        "dynamic": True,
    }
    _persist(paths, cfg)

    status = "updated" if is_update else "added"
    _log_casting_event(paths, {
        "event": "role_" + status,
        "agent": source_agent,
        "name": name,
        "role": proposal.role,
        "provider": proposal.provider,
        "capabilities": list(proposal.capabilities or []),
        "prompt_length": len(proposal.prompt or ""),
        "prompt": proposal.prompt or "",
    })
    if on_change is not None:
        try:
            on_change(name, status)
        except Exception:
            pass
    return True, status


def remove_role(
    paths: Paths,
    name: str,
    *,
    agents_cfg: Optional[Dict[str, Any]] = None,
    on_change: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    name = _normalize_name(name)
    if is_baseline(name):
        return False, "baseline_protected"
    cfg = agents_cfg if agents_cfg is not None else load_agents_config(paths)
    agents = cfg.get("agents") or {}
    if name not in agents:
        return False, "not_found"
    agents.pop(name, None)
    cfg["agents"] = agents
    _persist(paths, cfg)
    _log_casting_event(paths, {"event": "role_removed", "name": name})
    if on_change is not None:
        try:
            on_change(name, "removed")
        except Exception:
            pass
    return True, "removed"


def write_workflow(
    paths: Paths,
    proposal: WorkflowProposal,
    *,
    source_agent: str = "",
    on_change: Optional[Callable[[str, str], None]] = None,
) -> Tuple[bool, str]:
    name = _normalize_name(proposal.name)
    if not name:
        return False, "empty_name"
    body = proposal.yaml_body.strip()
    if not body:
        return False, "empty_yaml"
    target = paths.workflows / f"{name}.yaml"
    paths.workflows.mkdir(parents=True, exist_ok=True)
    try:
        # Ensure the YAML at least declares a name; if missing, prepend
        # one so loaders downstream can identify it.
        if not re.search(r"^name\s*:", body, re.MULTILINE):
            body = f"name: {name}\n" + body
        if proposal.description and not re.search(r"^description\s*:", body, re.MULTILINE):
            body = re.sub(
                r"^name\s*:.*$",
                lambda m: m.group(0) + f"\ndescription: {proposal.description}",
                body,
                count=1,
                flags=re.MULTILINE,
            )
        target.write_text(body.rstrip() + "\n", encoding="utf-8")
    except Exception as exc:
        log_exception("orchestration.casting.write_workflow", exc)
        return False, f"write_error:{exc}"
    _log_casting_event(paths, {
        "event": "workflow_written",
        "agent": source_agent,
        "name": name,
        "description": proposal.description,
        "yaml_length": len(body),
        "yaml": body,
    })
    if on_change is not None:
        try:
            on_change(name, "workflow_written")
        except Exception:
            pass
    return True, "written"


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


def render_roster_summary(paths: Paths) -> str:
    """Markdown bullet list of the current roster (for prompt context)."""
    agents = (load_agents_config(paths).get("agents") or {})
    if not agents:
        return "(no agents registered yet)"
    lines: List[str] = []
    for name in sorted(agents.keys()):
        cfg = agents[name] or {}
        marker = "[baseline]" if is_baseline(name) else "[dynamic]"
        role = (cfg.get("role") or "").strip()
        prov = cfg.get("provider") or "(default)"
        caps = cfg.get("capabilities") or []
        caps_str = ",".join(caps) if caps else "default"
        prompt_file = cfg.get("prompt") or f"{name}.md"
        prompt_preview = ""
        try:
            prompt_path = paths.prompts / prompt_file
            if prompt_path.exists():
                prompt_preview = " ".join(prompt_path.read_text(encoding="utf-8").strip().split())[:220]
        except Exception as exc:
            log_exception(f"orchestration.casting.render_roster_summary.{name}", exc)
        lines.append(
            f"- {marker} {name} :: {role} "
            f"(provider={prov}; capabilities={caps_str}; prompt={prompt_file}; prompt_preview={prompt_preview or '(missing)'})"
        )
    return "\n".join(lines)


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
