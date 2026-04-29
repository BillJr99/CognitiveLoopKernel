"""Common provider interface."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class ProviderUnavailable(RuntimeError):
    """Raised when a provider cannot service a request."""


@dataclass
class AgentRequest:
    """A single agent invocation."""

    agent: str
    prompt: str
    system: Optional[str] = None
    context_files: List[str] = field(default_factory=list)
    allowed_files: List[str] = field(default_factory=list)
    workdir: Optional[Path] = None
    metadata: Dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False
    timeout_s: int = 600


@dataclass
class AgentResponse:
    """Result of an agent invocation."""

    ok: bool
    text: str = ""
    files_written: List[str] = field(default_factory=list)
    error: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)


class AgentProvider:
    """Abstract base. Subclasses must implement :meth:`invoke`."""

    type_name: str = "base"

    def __init__(self, *, name: str, config: Optional[Dict[str, Any]] = None) -> None:
        self.name = name
        self.config = dict(config or {})

    def available(self) -> bool:
        """Return True if this provider can be used in the current env."""
        return True

    def describe(self) -> str:
        return f"{self.name} ({self.type_name})"

    def invoke(self, req: AgentRequest) -> AgentResponse:
        raise NotImplementedError
