"""Tests for the atomic-write behavior of clk_harness.config.save_json."""

import json

from clk_harness.config import load_json, save_json


def test_save_json_writes_payload(tmp_path):
    p = tmp_path / "out.json"
    save_json(p, {"hello": "world"})
    assert json.loads(p.read_text(encoding="utf-8")) == {"hello": "world"}


def test_save_json_rotates_previous_to_bak(tmp_path):
    p = tmp_path / "out.json"
    save_json(p, {"v": 1})
    save_json(p, {"v": 2})
    # Current file has v=2.
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}
    # Previous content rotated to .bak.
    bak = p.with_name(p.name + ".bak")
    assert bak.exists()
    assert json.loads(bak.read_text(encoding="utf-8")) == {"v": 1}


def test_save_json_no_backup_when_file_absent(tmp_path):
    p = tmp_path / "out.json"
    save_json(p, {"v": 1})
    bak = p.with_name(p.name + ".bak")
    # First write doesn't create a .bak (there was nothing to back up).
    assert not bak.exists()


def test_save_json_with_backup_false(tmp_path):
    p = tmp_path / "out.json"
    save_json(p, {"v": 1})
    save_json(p, {"v": 2}, backup=False)
    bak = p.with_name(p.name + ".bak")
    # backup=False suppresses the rotation.
    assert not bak.exists()
    assert json.loads(p.read_text(encoding="utf-8")) == {"v": 2}


def test_save_json_round_trip_via_load_json(tmp_path):
    p = tmp_path / "out.json"
    payload = {"a": 1, "b": [1, 2, 3], "c": {"nested": True}}
    save_json(p, payload)
    assert load_json(p) == payload


def test_save_json_no_temp_file_left_behind(tmp_path):
    p = tmp_path / "out.json"
    save_json(p, {"v": 1})
    # We use tmp+rename, so no .tmp should remain after success.
    leftovers = [child for child in tmp_path.iterdir() if child.name.endswith(".tmp")]
    assert leftovers == []
