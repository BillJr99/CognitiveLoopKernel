"""Response-quality validator for agent outputs.

Layer 1 of the robustness loops. Every dispatch in
:class:`~clk_harness.orchestration.agent.AgentRunner` is gated through
:func:`score` after the provider returns. When ``ResponseQuality.ok`` is
false the runner can re-dispatch with a repair preamble or escalate to
stochastic consensus, instead of accepting weak / malformed output.

The validator is intentionally cheap: regex + string checks, no provider
calls, no I/O beyond what's already passed in. Specific things it
detects:

* empty / sub-threshold text
* malformed ACTION blocks (header without END_ACTION, etc.)
* malformed POST blocks
* missing declared `outputs` POST keys (when ``expected_outputs`` is
  passed in)
* self-reported low confidence (``CONFIDENCE: <0..1>``,
  ``NEEDS_REVIEW: true``)
* refusal patterns ("I cannot", "I'm sorry, but ...") for ordinary tasks

Each flag carries a short reason string so the re-dispatch preamble can
quote it back to the worker.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

from . import blackboard as _blackboard

# ---------------------------------------------------------------------------
# Public dataclass
# ---------------------------------------------------------------------------


@dataclass
class ResponseQuality:
    """Verdict on a single agent response.

    ``ok`` is False whenever any flag in :attr:`flags` is set; the caller
    decides whether to retry based on :attr:`recoverable`. ``score`` is a
    rough 0..1 indicator usable by the critic-judge inner loop (Layer 3)
    when no external critic is dispatched.
    """

    ok: bool = True
    score: float = 1.0
    flags: List[str] = field(default_factory=list)
    reasons: List[str] = field(default_factory=list)
    # When False the caller should give up on this run rather than retry
    # (e.g. an explicit refusal). When True, a repair preamble is worth
    # spending tokens on.
    recoverable: bool = True
    # The CONFIDENCE / NEEDS_REVIEW values as parsed from the response,
    # surfaced separately so callers can log them without re-parsing.
    confidence: Optional[float] = None
    needs_review: Optional[bool] = None

    def summary(self) -> str:
        if self.ok:
            return f"ok score={self.score:.2f}"
        return f"flags={','.join(self.flags) or '?'} score={self.score:.2f}"

    def repair_hint(self) -> str:
        """Compact, model-readable description of what went wrong.

        Used by :class:`AgentRunner` to prefix the re-dispatch objective
        with the specific issues so the worker fixes them rather than
        re-rolling at random.
        """
        if self.ok or not self.reasons:
            return ""
        bullets = "\n".join(f"- {r}" for r in self.reasons)
        return (
            "Your previous response was rejected by the harness for the "
            "following reasons:\n"
            f"{bullets}\n"
            "Re-emit a complete response that fixes every item above."
        )


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------


_CONFIDENCE_RE = re.compile(
    r"^\s*CONFIDENCE\s*:\s*([0-9]*\.?[0-9]+)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_NEEDS_REVIEW_RE = re.compile(
    r"^\s*NEEDS_REVIEW\s*:\s*(true|yes|y|1|false|no|n|0)\s*$",
    re.IGNORECASE | re.MULTILINE,
)
_REFUSAL_RES: Sequence[re.Pattern] = tuple(
    re.compile(pat, re.IGNORECASE)
    for pat in (
        r"\bi\s+cannot\b",
        r"\bi\s+can'?t\b\s+(?:help|assist|do|comply)",
        r"\bi\s+(?:am|'m)\s+(?:sorry|unable)\b.*\b(?:cannot|can'?t|won'?t)\b",
        r"\bas\s+an\s+ai\s+(?:language\s+)?model\b",
        r"\bI\s+do\s+not\s+have\s+the\s+ability\b",
    )
)
_HEADERLESS_ACTION_RE = re.compile(r"^\s*ACTION\s*:\s*([A-Za-z]+)", re.IGNORECASE | re.MULTILINE)
_FILE_ACTION_RE = re.compile(
    r"^\s*ACTION\s*:\s*(write|edit|append|delete)\b", re.IGNORECASE | re.MULTILINE
)
_END_ACTION_RE = re.compile(r"^\s*END_ACTION\s*$", re.IGNORECASE | re.MULTILINE)
_POST_HEAD_RE = re.compile(r"^\s*POST\s*:\s*([A-Za-z][A-Za-z0-9_]*)\s*$", re.IGNORECASE | re.MULTILINE)
_POST_END_RE = re.compile(r"^\s*END_POST\s*$", re.IGNORECASE | re.MULTILINE)
_PROGRESS_RE = re.compile(r"^\s*PROGRESS\s*:\s*(yes|no|true|false)\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_confidence(text: str) -> Optional[float]:
    m = _CONFIDENCE_RE.search(text or "")
    if not m:
        return None
    try:
        v = float(m.group(1))
    except ValueError:
        return None
    if v < 0:
        v = 0.0
    if v > 1:
        # tolerate 0..100 scale
        v = min(1.0, v / 100.0)
    return v


def _parse_needs_review(text: str) -> Optional[bool]:
    m = _NEEDS_REVIEW_RE.search(text or "")
    if not m:
        return None
    return m.group(1).lower() in {"true", "yes", "y", "1"}


def progress_signal(text: Optional[str]) -> Optional[bool]:
    """Parse the agent's self-reported ``PROGRESS: yes/no`` marker.

    Returns True/False for an explicit signal (last marker wins when the
    response contains several), or None when the agent did not emit one.
    Used by the WorkflowRunner's stall detector: a cycle where every
    reporting agent says ``PROGRESS: no`` counts as stalled even when
    files were technically written.
    """
    matches = _PROGRESS_RE.findall(text or "")
    if not matches:
        return None
    return matches[-1].lower() in {"yes", "true"}


def _detect_refusal(text: str) -> bool:
    t = text or ""
    for pat in _REFUSAL_RES:
        if pat.search(t):
            return True
    return False


def _action_block_balance(text: str) -> int:
    """Difference between unterminated ACTION headers and END_ACTION lines.

    Returns 0 when balanced (or no actions at all). Positive when there
    are more ACTION headers than END_ACTION markers (malformed).
    """
    heads = len(_HEADERLESS_ACTION_RE.findall(text or ""))
    ends = len(_END_ACTION_RE.findall(text or ""))
    if heads == 0:
        return 0
    return heads - ends


def _post_block_balance(text: str) -> int:
    heads = len(_POST_HEAD_RE.findall(text or ""))
    ends = len(_POST_END_RE.findall(text or ""))
    if heads == 0:
        return 0
    return heads - ends


def _missing_outputs(text: str, expected: Sequence[str]) -> List[str]:
    """Return the subset of ``expected`` keys not present in any POST
    block's ``PRODUCES:`` list."""
    if not expected:
        return []
    try:
        blocks = _blackboard.parse_post_blocks(text or "")
    except Exception:
        blocks = []
    produced: set = set()
    for b in blocks:
        for key in (b.get("produces") or []):
            produced.add(str(key))
    return [k for k in expected if k not in produced]


