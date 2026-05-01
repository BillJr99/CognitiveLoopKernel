"""Blackboard module: posting, filtering, digest, contract verification."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clk_harness.config import Paths
from clk_harness.orchestration import blackboard as bb


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


def test_post_persists_json_with_schema(paths: Paths) -> None:
    p = bb.post(
        paths,
        author="researcher",
        body="Found that X correlates with Y",
        post_type="finding",
        produces=["x_y_correlation"],
        stage_id="research_a",
        workflow="engineering",
        slug_hint="x-y-correlation",
    )
    assert p.id
    assert p.author == "researcher"
    assert p.post_type == "finding"
    assert "x_y_correlation" in p.produces
    file_on_disk = paths.blackboard / f"{p.id}.json"
    assert file_on_disk.exists()
    raw = json.loads(file_on_disk.read_text(encoding="utf-8"))
    assert raw["author"] == "researcher"
    assert raw["body"].startswith("Found that")


def test_blackboard_lives_under_clk(paths: Paths) -> None:
    """Blackboard lives under .clk/, not in the project root or src tree."""
    assert paths.blackboard.is_relative_to(paths.clk)
    assert not paths.blackboard.is_relative_to(paths.root / "src")
    assert paths.blackboard == paths.clk / "blackboard"


def test_list_posts_returns_in_chronological_order(paths: Paths) -> None:
    bb.post(paths, author="a", body="first", post_type="note", slug_hint="one")
    bb.post(paths, author="b", body="second", post_type="note", slug_hint="two")
    bb.post(paths, author="c", body="third", post_type="note", slug_hint="three")
    posts = bb.list_posts(paths)
    assert [p.author for p in posts] == ["a", "b", "c"]


def test_filter_posts_by_type(paths: Paths) -> None:
    bb.post(paths, author="a", body="x", post_type="finding")
    bb.post(paths, author="b", body="y", post_type="decision")
    bb.post(paths, author="c", body="z", post_type="finding")
    findings = bb.filter_posts(bb.list_posts(paths), ["type:finding"])
    assert len(findings) == 2
    assert all(p.post_type == "finding" for p in findings)


def test_filter_posts_by_stage(paths: Paths) -> None:
    bb.post(paths, author="a", body="x", post_type="note", stage_id="stage_1")
    bb.post(paths, author="b", body="y", post_type="note", stage_id="stage_2")
    out = bb.filter_posts(bb.list_posts(paths), ["stage:stage_1"])
    assert len(out) == 1
    assert out[0].stage_id == "stage_1"


def test_filter_posts_by_produces_contract(paths: Paths) -> None:
    bb.post(paths, author="a", body="x", post_type="note", produces=["brief"])
    bb.post(paths, author="b", body="y", post_type="note", produces=["other"])
    out = bb.filter_posts(bb.list_posts(paths), ["produces:brief"])
    assert len(out) == 1
    assert "brief" in out[0].produces


def test_digest_truncates_long_bodies(paths: Paths) -> None:
    bb.post(paths, author="a", body="x" * 5000, post_type="note", slug_hint="big")
    text = bb.digest(paths, max_chars_per_post=200)
    assert "…" in text
    # No single line should exceed the truncation cap by much
    assert max(len(l) for l in text.splitlines()) < 400


def test_digest_empty_when_no_posts(paths: Paths) -> None:
    assert bb.digest(paths) == "Blackboard digest: (empty)"


def test_outputs_contract_satisfied_via_produces(paths: Paths) -> None:
    bb.post(paths, author="researcher", body="x", post_type="finding",
            stage_id="research", produces=["brief", "facts"])
    missing = bb.find_outputs_satisfied(
        bb.list_posts(paths), stage_id="research", expected=["brief", "facts"]
    )
    assert missing == []


def test_outputs_contract_unmet_when_no_post(paths: Paths) -> None:
    missing = bb.find_outputs_satisfied(
        bb.list_posts(paths), stage_id="research", expected=["brief"]
    )
    assert missing == ["brief"]


def test_outputs_contract_unmet_when_other_stage_posts(paths: Paths) -> None:
    bb.post(paths, author="a", body="x", post_type="note",
            stage_id="other", produces=["brief"])
    missing = bb.find_outputs_satisfied(
        bb.list_posts(paths), stage_id="research", expected=["brief"]
    )
    assert missing == ["brief"]


def test_post_block_parsing_full_grammar() -> None:
    text = """
preamble noise
POST: finding
TITLE: x correlates with y
PRODUCES: brief, x_y
CONSUMES: 20260101T000000-prior-id
BODY:
- correlation 0.91
- p < 0.001
END_POST

trailing noise
"""
    blocks = bb.parse_post_blocks(text)
    assert len(blocks) == 1
    b = blocks[0]
    assert b["post_type"] == "finding"
    assert b["title"] == "x correlates with y"
    assert b["produces"] == ["brief", "x_y"]
    assert b["consumes"] == ["20260101T000000-prior-id"]
    assert "correlation 0.91" in b["body"]


def test_post_block_parsing_multiple() -> None:
    text = """
POST: finding
BODY:
first
END_POST

POST: question
BODY:
second
END_POST
"""
    blocks = bb.parse_post_blocks(text)
    assert len(blocks) == 2
    assert blocks[0]["post_type"] == "finding"
    assert blocks[1]["post_type"] == "question"


def test_apply_post_blocks_persists_with_provenance(paths: Paths) -> None:
    text = """
POST: finding
TITLE: a
PRODUCES: alpha
BODY:
hello
END_POST
"""
    posted = bb.apply_post_blocks(
        paths, text, author="researcher", stage_id="research", workflow="engineering"
    )
    assert len(posted) == 1
    p = posted[0]
    assert p.author == "researcher"
    assert p.stage_id == "research"
    assert p.workflow == "engineering"
    assert p.produces == ["alpha"]
    # And it lands on disk
    assert (paths.blackboard / f"{p.id}.json").exists()
