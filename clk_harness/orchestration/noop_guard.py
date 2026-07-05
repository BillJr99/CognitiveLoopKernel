"""No-op detection: stop agents from "describing work instead of doing it".

The most common reliability failure: a producing agent emits three paragraphs of
prose (and maybe ``PROGRESS: yes``) but no ``ACTION:write``/``edit`` — so nothing
changes and the loop advances anyway. ``response_quality.score`` never caught this
because it has no notion of "this stage was *expected* to mutate files".

This module supplies that notion plus an escalating repair preamble the dispatch
loop prepends when a producing stage applied zero file mutations.
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Sequence

# Roles that are legitimately prose / verdict only.
_NON_PRODUCING = {"chief", "qa", "critic"}
_DEFAULT_PRODUCING = ("engineer", "ralph")


def is_mutation_expected(
    agent: str,
    *,
    outputs: Optional[Sequence[str]] = None,
    commit: bool = False,
    cfg: Optional[Dict[str, Any]] = None,
) -> bool:
    """Whether a stage run by ``agent`` should have changed files on disk.

    True when: the agent is in the configured producing set, OR the stage
    declared a non-empty ``outputs`` contract (and that is treated as
    producing), OR the stage is set to ``commit`` and the agent is not a
    known prose-only role.
    """
    g = (cfg or {}).get("noop_guard") or {}
    if not g.get("enabled", True):
        return False
    producing = {str(a).lower() for a in (g.get("producing_agents") or _DEFAULT_PRODUCING)}
    agent_l = (agent or "").lower()

    if agent_l in producing:
        return True
    if outputs and g.get("treat_outputs_stage_as_producing", True):
        return True
    if commit and agent_l not in _NON_PRODUCING:
        return True
    return False


def max_redispatch(cfg: Optional[Dict[str, Any]] = None) -> int:
    g = (cfg or {}).get("noop_guard") or {}
    if not g.get("enabled", True):
        return 0
    return int(g.get("max_redispatch", 2) or 0)


def repair_preamble(attempt: int, *, target: str = "") -> str:
    """Escalating instruction prepended when a producing stage made no change.

    ``attempt`` is 1-based (the n-th re-dispatch). ``target`` optionally names
    the file/area the stage implies, used in the worked example.
    """
    what = target or "the file(s) this objective requires"
    if attempt <= 1:
        return (
            "Your previous response changed NO files — it only described work. "
            f"Descriptions do nothing here. Emit ACTION blocks NOW that create or "
            f"edit {what}. Every ACTION must be a real file mutation:\n"
            "  ACTION: write\n"
            "  PATH: <path>\n"
            "  CONTENT:\n"
            "  <file contents>\n"
            "  END_ACTION"
        )
    if attempt == 2:
        path_hint = target or "src/<module>.py"
        return (
            "Still no files changed. This is serious. Output at least one "
            "concrete ACTION:write or ACTION:edit block with real content — not a "
            "plan, not a description. Worked example (adapt the path/content to "
            "this objective):\n"
            "  ACTION: write\n"
            f"  PATH: {path_hint}\n"
            "  CONTENT:\n"
            "  # real, working code or content here\n"
            "  END_ACTION\n"
            "Do this for every deliverable the objective implies."
        )
    return (
        "FINAL ATTEMPT. Your response MUST consist of ACTION blocks only. Output "
        "no prose, no preamble, no explanation — just the ACTION:write/edit blocks "
        f"that produce {what}. If you cannot, emit a POST: question TO: chief "
        "explaining the exact blocker instead."
    )
