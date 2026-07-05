"""Agent runner.

Loads a prompt template, renders it against the current state, and
invokes the configured provider. The runner is intentionally thin -
heavier orchestration lives in :mod:`workflow` and the loops.

Decomposed package; this ``__init__`` preserves the public surface of
the former ``clk_harness/orchestration/agent.py`` module:

* :mod:`.runner` — ``AgentRunner``: dispatch entry point, robustness
  layers (quality loop, auto-consensus), and provider invocation.
* :mod:`.prompts` — prompt assembly (templates, context collection,
  meta-prompting cache).
* :mod:`.transcript` — run records (``AgentSpec``, ``AgentRun``,
  ``AgentObserver``) and response-transcript processing (POST /
  PROPOSE / ACTION blocks, run history).
"""

from .prompts import PromptsMixin, _read_recent_casting_rejections
from .runner import AgentRunner
from .transcript import AgentObserver, AgentRun, AgentSpec, TranscriptMixin

__all__ = [
    "AgentObserver",
    "AgentRun",
    "AgentRunner",
    "AgentSpec",
    "PromptsMixin",
    "TranscriptMixin",
    "_read_recent_casting_rejections",
]
