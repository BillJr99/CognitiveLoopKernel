"""Unit tests for the .env read/write helper + schema (clk_harness/env_file.py)."""

from __future__ import annotations

from pathlib import Path

import pytest

from clk_harness import env_file


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    target = tmp_path / ".env"
    monkeypatch.setenv("CLK_ENV_FILE", str(target))
    return target


def test_is_secret_key_heuristic() -> None:
    assert env_file.is_secret_key("ANTHROPIC_API_KEY")
    assert env_file.is_secret_key("OPENAI_API_KEY")
    assert env_file.is_secret_key("CLK_TELEGRAM_BOT_TOKEN")
    assert env_file.is_secret_key("SOME_PASSWORD")
    # CLK_PI_KEY_TYPE names a provider; it is NOT a secret.
    assert not env_file.is_secret_key("CLK_PI_KEY_TYPE")
    assert not env_file.is_secret_key("CLK_PROVIDER")
    assert not env_file.is_secret_key("CLK_MAX_ITERATIONS")


def test_write_then_read_round_trip(env: Path) -> None:
    env_file.write_env({"CLK_PROVIDER": "claude", "CLK_MAX_ITERATIONS": "7"})
    data = env_file.read_env()
    assert data["CLK_PROVIDER"] == "claude"
    assert data["CLK_MAX_ITERATIONS"] == "7"


def test_comments_and_order_preserved(env: Path) -> None:
    env.write_text(
        "# header comment\n"
        "CLK_PROVIDER=shell\n"
        "\n"
        "# section\n"
        "CLK_MAX_ITERATIONS=10\n",
        encoding="utf-8",
    )
    env_file.write_env({"CLK_PROVIDER": "ollama"})
    text = env.read_text(encoding="utf-8")
    assert "# header comment" in text
    assert "# section" in text
    # Order preserved: provider line still before iterations line.
    assert text.index("CLK_PROVIDER") < text.index("CLK_MAX_ITERATIONS")
    assert "CLK_PROVIDER=ollama" in text


def test_atomic_write_leaves_backup(env: Path) -> None:
    env_file.write_env({"CLK_PROVIDER": "shell"})
    env_file.write_env({"CLK_PROVIDER": "claude"})
    bak = env.with_suffix(env.suffix + ".bak")
    assert bak.exists()
    assert "shell" in bak.read_text(encoding="utf-8")


def test_sentinel_preserves_secret(env: Path) -> None:
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-secret-123"})
    # Submitting the mask sentinel must NOT overwrite the stored secret.
    env_file.write_env({"ANTHROPIC_API_KEY": env_file.MASK_SENTINEL, "CLK_PROVIDER": "claude"})
    data = env_file.read_env()
    assert data["ANTHROPIC_API_KEY"] == "sk-secret-123"
    assert data["CLK_PROVIDER"] == "claude"


def test_none_blanks_value(env: Path) -> None:
    env_file.write_env({"CLK_PROVIDER": "claude"})
    env_file.write_env({"CLK_PROVIDER": None})
    assert env_file.read_env().get("CLK_PROVIDER", "") == ""


def test_removals_drop_key(env: Path) -> None:
    env_file.write_env({"FOO": "bar", "BAZ": "qux"})
    env_file.write_env({}, removals={"FOO"})
    data = env_file.read_env()
    assert "FOO" not in data
    assert data["BAZ"] == "qux"


def test_describe_env_masks_secrets(env: Path) -> None:
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-xyz", "CLK_PROVIDER": "claude"})
    variables, groups = env_file.describe_env()
    by_key = {v["key"]: v for v in variables}
    assert by_key["ANTHROPIC_API_KEY"]["value"] == env_file.MASK_SENTINEL
    assert by_key["ANTHROPIC_API_KEY"]["masked"] is True
    assert by_key["ANTHROPIC_API_KEY"]["is_secret"] is True
    # Non-secret value shown in the clear.
    assert by_key["CLK_PROVIDER"]["value"] == "claude"
    assert by_key["CLK_PROVIDER"]["masked"] is False
    # Groups are ordered and non-empty.
    assert "Core" in groups
    assert "API Keys" in groups


def test_describe_env_reveal_shows_secret(env: Path) -> None:
    env_file.write_env({"ANTHROPIC_API_KEY": "sk-xyz"})
    variables, _ = env_file.describe_env(reveal=True)
    by_key = {v["key"]: v for v in variables}
    assert by_key["ANTHROPIC_API_KEY"]["value"] == "sk-xyz"


def test_unknown_key_lands_in_extra_group(env: Path) -> None:
    env_file.write_env({"MY_CUSTOM_FLAG": "1"})
    variables, groups = env_file.describe_env()
    by_key = {v["key"]: v for v in variables}
    assert by_key["MY_CUSTOM_FLAG"]["group"] == "Other"
    assert "Other" in groups


def test_quotes_and_export_parsed() -> None:
    lines = env_file.parse_env('export FOO="a b"\nBAR=plain\n# c\n')
    kv = {ln.key: ln.value for ln in lines if ln.kind == "kv"}
    assert kv["FOO"] == "a b"
    assert kv["BAR"] == "plain"
    assert any(ln.kind == "comment" for ln in lines)
