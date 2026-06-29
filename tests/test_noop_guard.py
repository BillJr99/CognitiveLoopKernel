"""Unit tests for the no-op guard (FM1) and its response-quality flag."""

from __future__ import annotations

import pytest

from clk_harness.orchestration import noop_guard
from clk_harness.orchestration.response_quality import score


_CFG = {"noop_guard": {"enabled": True, "max_redispatch": 2,
                       "producing_agents": ["engineer", "ralph"],
                       "treat_outputs_stage_as_producing": True}}


def test_producing_agent_expected_to_mutate():
    assert noop_guard.is_mutation_expected("engineer", cfg=_CFG)
    assert noop_guard.is_mutation_expected("ralph", cfg=_CFG)


def test_prose_only_agents_not_expected():
    assert not noop_guard.is_mutation_expected("chief", cfg=_CFG)
    assert not noop_guard.is_mutation_expected("qa", cfg=_CFG)
    assert not noop_guard.is_mutation_expected("critic", cfg=_CFG)


def test_outputs_contract_makes_stage_producing():
    assert noop_guard.is_mutation_expected("analyst", outputs=["brief"], cfg=_CFG)
    assert not noop_guard.is_mutation_expected("analyst", outputs=[], cfg=_CFG)


def test_commit_stage_is_producing_for_unknown_role():
    assert noop_guard.is_mutation_expected("designer", commit=True, cfg=_CFG)
    # but chief/qa/critic stay prose-only even with commit
    assert not noop_guard.is_mutation_expected("qa", commit=True, cfg=_CFG)


def test_disabled_guard_never_expects():
    cfg = {"noop_guard": {"enabled": False}}
    assert not noop_guard.is_mutation_expected("engineer", cfg=cfg)
    assert noop_guard.max_redispatch(cfg) == 0


def test_repair_preamble_escalates():
    a1 = noop_guard.repair_preamble(1)
    a2 = noop_guard.repair_preamble(2)
    a3 = noop_guard.repair_preamble(3)
    assert "changed NO files" in a1
    assert "Worked example" in a2 or "worked example" in a2.lower()
    assert "FINAL ATTEMPT" in a3
    # later attempts get progressively more forceful / different text
    assert a1 != a2 != a3


def test_max_redispatch_reads_config():
    assert noop_guard.max_redispatch(_CFG) == 2


def test_score_flags_noop_on_prose():
    q = score(
        "I would create the parser and then add tests. This is a solid plan "
        "with plenty of detail to exceed the minimum character threshold.",
        expect_file_action=True,
    )
    assert "noop" in q.flags
    assert not q.ok
    assert q.recoverable  # worth re-dispatching


def test_score_no_noop_with_real_action():
    q = score(
        "ACTION: write\nPATH: src/app.py\nCONTENT:\nprint('hi')\nEND_ACTION",
        expect_file_action=True,
    )
    assert "noop" not in q.flags


def test_score_noop_not_flagged_when_not_expected():
    q = score("Just a prose answer, no actions, but not expected to mutate either.")
    assert "noop" not in q.flags
