"""Bundled prompt and workflow templates.

Materialized into ``.clk/prompts/`` and ``.clk/config/workflows/`` by
``clk init``. Kept as inline strings so the package has no external
data-file dependencies.
"""

from .prompts import PROMPTS
from .workflows import WORKFLOWS

__all__ = ["PROMPTS", "WORKFLOWS"]
