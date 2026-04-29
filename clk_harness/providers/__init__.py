"""Provider abstraction for CLK.

Each provider exposes a single ``AgentProvider`` interface so the
orchestration layer can drive Claude Code, Codex, Pi, Ollama, or a
shell-based dummy uniformly.
"""

from .base import AgentProvider, AgentRequest, AgentResponse, ProviderUnavailable
from .shell import ShellProvider
from .claude import ClaudeProvider
from .codex import CodexProvider
from .pi import PiProvider
from .ollama import OllamaProvider

__all__ = [
    "AgentProvider",
    "AgentRequest",
    "AgentResponse",
    "ProviderUnavailable",
    "ShellProvider",
    "ClaudeProvider",
    "CodexProvider",
    "PiProvider",
    "OllamaProvider",
    "load_provider",
    "available_providers",
]


def load_provider(name: str, config: dict) -> AgentProvider:
    """Instantiate a provider from its config block.

    ``config`` is the entry under ``providers.json`` -> ``providers`` -> name.
    """
    p_type = (config or {}).get("type") or name
    if p_type == "shell":
        return ShellProvider(name=name, config=config)
    if p_type == "claude":
        return ClaudeProvider(name=name, config=config)
    if p_type == "codex":
        return CodexProvider(name=name, config=config)
    if p_type == "pi":
        return PiProvider(name=name, config=config)
    if p_type == "ollama":
        return OllamaProvider(name=name, config=config)
    # Unknown - degrade to shell so the harness keeps running.
    return ShellProvider(name=name, config=config)


def available_providers(providers_cfg: dict) -> dict:
    """Return ``{name: bool}`` indicating which providers are usable."""
    out = {}
    for name, cfg in (providers_cfg.get("providers") or {}).items():
        try:
            prov = load_provider(name, cfg)
            out[name] = prov.available()
        except Exception:
            out[name] = False
    return out
