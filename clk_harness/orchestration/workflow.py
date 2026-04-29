"""Workflow parser and runner (Archon-style YAML).

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

import shlex
import subprocess
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import Paths
from ..git_ops import add_all, commit as git_commit, has_changes
from ..utils.logging_utils import log, log_exception
from .agent import AgentRunner, AgentRun


try:
    import yaml  # type: ignore
except Exception as _yaml_exc:
    yaml = None
    print(
        f"[orchestration.workflow] PyYAML not available; using minimal YAML fallback ({_yaml_exc})",
        file=sys.stderr,
    )


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

    lines = [l.rstrip() for l in text.splitlines() if l.strip() and not l.lstrip().startswith("#")]
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
                        cur[k2.strip()] = parse_scalar(v2)
                else:
                    if cur is None:
                        cur = {}
                    if ":" in stripped:
                        k2, _, v2 = stripped.partition(":")
                        cur[k2.strip()] = parse_scalar(v2)
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
    inputs: List[str] = field(default_factory=list)
    outputs: List[str] = field(default_factory=list)
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
            data = _mini_yaml_loads(text)
    else:
        try:
            data = _mini_yaml_loads(text)
        except Exception as exc:
            log_exception("orchestration.workflow.load_workflow.fallback", exc)
            raise

    stages: List[WorkflowStage] = []
    for raw in data.get("stages") or []:
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


class WorkflowRunner:
    def __init__(self, paths: Paths, runner: AgentRunner) -> None:
        self.paths = paths
        self.runner = runner

    def run(self, workflow: Workflow, *, dry_run: Optional[bool] = None) -> List[StageResult]:
        log(f"workflow start: {workflow.name} ({len(workflow.stages)} stages)")
        results: List[StageResult] = []
        completed: Dict[str, bool] = {}
        for stage in workflow.stages:
            if any(dep not in completed or not completed[dep] for dep in stage.depends_on):
                log(f"stage {stage.id} skipped: unmet deps {stage.depends_on}", level="WARN")
                completed[stage.id] = False
                continue
            log(f"stage {stage.id} -> agent {stage.agent}")
            run = self.runner.run(
                stage.agent,
                stage.objective,
                extra={"stage_id": stage.id, "workflow": workflow.name},
                dry_run=dry_run,
            )
            ok = run.response.ok
            if dry_run:
                v_ok, v_out = True, "(dry-run: validation skipped)"
            else:
                v_ok, v_out = self._validate(stage)
            committed = False
            if ok and v_ok and stage.commit and not dry_run:
                committed = self._commit(workflow, stage, run, v_out)
            results.append(
                StageResult(
                    stage=stage,
                    run=run,
                    validated=v_ok,
                    validation_output=v_out,
                    committed=committed,
                )
            )
            completed[stage.id] = ok and v_ok
        log(f"workflow done: {workflow.name}")
        return results

    def _validate(self, stage: WorkflowStage) -> tuple[bool, str]:
        if not stage.validation:
            return True, ""
        try:
            r = subprocess.run(
                stage.validation,
                shell=True,
                cwd=str(self.paths.root),
                capture_output=True,
                text=True,
                timeout=120,
            )
            output = (r.stdout or "") + (r.stderr or "")
            return r.returncode == 0, output.strip()
        except Exception as exc:
            log_exception("orchestration.workflow._validate", exc)
            return False, str(exc)

    def _commit(
        self,
        workflow: Workflow,
        stage: WorkflowStage,
        run: AgentRun,
        validation_output: str,
    ) -> bool:
        if not has_changes(self.paths.root):
            return False
        if not add_all(self.paths.root):
            return False
        return git_commit(
            self.paths.root,
            agent=f"{workflow.name}.{stage.id}",
            objective=stage.objective,
            files_changed=run.files_written,
            validation=stage.validation or "none",
            next_step=f"continue workflow {workflow.name}",
            body_extra=(validation_output or "")[:500],
        )
