"""Tests for clk_harness.pricing — the per-provider cost estimator."""

from clk_harness import pricing


def test_known_provider_model_resolves_to_table():
    p = pricing.lookup("claude", "claude-sonnet-4-5")
    assert p.input_per_1k > 0
    assert p.output_per_1k > 0


def test_unknown_model_falls_back_to_provider_default():
    p = pricing.lookup("claude", "totally-made-up-model")
    assert p == pricing.DEFAULT_PRICING["claude"]["_default"]


def test_unknown_provider_returns_zero():
    p = pricing.lookup("does-not-exist", "x")
    assert p.input_per_1k == 0.0
    assert p.output_per_1k == 0.0


def test_local_providers_are_free():
    for name in ("shell", "ollama"):
        p = pricing.lookup(name, "anything")
        assert p.input_per_1k == 0.0
        assert p.output_per_1k == 0.0


def test_per_provider_override():
    p = pricing.lookup("pi", "x", {"pricing": {"input_per_1k": 0.5, "output_per_1k": 1.5}})
    assert p.input_per_1k == 0.5
    assert p.output_per_1k == 1.5


def test_per_model_override_beats_provider_override():
    overrides = {
        "pricing": {"input_per_1k": 0.5, "output_per_1k": 1.5},
        "pricing_by_model": {"my-model": {"input_per_1k": 9.0, "output_per_1k": 0.0}},
    }
    assert pricing.lookup("pi", "my-model", overrides).input_per_1k == 9.0
    assert pricing.lookup("pi", "other-model", overrides).input_per_1k == 0.5


def test_estimate_usd_scales_linearly():
    # claude-sonnet-4-5: $0.003 in / $0.015 out per 1k tokens
    # 2k in + 1k out = $0.006 + $0.015 = $0.021
    cost = pricing.estimate_usd("claude", "claude-sonnet-4-5", 2000, 1000)
    assert abs(cost - 0.021) < 1e-6


def test_estimate_usd_is_zero_for_local_providers():
    assert pricing.estimate_usd("ollama", "llama3.1", 100_000, 100_000) == 0.0
    assert pricing.estimate_usd("shell", None, 100_000, 100_000) == 0.0


def test_format_usd_zero():
    assert pricing.format_usd(0.0) == "$0.00"
    assert pricing.format_usd(-1.0) == "$0.00"


def test_format_usd_sub_cent_uses_three_decimals():
    assert pricing.format_usd(0.005) == "$0.005"
    assert pricing.format_usd(0.001) == "$0.001"


def test_format_usd_normal_amount():
    assert pricing.format_usd(0.42) == "$0.42"
    assert pricing.format_usd(12.34) == "$12.34"
