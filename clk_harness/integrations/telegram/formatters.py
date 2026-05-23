"""Message formatting helpers for the Telegram bot.

Telegram caps a single text message at 4096 characters. Long output is
chunked into multiple sends. A token-redaction helper guards against
accidentally echoing the bot's HTTP API token into chat (Telegram tokens
look like ``\\d+:[A-Za-z0-9_-]{30,}``).
"""

from __future__ import annotations

import re
from typing import Iterable, List, Optional

TELEGRAM_MAX = 4096
_TOKEN_RE = re.compile(r"\b\d{6,12}:[A-Za-z0-9_-]{30,}\b")


def redact_token(text: str, token: Optional[str] = None) -> str:
    """Replace Telegram bot tokens in ``text`` with ``[REDACTED]``."""
    out = _TOKEN_RE.sub("[REDACTED]", text)
    if token:
        out = out.replace(token, "[REDACTED]")
    return out


def chunk(text: str, limit: int = TELEGRAM_MAX) -> List[str]:
    """Split ``text`` into messages no larger than ``limit`` chars.

    Prefers line boundaries; falls back to hard-slicing if any single line
    exceeds the limit.
    """
    if len(text) <= limit:
        return [text]
    parts: List[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        if len(line) > limit:
            # Flush current, then hard-slice the over-long line.
            if current:
                parts.append(current)
                current = ""
            for i in range(0, len(line), limit):
                parts.append(line[i : i + limit])
            continue
        if len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


def code_block(text: str, lang: str = "") -> str:
    """Wrap ``text`` in a Markdown code fence for monospace display."""
    return f"```{lang}\n{text}\n```"


def tail(lines: Iterable[str], n: int) -> List[str]:
    """Return the last ``n`` lines from an iterable."""
    buf: List[str] = []
    for line in lines:
        buf.append(line)
        if len(buf) > n:
            buf.pop(0)
    return buf
