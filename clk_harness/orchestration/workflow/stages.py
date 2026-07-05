"""Workflow data model and parsing (Archon-style YAML).

A workflow file looks like:

    name: engineering
    description: Single development cycle.
    stages:
      - id: decompose
        agent: chief
        objective: Decompose the current top-level objective.
      - id: research
        agent: researcher
        objective: Investigate open assumptions.
        depends_on: [decompose]
        validation: "echo OK"
        commit: true
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ...log import get_logger, log_exception
from ..agent import AgentRun

logger = get_logger(__name__)

_ROUND_STATUS_RE = re.compile(r"^\s*ROUND_STATUS\s*:\s*(continue|done|finished)\s*$", re.IGNORECASE | re.MULTILINE)


def _round_status(text: str) -> str:
    """Return 'continue' or 'done'. Default 'done' when no marker found."""
    if not text:
        return "done"
    matches = _ROUND_STATUS_RE.findall(text)
    if not matches:
        return "done"
    last = matches[-1].lower()
    return "continue" if last == "continue" else "done"


try:
    import yaml  # type: ignore
except Exception as _exc:
    # PyYAML is optional. The mini-YAML loader below covers the workflow
    # subset CLK uses, so we quietly fall back rather than spraying a
    # warning across stderr (which would also corrupt the TUI).
    logger.debug("PyYAML unavailable; using the built-in mini-YAML loader: %s", _exc)
    yaml = None


def _mini_yaml_loads(text: str) -> Dict[str, Any]:
    """Minimal YAML loader for the workflow subset used by CLK.

    Supports:
      * top-level scalar keys (``key: value``)
      * a single ``stages:`` key whose value is a list of dicts
      * each list item begins with ``- key: value`` then ``key: value`` lines
      * inline lists like ``[a, b]`` and booleans (``true``/``false``)
      * quoted scalar values

    This is *not* a general YAML parser - it handles exactly what the
    bundled workflows use. Keeping it local avoids a hard dependency on
    PyYAML when ``ensurepip`` is unavailable.
    """

    def parse_scalar(raw: str) -> Any:
        s = raw.strip()
        if not s:
            return ""
        if (s.startswith('"') and s.endswith('"')) or (s.startswith("'") and s.endswith("'")):
            return s[1:-1]
        if s.startswith("[") and s.endswith("]"):
            inner = s[1:-1].strip()
            if not inner:
                return []
            return [parse_scalar(p) for p in _split_csv(inner)]
        low = s.lower()
        if low == "true":
            return True
        if low == "false":
            return False
        if low in ("null", "~"):
            return None
        try:
            if "." in s:
                return float(s)
            return int(s)
        except ValueError:
            return s

    def _split_csv(s: str) -> List[str]:
        out: List[str] = []
        buf = ""
        depth = 0
        in_quote: Optional[str] = None
        for ch in s:
            if in_quote:
                buf += ch
                if ch == in_quote:
                    in_quote = None
                continue
            if ch in ("'", '"'):
                in_quote = ch
                buf += ch
                continue
            if ch == "[":
                depth += 1
                buf += ch
                continue
            if ch == "]":
                depth -= 1
                buf += ch
                continue
            if ch == "," and depth == 0:
                out.append(buf.strip())
                buf = ""
                continue
            buf += ch
        if buf.strip():
            out.append(buf.strip())
        return out

    def continuation(start: int, base_indent: int) -> Tuple[str, int]:
        parts: List[str] = []
        j = start
        while j < len(lines):
            line = lines[j]
            stripped = line.lstrip()
            indent = len(line) - len(stripped)
            if indent <= base_indent:
                break
            if stripped.startswith("- "):
                break
            if ":" in stripped:
                break
            parts.append(stripped)
            j += 1
        return " ".join(parts).strip(), j

    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip() and not ln.lstrip().startswith("#")]
    result: Dict[str, Any] = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith(" "):  # unexpected at top level, skip
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, val = line.partition(":")
        key = key.strip()
        val = val.strip()
        if val:
            result[key] = parse_scalar(val)
            i += 1
            extra, ni = continuation(i, 0)
            if extra and isinstance(result[key], str):
                result[key] = f"{result[key]} {extra}".strip()
                i = ni
            continue
        # value on subsequent indented lines
        i += 1
        if i < len(lines) and lines[i].lstrip().startswith("- "):
            items: List[Any] = []
            cur: Optional[Dict[str, Any]] = None
            while i < len(lines) and lines[i].startswith(" "):
                sub = lines[i]
                stripped = sub.lstrip()
                if stripped.startswith("- "):
                    if cur is not None:
                        items.append(cur)
                    cur = {}
                    rest = stripped[2:]
                    if ":" in rest:
                        k2, _, v2 = rest.partition(":")
                        k2 = k2.strip()
                        cur[k2] = parse_scalar(v2)
                        i += 1
                        extra, ni = continuation(i, len(sub) - len(stripped))
                        if extra and isinstance(cur[k2], str):
                            cur[k2] = f"{cur[k2]} {extra}".strip()
                            i = ni
                        continue
                else:
                    if cur is None:
                        cur = {}
                    if ":" in stripped:
                        k2, _, v2 = stripped.partition(":")
                        k2 = k2.strip()
                        cur[k2] = parse_scalar(v2)
                        i += 1
                        extra, ni = continuation(i, len(sub) - len(stripped))
                        if extra and isinstance(cur[k2], str):
                            cur[k2] = f"{cur[k2]} {extra}".strip()
                            i = ni
                        continue
                i += 1
            if cur is not None:
                items.append(cur)
            result[key] = items
        else:
            # nested mapping not used by our workflow format; collect raw
            buf: List[str] = []
            while i < len(lines) and lines[i].startswith(" "):
                buf.append(lines[i])
                i += 1
            result[key] = "\n".join(buf)
    return result


@dataclass
class WorkflowStage:
    id: str
    agent: str
    objective: str
    depends_on: List[str] = field(default_factory=list)
    validation: Optional[str] = None
    commit: bool = True
    # Blackboard contract: ``inputs`` selectors filter the blackboard
    # digest spliced into the worker's prompt; ``outputs`` are contract
    # keys the worker promises to satisfy via POST blocks. Missing
    # outputs surface as a warning (not a hard failure) post-run so
    # downstream stages can still attempt their work.
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
    # Phase tag. ``review`` makes this a chief-review stage that auto-
    # digests upstream post-output and asks the chief to decide
    # CONTINUE / REDIRECT / ABORT. Unknown phases are passed through to
    # the worker prompt as-is for downstream behaviors to interpret.
    phase: str = ""
    # When > 1, the stage runs in turn-based rounds: after each round
    # the runner refreshes the worker's prompt with any new blackboard
    # posts (including those from sibling parallel workers) and re-
    # dispatches. The worker stops the loop early by emitting
    # ``ROUND_STATUS: done`` (default), or requests another round with
    # ``ROUND_STATUS: continue``.
    rounds: int = 1
    # Tag for sensitive stages. When true, the runner runs an extra
    # chief checkpoint after the stage completes (CONTINUE / REDIRECT /
    # ABORT) AND uses meta-prompt drafting on dispatch when configured.
    careful: bool = False
    # Critic-judge inner refinement loop. When present, after the
    # worker's first response the harness dispatches the named critic
    # agent to score the response 0..1; if below
    # ``accept_threshold`` the worker is re-dispatched with the critic's
    # feedback, up to ``max_rounds`` total worker dispatches. ``None``
    # means "use the default policy from clk.config.json::robustness".
    refine: Optional[Dict[str, Any]] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Workflow:
    name: str
    description: str
    stages: List[WorkflowStage]


def load_workflow(path: Path) -> Workflow:
    text = path.read_text(encoding="utf-8")
    if yaml is not None:
        try:
            data = yaml.safe_load(text) or {}
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.pyyaml", exc)
            try:
                data = _mini_yaml_loads(text)
            except Exception:
                data = {}
    else:
        try:
            data = _mini_yaml_loads(text)
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.fallback", exc)
            raise

    # If parsing produced nothing usable (e.g. the chief wrote a
    # malformed workflow that wedges every subsequent supervise cycle),
    # restore the bundled template for this workflow name so the runner
    # has stages to execute on the next pass.
    if not isinstance(data, dict) or not data.get("stages"):
        try:
            from ...templates.workflows import WORKFLOWS as _BUNDLED_WORKFLOWS
        except Exception:
            _BUNDLED_WORKFLOWS = {}
        fallback = _BUNDLED_WORKFLOWS.get(path.name)
        if fallback:
            try:
                path.write_text(fallback, encoding="utf-8")
            except Exception as exc:
                log_exception("orchestration.workflow.load_workflow.restore", exc)
            if yaml is not None:
                try:
                    data = yaml.safe_load(fallback) or {}
                except Exception:
                    data = _mini_yaml_loads(fallback)
            else:
                data = _mini_yaml_loads(fallback)

    stages: List[WorkflowStage] = []
    for raw in data.get("stages") or []:
        try:
            rounds = int(raw.get("rounds") or 1)
        except (TypeError, ValueError):
            rounds = 1
        refine_raw = raw.get("refine")
        if isinstance(refine_raw, dict):
            refine_cfg: Optional[Dict[str, Any]] = dict(refine_raw)
        elif refine_raw in (True, "true", "yes", 1):
            refine_cfg = {}
        else:
            refine_cfg = None
        stages.append(
            WorkflowStage(
                id=str(raw.get("id") or raw.get("agent") or "stage"),
                agent=str(raw.get("agent") or "engineer"),
                objective=str(raw.get("objective") or ""),
                depends_on=list(raw.get("depends_on") or []),
                validation=raw.get("validation"),
                commit=bool(raw.get("commit", True)),
                inputs=list(raw.get("inputs") or []),
                outputs=list(raw.get("outputs") or []),
                phase=str(raw.get("phase") or "").strip().lower(),
                rounds=max(1, rounds),
                careful=bool(raw.get("careful") or False),
                refine=refine_cfg,
                metadata=dict(raw.get("metadata") or {}),
            )
        )
    return Workflow(
        name=str(data.get("name") or path.stem),
        description=str(data.get("description") or ""),
        stages=stages,
    )


@dataclass
class StageResult:
    stage: WorkflowStage
    run: AgentRun
    validated: bool
    validation_output: str = ""
    committed: bool = False
    failure_reason: str = ""  # filled when ok=False or validated=False
