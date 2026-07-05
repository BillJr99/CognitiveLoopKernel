"""Workflow parser and runner (Archon-style YAML).

Decomposed package; this ``__init__`` preserves the public surface of the
former ``clk_harness/orchestration/workflow.py`` module so existing
imports keep working:

* :mod:`.stages` — data model + YAML parsing (``Workflow``,
  ``WorkflowStage``, ``StageResult``, ``load_workflow``).
* :mod:`.engine` — ``WorkflowRunner``: the supervise-cycle driver and
  per-stage dispatch/execution.
* :mod:`.review` — chief review / checkpoint / critic-judge refinement /
  adversarial debate behaviors.
* :mod:`.recovery` — provider-failure classification, recovery
  dispatches, stall rescue, and rollback policy.
* :mod:`.validation` — stage validation, outputs contract, commits, and
  the done gate.
"""

from ..agent import AgentRun, AgentRunner
from ..telemetry import CycleTelemetry
from .engine import WorkflowRunner
from .recovery import RecoveryMixin, is_provider_failure
from .review import ReviewMixin
from .stages import (
    _ROUND_STATUS_RE,
    StageResult,
    Workflow,
    WorkflowStage,
    _mini_yaml_loads,
    _round_status,
    load_workflow,
    yaml,
)
from .validation import ValidationMixin

__all__ = [
    "AgentRun",
    "AgentRunner",
    "CycleTelemetry",
    "RecoveryMixin",
    "ReviewMixin",
    "StageResult",
    "ValidationMixin",
    "Workflow",
    "WorkflowRunner",
    "WorkflowStage",
    "_ROUND_STATUS_RE",
    "_mini_yaml_loads",
    "_round_status",
    "is_provider_failure",
    "load_workflow",
    "yaml",
]
