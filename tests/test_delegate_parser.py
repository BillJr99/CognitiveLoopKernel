"""Unit tests for the DELEGATE: block parser (casting.director)."""

from __future__ import annotations

from clk_harness.orchestration import casting


def test_parse_full_block() -> None:
    props = casting.parse_delegate_proposals(
        "noise\n"
        "DELEGATE: probe\n"
        "TO: engineer\n"
        "CONTEXT: focus on the parser\n"
        "TASK:\n"
        "investigate the failing case\n"
        "and report the root cause\n"
        "END_DELEGATE\n"
        "trailing noise"
    )
    assert len(props) == 1
    p = props[0]
    assert p.name == "probe"
    assert p.target == "engineer"
    assert p.context == "focus on the parser"
    assert p.objective == "investigate the failing case\nand report the root cause"


def test_context_is_optional() -> None:
    props = casting.parse_delegate_proposals(
        "DELEGATE: x\nTO: qa\nTASK:\nrun the checks\nEND_DELEGATE"
    )
    assert len(props) == 1
    assert props[0].context == ""
    assert props[0].objective == "run the checks"


def test_missing_target_is_dropped() -> None:
    assert casting.parse_delegate_proposals("DELEGATE: x\nTASK:\ny\nEND_DELEGATE") == []


def test_missing_task_is_dropped() -> None:
    assert casting.parse_delegate_proposals("DELEGATE: x\nTO: engineer\nEND_DELEGATE") == []


def test_no_block_returns_empty() -> None:
    assert casting.parse_delegate_proposals("just prose, no delegate") == []


def test_multiple_blocks_all_parsed() -> None:
    props = casting.parse_delegate_proposals(
        "DELEGATE: a\nTO: engineer\nTASK:\nfirst\nEND_DELEGATE\n"
        "DELEGATE: b\nTO: qa\nTASK:\nsecond\nEND_DELEGATE\n"
    )
    assert [(p.name, p.target, p.objective) for p in props] == [
        ("a", "engineer", "first"),
        ("b", "qa", "second"),
    ]
