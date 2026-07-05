"""Machine-checkable completion gate (the enforced "high bar to stop").

Today CLK's done-checklist lives only in the chief's prose prompt: the harness
writes ``done.md`` the instant any agent emits ``ACTION:done`` and never verifies
anything. That is why autonomous runs stop prematurely. This module turns the
checklist into code: ``ACTION:done`` / ``done.md`` become a *request*, and the
mission/supervise loop only grants it (writing ``done_granted.md``) when
:func:`evaluate_done_gate` reports every enabled criterion satisfied.

All signals are things the system already produces:

* tests green   -> from the cycle's :class:`~clk_harness.orchestration.evaluator.EvalResult`
* deliverables  -> real files exist under the project root
* qa pass       -> a ``POST: qa`` blackboard entry whose body says PASS
* ralph pass    -> ``.clk/state/experiments.jsonl`` has an entry (a refinement ran)
* no TODOs      -> changed files contain no TODO/FIXME/placeholder markers (opt-in)
* charter       -> the chief's own machine-checkable success criteria

The gate is *adaptive*: when the evaluator reports a ``weak`` gate (no real test
command could be derived — docs / research / content projects), the tests-green
requirement is relaxed so the mission can still finish.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..log import get_logger, log_exception
from . import blackboard as _blackboard

logger = get_logger(__name__)

_EXCLUDE_TOP = {".clk", ".git", "node_modules", "__pycache__", ".pytest_cache", "venv", ".venv"}
_STATEISH_ROOT_FILES = {"PROGRESS.md", "DECISIONS.md", "MISSION.md", "CHARTER.md"}
_TODO_RE = re.compile(r"\b(TODO|FIXME|XXX|HACK|PLACEHOLDER)\b", re.IGNORECASE)
_QA_PASS_RE = re.compile(r"\bPASS(ED)?\b", re.IGNORECASE)
_QA_FAIL_RE = re.compile(r"\bFAIL(ED|URE)?\b", re.IGNORECASE)


@dataclass
class DoneGateVerdict:
    passed: bool = False
    failures: List[str] = field(default_factory=list)
    checked: Dict[str, Any] = field(default_factory=dict)
    reasons: List[str] = field(default_factory=list)

    def summary(self) -> str:
        if self.passed:
            return "done-gate PASS — every enabled completion criterion satisfied"
        return "done-gate REJECT — unmet: " + (", ".join(self.failures) or "?")

    def repair_hint(self) -> str:
        """Preamble quoting unmet criteria back to the chief on re-dispatch."""
        if self.passed or not self.reasons:
            return ""
        bullets = "\n".join(f"- {r}" for r in self.reasons)
        return (
            "The harness REJECTED your request to finish (ACTION:done). The "
            "following completion criteria are not yet satisfied:\n"
            f"{bullets}\n"
            "Do NOT emit ACTION:done again until these pass. Keep working: "
            "dispatch the work that closes each gap."
        )


# --- individual signal helpers --------------------------------------------


def deliverable_files(root: Path, *, limit: int = 5000) -> List[str]:
    """Real product files under ``root`` (excludes harness/state/vcs noise)."""
    root = Path(root)
    out: List[str] = []
    try:
        for p in root.rglob("*"):
            if not p.is_file():
                continue
            try:
                rel = p.relative_to(root)
            except ValueError:
                continue
            parts = rel.parts
            if parts and parts[0] in _EXCLUDE_TOP:
                continue
            if len(parts) == 1 and parts[0] in _STATEISH_ROOT_FILES:
                continue
            out.append(str(rel))
            if len(out) >= limit:
                break
    except Exception as exc:
        log_exception("orchestration.done_gate.deliverable_files", exc)
    return out


def has_qa_pass(paths: Paths) -> bool:
    """True when the most recent ``POST: qa`` entry reads as a PASS."""
    try:
        qa_posts = [p for p in _blackboard.list_posts(paths) if (p.post_type or "").lower() == "qa"]
    except Exception as exc:
        log_exception("orchestration.done_gate.has_qa_pass", exc)
        return False
    if not qa_posts:
        return False
    body = qa_posts[-1].body or ""
    return bool(_QA_PASS_RE.search(body)) and not _QA_FAIL_RE.search(body)


def has_ralph_pass(paths: Paths) -> bool:
    """True when at least one refinement iteration has been recorded."""
    exp = paths.state / "experiments.jsonl"
    try:
        if exp.exists() and exp.stat().st_size > 0:
            return any(line.strip() for line in exp.read_text(encoding="utf-8").splitlines())
    except Exception as exc:
        log_exception("orchestration.done_gate.has_ralph_pass.exp", exc)
    # Fallback: a ralph dispatch in the activity log.
    activity = paths.logs / "activity.jsonl"
    try:
        if activity.exists():
            for line in activity.read_text(encoding="utf-8").splitlines():
                if '"ralph"' in line and "dispatch" in line:
                    return True
    except Exception as exc:
        log_exception("orchestration.done_gate.has_ralph_pass.activity", exc)
    return False


def todo_markers(root: Path, *, max_files: int = 2000) -> List[str]:
    """Deliverable files that still contain TODO/FIXME/placeholder markers."""
    hits: List[str] = []
    for rel in deliverable_files(root, limit=max_files):
        fp = Path(root) / rel
        try:
            if fp.suffix.lower() in {".png", ".jpg", ".jpeg", ".gif", ".pdf", ".zip", ".bin"}:
                continue
            text = fp.read_text(encoding="utf-8", errors="ignore")
        except Exception as _exc:
            logger.debug("done-gate: skipping unreadable %s: %s", rel, _exc)
            continue
        if _TODO_RE.search(text):
            hits.append(rel)
    return hits


def _check_extra_criterion(root: Path, crit: Dict[str, Any]) -> bool:
    """Evaluate one charter-derived machine-checkable success criterion."""
    ctype = (crit.get("type") or "").lower()
    value = crit.get("value") or ""
    try:
        if ctype == "file":
            return (Path(root) / value).exists()
        if ctype == "command" and value:
            r = subprocess.run(
                value, shell=True, cwd=str(root),
                capture_output=True, text=True, timeout=180,
            )
            return r.returncode == 0
    except Exception as exc:
        log_exception("orchestration.done_gate._check_extra_criterion", exc)
    return False


# --- top-level gate --------------------------------------------------------


def evaluate_done_gate(
    paths: Paths,
    clk_cfg: Optional[Dict[str, Any]],
    eval_result: Any = None,
    *,
    extra_criteria: Optional[List[Dict[str, Any]]] = None,
) -> DoneGateVerdict:
    """Return a verdict on whether the mission is allowed to stop.

    ``extra_criteria`` are charter-derived machine-checkable checks, each a
    dict ``{"label":..., "type":"file"|"command", "value":...}``.
    """
    cfg = (clk_cfg or {}).get("done_gate") or {}
    verdict = DoneGateVerdict()

    if not cfg.get("enabled", True):
        verdict.passed = True
        verdict.reasons.append("done-gate disabled (done_gate.enabled=false)")
        return verdict

    failures: List[str] = []
    reasons: List[str] = []
    checked: Dict[str, Any] = {}

    # 1. tests green (adaptive: relax on a weak gate)
    if cfg.get("require_tests_green", True):
        weak = bool(getattr(eval_result, "weak", False))
        ok = bool(getattr(eval_result, "ok", False))
        if eval_result is None:
            checked["tests_green"] = "skipped (no eval result)"
        elif weak:
            checked["tests_green"] = "relaxed (weak gate — no real test command)"
        elif not ok:
            failures.append("tests_red")
            reasons.append("Tests are not green. Make the validation command exit 0.")
            checked["tests_green"] = False
        else:
            checked["tests_green"] = True

    # 2. deliverables exist
    if cfg.get("require_deliverables", True):
        min_n = int(cfg.get("min_deliverable_files", 1) or 1)
        files = deliverable_files(paths.root)
        checked["deliverable_files"] = len(files)
        if len(files) < min_n:
            failures.append("no_deliverables")
            reasons.append(
                f"Only {len(files)} product file(s) on disk (need >= {min_n}). "
                "Emit ACTION:write blocks that create real deliverables."
            )

    # 3. qa pass on the blackboard
    if cfg.get("require_qa_pass", True):
        ok = has_qa_pass(paths)
        checked["qa_pass"] = ok
        if not ok:
            failures.append("no_qa_pass")
            reasons.append(
                "No QA PASS on the blackboard. Dispatch qa and have it emit a "
                "`POST: qa` block whose body states PASS."
            )

    # 4. a ralph refinement pass occurred
    if cfg.get("require_ralph_pass", True):
        ok = has_ralph_pass(paths)
        checked["ralph_pass"] = ok
        if not ok:
            failures.append("no_ralph_pass")
            reasons.append(
                "No refinement pass recorded. Run at least one ralph refine "
                "iteration before finishing."
            )

    # 5. no TODO / placeholder markers (opt-in)
    if cfg.get("forbid_todo_markers", False):
        hits = todo_markers(paths.root)
        checked["todo_markers"] = len(hits)
        if hits:
            failures.append("todo_markers")
            sample = ", ".join(hits[:5])
            reasons.append(f"TODO/placeholder markers remain in: {sample}.")

    # 6. charter-derived machine-checkable success criteria
    for crit in (extra_criteria or []):
        label = crit.get("label") or crit.get("value") or "charter criterion"
        ok = _check_extra_criterion(paths.root, crit)
        checked[f"charter:{label}"] = ok
        if not ok:
            failures.append(f"charter:{label}")
            reasons.append(f"Charter success criterion not met: {label}.")

    verdict.failures = failures
    verdict.reasons = reasons
    verdict.checked = checked
    verdict.passed = not failures
    return verdict
