"""Mission charter — the chief's up-front commitment, authored before the plan.

A mission starts by asking the chief to write a *charter*: the mission
statement, scope, explicit non-goals, success criteria, and constraints. The
living plan and the done-gate are derived from it, so "done" is measured against
something the chief committed to at the outset rather than drifting.

Persisted as ``.clk/state/charter.json`` (machine) + ``.clk/state/CHARTER.md``
(human mirror), and trace-committed so the git log records the commitment.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional

from ..config import Paths, load_json, save_json
from ..git_ops import commit_trace
from ..utils.activity_log import log_event
from ..utils.logging_utils import log_exception
from . import blackboard as _blackboard
from . import casting as _casting

# Filename tokens that make a success criterion machine-checkable as "exists".
_FILE_EXT_RE = re.compile(
    r"\b([\w./-]+\.(?:py|md|json|txt|ya?ml|js|ts|tsx|html|css|sh|toml|cfg|ini|rs|go|java|rb))\b"
)


@dataclass
class Charter:
    objective: str = ""
    mission_statement: str = ""
    scope: List[str] = field(default_factory=list)
    non_goals: List[str] = field(default_factory=list)
    success_criteria: List[str] = field(default_factory=list)
    constraints: List[str] = field(default_factory=list)
    assumptions: List[str] = field(default_factory=list)
    created_at: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "objective": self.objective,
            "mission_statement": self.mission_statement,
            "scope": list(self.scope),
            "non_goals": list(self.non_goals),
            "success_criteria": list(self.success_criteria),
            "constraints": list(self.constraints),
            "assumptions": list(self.assumptions),
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any]) -> "Charter":
        raw = raw or {}
        return cls(
            objective=str(raw.get("objective") or ""),
            mission_statement=str(raw.get("mission_statement") or ""),
            scope=[str(x) for x in (raw.get("scope") or [])],
            non_goals=[str(x) for x in (raw.get("non_goals") or [])],
            success_criteria=[str(x) for x in (raw.get("success_criteria") or [])],
            constraints=[str(x) for x in (raw.get("constraints") or [])],
            assumptions=[str(x) for x in (raw.get("assumptions") or [])],
            created_at=str(raw.get("created_at") or ""),
        )

    def render_md(self) -> str:
        def _bullets(items: List[str]) -> str:
            return "\n".join(f"- {i}" for i in items) or "- (none)"
        return (
            "# Mission Charter\n\n"
            f"**Objective:** {self.objective or '(none)'}\n\n"
            f"**Mission:** {self.mission_statement or '(none)'}\n\n"
            f"## Scope\n{_bullets(self.scope)}\n\n"
            f"## Non-goals\n{_bullets(self.non_goals)}\n\n"
            f"## Success criteria\n{_bullets(self.success_criteria)}\n\n"
            f"## Constraints\n{_bullets(self.constraints)}\n\n"
            f"## Assumptions\n{_bullets(self.assumptions)}\n\n"
            f"_Authored {self.created_at}_\n"
        )


def charter_json_path(paths: Paths):
    return paths.state / "charter.json"


def charter_md_path(paths: Paths):
    return paths.state / "CHARTER.md"


def load_charter(paths: Paths) -> Optional[Charter]:
    p = charter_json_path(paths)
    if not p.exists():
        return None
    raw = load_json(p, {})
    if not raw:
        return None
    return Charter.from_dict(raw)


def save_charter(paths: Paths, charter: Charter) -> None:
    paths.state.mkdir(parents=True, exist_ok=True)
    save_json(charter_json_path(paths), charter.to_dict())
    try:
        charter_md_path(paths).write_text(charter.render_md(), encoding="utf-8")
    except Exception as exc:
        log_exception("orchestration.charter.save_charter.md", exc)


def derive_done_criteria(charter: Optional[Charter]) -> List[Dict[str, Any]]:
    """Turn the charter's success criteria into machine-checkable checks.

    Conservative: only file-exists criteria are auto-extracted (running
    arbitrary commands parsed from prose would be unsafe). A criterion that
    names a file (``report.md``, ``src/app.py``) becomes a ``file`` check;
    everything else is left for the chief's phase-gate judgment.
    """
    out: List[Dict[str, Any]] = []
    if charter is None:
        return out
    for crit in charter.success_criteria:
        m = _FILE_EXT_RE.search(crit or "")
        if m:
            out.append({"label": crit.strip()[:80], "type": "file", "value": m.group(1)})
    return out


def charter_objective(objective: str) -> str:
    return (
        "MISSION CHARTER MODE. You are kicking off an autonomous mission with no "
        "further human input. Before any work begins, author the charter that the "
        "whole mission will be judged against.\n\n"
        f"Mission objective:\n{objective}\n\n"
        "Emit exactly one PROPOSE_CHARTER block (machine-parsed):\n\n"
        "  PROPOSE_CHARTER\n"
        "  MISSION: <one-sentence statement of what success delivers>\n"
        "  SCOPE: <item; item; item>\n"
        "  NON_GOALS: <item; item>\n"
        "  SUCCESS: <criterion; criterion; criterion>\n"
        "  CONSTRAINTS: <item; item>\n"
        "  ASSUMPTIONS: <item; item>\n"
        "  END_CHARTER\n\n"
        "Make SUCCESS criteria concrete and verifiable. Where a criterion implies a "
        "file, name the file (e.g. 'README.md documents setup', 'tests/ pass') — the "
        "harness enforces file-named criteria mechanically. Keep items short; "
        "separate them with semicolons."
    )


def _fallback_charter(objective: str) -> Charter:
    return Charter(
        objective=objective,
        mission_statement=objective,
        scope=["Deliver a working solution for the objective"],
        non_goals=[],
        success_criteria=[
            "A runnable deliverable exists",
            "Tests or a smoke check pass",
            "README documents setup",
        ],
        constraints=[],
        assumptions=[],
        created_at=datetime.now().isoformat(timespec="seconds"),
    )


def bootstrap_charter(paths: Paths, runner, objective: str, *, dry_run: bool = False) -> Charter:
    """Dispatch the chief in CHARTER mode, persist + commit the result.

    Falls back to a sensible default charter when parsing fails or on a
    dry run.
    """
    charter: Optional[Charter] = None
    if not dry_run:
        try:
            run = runner.run(
                "chief",
                charter_objective(objective),
                extra={"phase": "charter"},
                dry_run=dry_run,
            )
            prop = _casting.parse_charter_proposal(run.response.text or "")
            if prop is not None:
                charter = Charter(
                    objective=objective,
                    mission_statement=prop.mission,
                    scope=prop.scope,
                    non_goals=prop.non_goals,
                    success_criteria=prop.success,
                    constraints=prop.constraints,
                    assumptions=prop.assumptions,
                    created_at=datetime.now().isoformat(timespec="seconds"),
                )
        except Exception as exc:
            log_exception("orchestration.charter.bootstrap_charter", exc)
    if charter is None:
        charter = _fallback_charter(objective)

    save_charter(paths, charter)
    log_event(
        paths,
        "mission_charter",
        objective=objective,
        mission=charter.mission_statement,
        success_count=len(charter.success_criteria),
        scope_count=len(charter.scope),
    )
    try:
        _blackboard.post(
            paths,
            author="mission",
            body=charter.render_md(),
            post_type="charter",
            produces=["mission_charter"],
            slug_hint="charter",
        )
    except Exception as exc:
        log_exception("orchestration.charter.bootstrap_charter.post", exc)
    try:
        commit_trace(
            paths.root,
            kind="charter",
            summary=(charter.mission_statement or objective)[:80],
            meta={"success_criteria": len(charter.success_criteria), "scope": len(charter.scope)},
        )
    except Exception as exc:
        log_exception("orchestration.charter.bootstrap_charter.commit", exc)
    return charter
