"""Presentation helpers shared by the TUI widgets.

Token-count formatting and word wrapping used by the dashboard
cards, the worker's status output, and the curses renderer.
"""

from __future__ import annotations

from typing import List

from ..log import get_logger

logger = get_logger(__name__)


def _format_tokens(n: int) -> str:
    if n >= 1_000_000:
        return f"{n/1_000_000:.2f}M"
    if n >= 1_000:
        return f"{n/1_000:.1f}k"
    return str(n)


def _word_wrap(text: str, width: int) -> List[str]:
    """Wrap ``text`` to ``width`` columns, breaking on word boundaries.

    Falls back to mid-word splits when a single token exceeds width
    (e.g. a long URL). Empty input returns ``[""]`` so callers can rely
    on at least one row.
    """
    if width <= 1:
        return [text]
    if not text:
        return [""]
    out: List[str] = []
    for paragraph in text.splitlines() or [text]:
        if not paragraph:
            out.append("")
            continue
        words = paragraph.split(" ")
        current = ""
        for word in words:
            if len(word) > width:
                # Hard-break the giant token. Flush whatever we have first.
                if current:
                    out.append(current)
                    current = ""
                while len(word) > width:
                    out.append(word[:width])
                    word = word[width:]
                current = word
                continue
            if not current:
                current = word
            elif len(current) + 1 + len(word) <= width:
                current = f"{current} {word}"
            else:
                out.append(current)
                current = word
        if current:
            out.append(current)
    return out or [""]

