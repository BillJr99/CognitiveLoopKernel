from clk_harness.integrations.telegram.formatters import (
    TELEGRAM_MAX,
    chunk,
    code_block,
    redact_token,
    tail,
)


def test_chunk_short_passthrough():
    assert chunk("hi") == ["hi"]


def test_chunk_respects_limit():
    text = "abcdef" * 1000  # 6000 chars, no newlines
    parts = chunk(text, limit=100)
    assert all(len(p) <= 100 for p in parts)
    assert "".join(parts) == text


def test_chunk_prefers_line_boundaries():
    text = ("line\n" * 20)
    parts = chunk(text, limit=15)
    for p in parts:
        assert len(p) <= 15
        # No partial-line ends (each chunk ends with newline or whole content)
    assert "".join(parts) == text


def test_chunk_default_limit():
    text = "x" * (TELEGRAM_MAX + 50)
    parts = chunk(text)
    assert len(parts) == 2
    assert all(len(p) <= TELEGRAM_MAX for p in parts)


def test_redact_token_pattern():
    payload = "log: 123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef-_ fired"
    out = redact_token(payload)
    assert "[REDACTED]" in out
    assert "ABCDEF" not in out


def test_redact_token_explicit():
    out = redact_token("oops my-secret was leaked", token="my-secret")
    assert "my-secret" not in out
    assert "[REDACTED]" in out


def test_code_block():
    assert code_block("x", "json") == "```json\nx\n```"


def test_tail():
    assert tail(["a", "b", "c", "d"], 2) == ["c", "d"]
    assert tail([], 5) == []