# ---------------------------------------------------------------------------
# Top-level scoring
# ---------------------------------------------------------------------------


def score(
    text: Optional[str],
    *,
    min_chars: int = 40,
    expected_outputs: Optional[Sequence[str]] = None,
    require_confidence: bool = False,
    expect_file_action: bool = False,
) -> ResponseQuality:
    """Score a single response text against the harness's quality rules.

    Parameters
    ----------
    text:
        The full response text emitted by the worker.
    min_chars:
        Threshold below which the response is treated as suspect-empty.
    expected_outputs:
        When set, every key must appear in some POST block's PRODUCES list
        for the response to pass.
    require_confidence:
        When True, missing the ``CONFIDENCE:`` line itself counts as a flag.
        Off by default so existing agents that have not been re-prompted
        yet aren't penalised.
    expect_file_action:
        When True, a response that contains no file-mutating ACTION block
        (write / edit / append / delete) is flagged ``noop`` (recoverable) —
        the worker described work instead of doing it. Parse-level only; the
        dispatch loop also enforces this at apply time via
        ``AgentRun.file_mutations_applied``.
    """

    q = ResponseQuality()
    body = (text or "").strip()
    q.confidence = _parse_confidence(text or "")
    q.needs_review = _parse_needs_review(text or "")

    # 1. Empty / near-empty
    if not body or len(body) < max(1, int(min_chars)):
        q.flags.append("empty")
        q.reasons.append(
            f"Response body was {len(body)} chars (minimum {min_chars}). "
            "Re-emit a substantive response."
        )

    # 2. Refusal — not recoverable; harness should escalate to chief instead
    if _detect_refusal(text or ""):
        q.flags.append("refusal")
        q.reasons.append(
            "Response looked like a refusal. The task is in-scope for this "
            "harness; respond directly or, if blocked, post a POST: question "
            "to the chief explaining the obstacle."
        )
        q.recoverable = False

    # 3. Malformed ACTION blocks
    # balance==1 is forgivable: the parser already handles an EOF-terminated
    # block correctly (the last block's END_ACTION was truncated). Only flag
    # when 2+ blocks are unclosed, which means genuinely interleaved nesting
    # that would corrupt parsing.
    act_balance = _action_block_balance(text or "")
    if act_balance > 1:
        q.flags.append("malformed_action")
        q.reasons.append(
            f"{act_balance} ACTION header(s) had no matching END_ACTION. "
            "Every ACTION block must terminate with a line `END_ACTION`."
        )

    # 4. Malformed POST blocks
    post_balance = _post_block_balance(text or "")
    if post_balance > 0:
        q.flags.append("malformed_post")
        q.reasons.append(
            f"{post_balance} POST header(s) had no matching END_POST. "
            "Every POST block must terminate with a line `END_POST`."
        )

    # 5. Missing declared outputs
    missing = _missing_outputs(text or "", list(expected_outputs or []))
    if missing:
        q.flags.append("outputs_missing")
        produces_line = ", ".join(missing)
        q.reasons.append(
            "Declared output contract keys not satisfied: "
            f"{', '.join(missing)}. You MUST emit a POST block that lists "
            "every missing key in its PRODUCES line. Exact format:\n"
            f"  POST: finding\n"
            f"  PRODUCES: {produces_line}\n"
            f"  BODY:\n"
            f"  <your summary here>\n"
            f"  END_POST\n"
            "The PRODUCES line must contain every unsatisfied key above, "
            "comma-separated on a single line."
        )

    # 5b. No-op: a producing stage that emitted no file-mutating ACTION.
    if expect_file_action and not _FILE_ACTION_RE.search(text or ""):
        q.flags.append("noop")
        q.reasons.append(
            "Your response changed no files — it contained no ACTION:write/"
            "edit/append/delete block. Descriptions do nothing here. Emit at "
            "least one real file-mutating ACTION block."
        )

    # 6. Self-reported low confidence
    if q.confidence is not None and q.confidence < 0.5:
        q.flags.append("low_confidence")
        q.reasons.append(
            f"You reported CONFIDENCE: {q.confidence:.2f}. Either "
            "improve the response or escalate via POST: question."
        )
    if q.needs_review is True:
        q.flags.append("needs_review_self")
        q.reasons.append(
            "You set NEEDS_REVIEW: true. Sharpen the answer or call out "
            "the specific uncertainty so a peer can resolve it."
        )

    if require_confidence and q.confidence is None:
        q.flags.append("confidence_missing")
        q.reasons.append(
            "Response did not include a CONFIDENCE: <0..1> line. Emit "
            "one final line stating your confidence so the harness can "
            "decide whether to re-sample."
        )

    # Final aggregation
    q.ok = not q.flags
    # Rough score: 1.0 minus fixed deductions per flag, floored at 0.
    deductions = {
        "empty": 0.6,
        "refusal": 0.5,
        "malformed_action": 0.4,
        "malformed_post": 0.3,
        "outputs_missing": 0.4,
        "noop": 0.5,
        "low_confidence": 0.3,
        "needs_review_self": 0.2,
        "confidence_missing": 0.1,
    }
    s = 1.0
    for f in q.flags:
        s -= deductions.get(f, 0.2)
    q.score = max(0.0, round(s, 3))
    return q


def is_recoverable(q: ResponseQuality) -> bool:
    """Convenience: is the quality verdict worth retrying?"""
    return (not q.ok) and q.recoverable
