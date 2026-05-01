"""Duplicate-agent prevention: name normalisation, prompt similarity, register_role gating."""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration import casting


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


# ---------------------------------------------------------------------------
# _name_key
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("a, b", [
    ("engineer", "engineering"),
    ("engineer", "engineers"),
    ("engineer", "coder"),
    ("engineer", "developer"),
    ("engineer", "developers"),
    ("engineer", "implementer"),
    ("engineer", "implementers"),
    ("analyst", "analysis"),
    ("analyst", "analytics"),
    ("researcher", "research"),
    ("researcher", "researchers"),
    ("qa", "tester"),
    ("qa", "validator"),
    ("qa", "reviewers"),
    ("operator", "devops"),
])
def test_name_key_collapses_synonyms(a: str, b: str) -> None:
    assert casting._name_key(a) == casting._name_key(b)


@pytest.mark.parametrize("a, b", [
    ("engineer", "librarian"),
    ("engineer", "data_steward"),
    ("analyst", "doc_writer"),
    ("researcher", "engineer"),
    ("qa", "engineer"),
])
def test_name_key_keeps_distinct_roles_distinct(a: str, b: str) -> None:
    assert casting._name_key(a) != casting._name_key(b)


# ---------------------------------------------------------------------------
# _similar_existing_name
# ---------------------------------------------------------------------------


def test_similar_existing_name_catches_plurals_and_synonyms() -> None:
    agents = {"engineer": {}, "qa": {}, "researcher": {}}
    assert casting._similar_existing_name("engineering", agents) == "engineer"
    assert casting._similar_existing_name("engineers", agents) == "engineer"
    assert casting._similar_existing_name("coder", agents) == "engineer"
    assert casting._similar_existing_name("tester", agents) == "qa"
    assert casting._similar_existing_name("research", agents) == "researcher"


def test_similar_existing_name_returns_none_for_distinct() -> None:
    agents = {"engineer": {}, "qa": {}}
    assert casting._similar_existing_name("librarian", agents) is None
    assert casting._similar_existing_name("data_steward", agents) is None


# ---------------------------------------------------------------------------
# _prompt_similarity (Jaccard with stopwords)
# ---------------------------------------------------------------------------


def test_prompt_similarity_high_for_near_duplicate() -> None:
    a = (
        "Implement the smallest vertical slice that advances the objective. "
        "Stay within project root. Add or update tests in tests/ for any "
        "code you change. Use ACTION blocks to create or edit files."
    )
    b = (
        "Implement the next vertical slice that advances the objective. "
        "Stay within the project root. Update tests in tests/ for any code "
        "you change. Use ACTION blocks to write or edit files."
    )
    assert casting._prompt_similarity(a, b) >= 0.45


def test_prompt_similarity_zero_for_unrelated() -> None:
    a = "Implement the next vertical slice that advances the objective."
    b = "Catalog book inventory and maintain lending records database."
    assert casting._prompt_similarity(a, b) < 0.1


def test_prompt_similarity_handles_empty() -> None:
    assert casting._prompt_similarity("", "anything") == 0.0
    assert casting._prompt_similarity("anything", "") == 0.0


# ---------------------------------------------------------------------------
# register_role: end-to-end gating
# ---------------------------------------------------------------------------


def _proposal(name: str, prompt: str = "", role: str = "") -> casting.RoleProposal:
    return casting.RoleProposal(name=name, role=role, prompt=prompt)


def test_register_role_rejects_synonym_name(paths: Paths) -> None:
    cfg: dict = {"agents": {"engineer": {"prompt": "engineer.md", "role": "implement"}}}
    ok, status = casting.register_role(
        paths,
        _proposal("engineering", prompt="any body"),
        agents_cfg=cfg,
    )
    assert ok is False
    assert status.startswith("similar_to_existing:engineer")


def test_register_role_rejects_engineering_even_without_engineer_in_agents(paths: Paths) -> None:
    # "engineer" is a seed-role anchor; the check must fire even when it is
    # absent from agents.json (e.g. after a manual reset to baseline-only).
    cfg: dict = {"agents": {}}  # engineer intentionally absent
    ok, status = casting.register_role(
        paths,
        _proposal("engineering", prompt="implement the objective"),
        agents_cfg=cfg,
    )
    assert ok is False
    assert "engineer" in status


def test_register_role_rejects_synonym_via_synonym_table(paths: Paths) -> None:
    cfg: dict = {"agents": {"engineer": {"prompt": "engineer.md", "role": "implement"}}}
    ok, status = casting.register_role(
        paths, _proposal("coder", prompt="distinct body"), agents_cfg=cfg
    )
    assert ok is False
    assert "engineer" in status


def test_register_role_rejects_near_duplicate_prompt(paths: Paths) -> None:
    # Seed an existing role with a real prompt body on disk.
    paths.prompts.mkdir(parents=True, exist_ok=True)
    body = (
        "You are the librarian agent. Catalog books, manage loans, and "
        "maintain the database of borrower records and lending histories. "
        "Audit the inventory weekly. Maintain database integrity for the "
        "library lending system and process loan requests efficiently."
    )
    (paths.prompts / "librarian.md").write_text(body, encoding="utf-8")
    cfg: dict = {"agents": {"librarian": {"prompt": "librarian.md", "role": "books"}}}

    near_dup = (
        "You are the bibliotech agent. Catalog book inventory, manage "
        "loans, and maintain the borrower records database for the "
        "lending system. Audit inventory weekly. Process loan requests "
        "efficiently and maintain database integrity."
    )
    ok, status = casting.register_role(
        paths, _proposal("bibliotech", prompt=near_dup), agents_cfg=cfg
    )
    assert ok is False
    assert status.startswith("similar_prompt_to:librarian")


def test_register_role_accepts_distinct(paths: Paths) -> None:
    cfg: dict = {"agents": {"engineer": {"prompt": "engineer.md", "role": "implement"}}}
    ok, status = casting.register_role(
        paths,
        _proposal("data_steward", prompt="ensure schema integrity and migrations", role="data"),
        agents_cfg=cfg,
    )
    assert ok is True
    assert status == "added"
    assert "data_steward" in cfg["agents"]
