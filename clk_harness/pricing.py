"""Provider pricing table.

A small, hand-curated map from ``(provider, model)`` to USD-per-1k-tokens
rates. Used by the TUI title bar to surface projected cost as a run
proceeds, and by the session-cost cap to pause loops that cross a
configured spend limit.

The numbers here are best-effort and easy to override per-project by
adding ``"pricing": {"input_per_1k": ..., "output_per_1k": ...}`` to
the matching block in ``.clk/config/providers.json``. The override is
applied per-provider; a per-model override lives under
``providers.{name}.pricing_by_model.{model}``.

Local providers (``shell``, ``ollama``) cost nothing — they're omitted
from the table so they render as ``$0.00`` everywhere.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional


@dataclass(frozen=True)
class Price:
    input_per_1k: float
    output_per_1k: float


# Seed table. Update via providers.json overrides, not here, when prices
# move — keeping this constant means tests can pin to a known baseline.
DEFAULT_PRICING: Dict[str, Dict[str, Price]] = {
    "claude": {
        "claude-sonnet-4-5":         Price(0.003, 0.015),
        "claude-3-5-sonnet-latest":  Price(0.003, 0.015),
        "claude-3-5-sonnet-20241022":Price(0.003, 0.015),
        "claude-3-5-haiku-latest":   Price(0.0008, 0.004),
        "claude-3-opus-latest":      Price(0.015, 0.075),
        "_default":                  Price(0.003, 0.015),
    },
    "codex": {
        "gpt-4o":      Price(0.0025, 0.010),
        "gpt-4o-mini": Price(0.00015, 0.0006),
        "o1-mini":     Price(0.003, 0.012),
        "o1":          Price(0.015, 0.060),
        "_default":    Price(0.0025, 0.010),
    },
    "gemini": {
        "gemini-1.5-pro":   Price(0.00125, 0.005),
        "gemini-1.5-flash": Price(0.000075, 0.0003),
        "_default":         Price(0.00125, 0.005),
    },
    "pi": {
        # pi.dev routes through whatever upstream provider you point it
        # at, so we use a conservative blended default. Users who want
        # exact numbers should override via providers.pi.pricing.
        "_default": Price(0.003, 0.015),
    },
    "openwebui": {
        # OpenWebUI usually fronts a self-hosted model; assume free
        # unless the user opts in via providers.openwebui.pricing.
        "_default": Price(0.0, 0.0),
    },
    "ollama":   {"_default": Price(0.0, 0.0)},
    "shell":    {"_default": Price(0.0, 0.0)},
}


def lookup(provider: str, model: Optional[str], overrides: Optional[Dict[str, Any]] = None) -> Price:
    """Return the per-1k pricing for ``(provider, model)``.

    Resolution order:
      1. per-model override under ``overrides.pricing_by_model``
      2. per-provider override under ``overrides.pricing``
      3. ``DEFAULT_PRICING[provider][model]``
      4. ``DEFAULT_PRICING[provider]["_default"]``
      5. Price(0.0, 0.0) (so unknown providers don't crash the title bar)
    """
    overrides = overrides or {}
    by_model = (overrides.get("pricing_by_model") or {}).get(model or "", None)
    if isinstance(by_model, dict):
        return Price(
            float(by_model.get("input_per_1k", 0.0)),
            float(by_model.get("output_per_1k", 0.0)),
        )
    flat = overrides.get("pricing")
    if isinstance(flat, dict):
        return Price(
            float(flat.get("input_per_1k", 0.0)),
            float(flat.get("output_per_1k", 0.0)),
        )
    table = DEFAULT_PRICING.get(provider) or {}
    if model and model in table:
        return table[model]
    return table.get("_default", Price(0.0, 0.0))


def estimate_usd(
    provider: str,
    model: Optional[str],
    input_tokens: int,
    output_tokens: int,
    overrides: Optional[Dict[str, Any]] = None,
) -> float:
    """Best-effort USD cost for the given token counts."""
    p = lookup(provider, model, overrides)
    return (input_tokens / 1000.0) * p.input_per_1k + (output_tokens / 1000.0) * p.output_per_1k


def format_usd(amount: float) -> str:
    """Format a USD amount for the TUI title bar.

    Free tiers render as ``$0.00``. Below a cent renders three decimals
    so the user can see "almost-zero" totals creeping up. Past a dollar
    rounds to two decimals.
    """
    if amount <= 0:
        return "$0.00"
    if amount < 0.01:
        return f"${amount:.3f}"
    if amount < 1.0:
        return f"${amount:.2f}"
    return f"${amount:.2f}"


__all__ = [
    "Price",
    "DEFAULT_PRICING",
    "lookup",
    "estimate_usd",
    "format_usd",
]
