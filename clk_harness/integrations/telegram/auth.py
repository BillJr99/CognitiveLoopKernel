"""Allowlist enforcement for Telegram users.

Resolution order (highest precedence first):
  1. ``CLK_TELEGRAM_ALLOWED_USERS`` env var (comma-separated numeric IDs)
  2. ``telegram.allowed_user_ids`` array in ``.clk/clk.json``
  3. CLI ``--allow-user`` flags (passed in via ``extra_ids``)

Non-numeric entries are silently dropped. Empty allowlist means *nobody*
is permitted (fail-closed) -- the bot will reply with a canned message
and ignore the update.
"""

from __future__ import annotations

import os
from typing import Iterable, List, Optional, Set


def _parse_ids(raw: Optional[str]) -> List[int]:
    if not raw:
        return []
    out: List[int] = []
    for piece in raw.split(","):
        piece = piece.strip()
        if not piece:
            continue
        try:
            out.append(int(piece))
        except ValueError:
            continue
    return out


def load_allowlist(
    config_ids: Optional[Iterable[int]] = None,
    extra_ids: Optional[Iterable[int]] = None,
    *,
    env: Optional[dict] = None,
) -> Set[int]:
    """Compute the effective allowlist as a set of integer user IDs."""
    src = env if env is not None else os.environ
    ids: Set[int] = set()
    ids.update(_parse_ids(src.get("CLK_TELEGRAM_ALLOWED_USERS")))
    if config_ids:
        for cid in config_ids:
            try:
                ids.add(int(cid))
            except (TypeError, ValueError):
                continue
    if extra_ids:
        for cid in extra_ids:
            try:
                ids.add(int(cid))
            except (TypeError, ValueError):
                continue
    return ids


def is_allowed(user_id: Optional[int], allowlist: Set[int]) -> bool:
    if user_id is None:
        return False
    return user_id in allowlist
