"""Intra- and inter-agent deliberation — "the team thinks".

Two cheap mechanisms already exist in the harness:

* inter-agent: directed blackboard Q&A (``POST: question TO: <peer>
  URGENCY: blocking``), routed inline by the AgentRunner.
* intra-agent / debate: the critic<->worker refine loop in the WorkflowRunner.

This module makes them first-class and default-on within missions:

* a self-reflection preamble injected into producing dispatches ("restate the
  goal, list approaches, pick one, then act");
* an invitation to raise blocking questions when stuck;
* a phase-gate guard: a phase cannot ``pass`` while a blocking question for it
  is still unanswered.

Everything is config-gated under ``deliberation.*`` with a single kill switch.
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional

from ..config import Paths
from . import blackboard as _blackboard


def _cfg(clk_cfg: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return (clk_cfg or {}).get("deliberation") or {}


def enabled(clk_cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(_cfg(clk_cfg).get("enabled", True))


def min_debate_rounds(clk_cfg: Optional[Dict[str, Any]]) -> int:
    if not enabled(clk_cfg):
        return 0
    return int(_cfg(clk_cfg).get("min_debate_rounds", 1) or 0)


def require_open_questions_resolved(clk_cfg: Optional[Dict[str, Any]]) -> bool:
    return bool(enabled(clk_cfg) and _cfg(clk_cfg).get("require_open_questions_resolved", True))


_SELF_REFLECT = (
    "Think before you act: (1) restate the objective in one line; (2) list 2-3 "
    "viable approaches; (3) pick one and say why in a sentence; (4) THEN emit the "
    "ACTION blocks that implement it. Do not skip straight to prose."
)

_ENCOURAGE_QUESTIONS = (
    "If a decision is genuinely blocked on information another agent owns, ask it "
    "directly: emit `POST: question TO: <peer> URGENCY: blocking` with a focused "
    "question. The harness routes it and feeds you the answer. Do not guess on "
    "load-bearing unknowns; do not invent questions you can answer yourself."
)


def dispatch_preamble(clk_cfg: Optional[Dict[str, Any]]) -> str:
    """Preamble injected ahead of producing dispatches in a mission."""
    if not enabled(clk_cfg):
        return ""
    parts: List[str] = []
    c = _cfg(clk_cfg)
    if c.get("self_reflect_preamble", True):
        parts.append(_SELF_REFLECT)
    if c.get("encourage_questions", True):
        parts.append(_ENCOURAGE_QUESTIONS)
    return ("\n\n".join(parts) + "\n\n") if parts else ""


def unresolved_blocking_questions(paths: Paths) -> List["_blackboard.Post"]:
    """Blocking questions on the board that have no matching answer yet."""
    try:
        unanswered = _blackboard.find_unanswered_questions(paths)
    except Exception:
        return []
    return [
        q for q in unanswered
        if (q.urgency or "blocking").lower() == "blocking"
    ]
