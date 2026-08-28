"""Documentation-consistency tests.

These assertions exist so that future edits don't let the docs
(``docs/MISSIONS.md`` / ``docs/CONFIGURATION.md``, split out of the
README), ``.env.example``, the ``DEFAULT_CLK_CONFIG`` schema, and the
kickoff env-var mapping (``clk_harness/kickoff.py``, which the
``kickoff.sh`` wrapper delegates to) drift apart. They run against the literal file
contents — no harness behavior is exercised.

When a knob is added or renamed, the change must touch four places:

1. ``clk_harness/config.py::DEFAULT_CLK_CONFIG`` — the schema.
2. ``.env.example`` — the user-facing override.
3. ``clk_harness/kickoff.py`` — translation from env var → JSON key.
4. ``docs/MISSIONS.md`` (Robustness loops) or ``docs/CONFIGURATION.md``
   (Cost guardrails) — the user-facing knob documentation.

The tests below check (1) ↔ (2) ↔ (4) for the new ``robustness`` block
specifically, and that (3) sees the same env-var family. The "prior
knobs" block in ``.env.example`` is checked for the env-var names
documented there to ensure the docs-parity pass covers them too.
"""

from __future__ import annotations

from pathlib import Path

from clk_harness.config import DEFAULT_CLK_CONFIG

REPO = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = (REPO / ".env.example").read_text(encoding="utf-8")
# kickoff.sh is a thin wrapper now; the env-var → config mapping lives in
# the `clk kickoff` subcommand.
KICKOFF = (REPO / "clk_harness" / "kickoff.py").read_text(encoding="utf-8")
# The README's deep-dive sections were split into docs/; the robustness
# documentation now lives in MISSIONS.md (Robustness loops) and
# CONFIGURATION.md (Cost guardrails / multiplier table).
MISSIONS_DOC = (REPO / "docs" / "MISSIONS.md").read_text(encoding="utf-8")
CONFIG_DOC = (REPO / "docs" / "CONFIGURATION.md").read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# robustness block: round-trip schema ↔ env vars ↔ README
# ---------------------------------------------------------------------------


# Some keys are introspected at runtime by name (e.g. "plateau_action")
# while others are knobs the user only sees on a coarser axis (the docs
# explain plateau as a single concept rather than enumerating
# `plateau_window` and `plateau_action` separately). Keys listed here are
# excluded from the docs-mention check below; everything else must
# appear by name in the Robustness-loops or Cost-guardrails section.
_README_OPTIONAL_KEYS: frozenset = frozenset({
    "qa_parallel_judges",  # internal cap, documented as part of the Q&A protocol
})


def test_robustness_defaults_exist() -> None:
    """DEFAULT_CLK_CONFIG must carry a ``robustness`` block."""
    assert "robustness" in DEFAULT_CLK_CONFIG
    block = DEFAULT_CLK_CONFIG["robustness"]
    # Minimum set of keys the layers require to function.
    required = {
        "auto_consensus",
        "auto_refine",
        "max_quality_retries",
        "min_response_chars",
        "refine_max_rounds",
        "refine_accept_threshold",
        "max_qa_depth",
        "plateau_window",
        "plateau_action",
    }
    missing = required - set(block.keys())
    assert not missing, f"DEFAULT_CLK_CONFIG['robustness'] missing keys: {missing}"


def _env_var_for(key: str) -> str:
    return "CLK_ROBUSTNESS_" + key.upper()


def test_env_example_documents_every_robustness_key() -> None:
    """Every robustness key in DEFAULT_CLK_CONFIG must appear as a
    CLK_ROBUSTNESS_* line in .env.example so users can override it."""
    block = DEFAULT_CLK_CONFIG["robustness"]
    for key in block.keys():
        var = _env_var_for(key)
        assert var in ENV_EXAMPLE, (
            f"{var} not documented in .env.example "
            f"(needed because DEFAULT_CLK_CONFIG['robustness']['{key}'] "
            "is now a public knob)"
        )


def test_kickoff_sh_maps_every_robustness_key() -> None:
    """The kickoff env-var override block (clk_harness/kickoff.py) must recognise every
    CLK_ROBUSTNESS_* variable."""
    block = DEFAULT_CLK_CONFIG["robustness"]
    for key in block.keys():
        var = _env_var_for(key)
        assert var in KICKOFF, (
            f"{var} not handled in the kickoff env→config mapping (clk_harness/kickoff.py) "
            f"(needed to make the .env.example override actually take effect)"
        )


def test_readme_documents_robustness_keys() -> None:
    """The docs' Robustness-loops (docs/MISSIONS.md) or Cost-guardrails
    (docs/CONFIGURATION.md) section must mention every robustness knob
    by name."""
    # Slice off the relevant chunk so we don't count incidental mentions
    # elsewhere (e.g. in the changelog "What's new" block which is
    # already lossier on purpose).
    rl_start = MISSIONS_DOC.find("## Robustness loops")
    rl_end_marker = "## Completion criteria"
    rl_end = MISSIONS_DOC.find(rl_end_marker, rl_start) if rl_start != -1 else -1
    assert rl_start != -1 and rl_end != -1, (
        "docs/MISSIONS.md is missing the ## Robustness loops section "
        "(must live between ## Loops and ## Completion criteria)"
    )
    rl_section = MISSIONS_DOC[rl_start:rl_end]

    cost_start = CONFIG_DOC.find("### Robustness-loop multipliers")
    cost_end_marker = "## Customization"
    cost_end = CONFIG_DOC.find(cost_end_marker, cost_start) if cost_start != -1 else -1
    assert cost_start != -1 and cost_end != -1, (
        "docs/CONFIGURATION.md is missing the ### Robustness-loop multipliers "
        "section under ## Cost guardrails"
    )
    cost_section = CONFIG_DOC[cost_start:cost_end]
    combined = rl_section + "\n" + cost_section

    block = DEFAULT_CLK_CONFIG["robustness"]
    for key in block.keys():
        if key in _README_OPTIONAL_KEYS:
            continue
        assert key in combined, (
            f"robustness key '{key}' not mentioned in the docs "
            "(docs/MISSIONS.md Robustness-loops section or "
            "docs/CONFIGURATION.md Cost-guardrails table)"
        )


# ---------------------------------------------------------------------------
# New config blocks: env-var + kickoff parity
# ---------------------------------------------------------------------------
# The autonomy blocks must be overridable from the environment the same way
# robustness is: every key gets a CLK_<BLOCK>_<KEY> var in .env.example AND a
# matching mapping in clk_harness/kickoff.py, so an override actually takes effect.

_AUTONOMY_BLOCKS = ("mission", "done_gate", "noop_guard", "deliberation")


def _block_env_var(block: str, key: str) -> str:
    return "CLK_" + block.upper() + "_" + key.upper()


def test_env_example_documents_new_blocks() -> None:
    for block in _AUTONOMY_BLOCKS:
        assert block in DEFAULT_CLK_CONFIG, f"DEFAULT_CLK_CONFIG missing '{block}'"
        for key in DEFAULT_CLK_CONFIG[block].keys():
            var = _block_env_var(block, key)
            assert var in ENV_EXAMPLE, (
                f"{var} not documented in .env.example (DEFAULT_CLK_CONFIG"
                f"['{block}']['{key}'] is a public knob)"
            )


def test_kickoff_sh_maps_new_blocks() -> None:
    for block in _AUTONOMY_BLOCKS:
        for key in DEFAULT_CLK_CONFIG[block].keys():
            var = _block_env_var(block, key)
            assert var in KICKOFF, (
                f"{var} not handled in the kickoff env→config mapping (clk_harness/kickoff.py)"
            )


def test_validation_auto_derive_wired() -> None:
    assert "CLK_VALIDATION_AUTO_DERIVE" in ENV_EXAMPLE
    assert "CLK_VALIDATION_AUTO_DERIVE" in KICKOFF


# ---------------------------------------------------------------------------
# Prior-knob parity (just an inventory check)
# ---------------------------------------------------------------------------


_PRIOR_KNOBS = (
    "CLK_PROVIDER_TIMEOUT_S",
    "CLK_PROVIDER_NO_OUTPUT_TIMEOUT_S",
    "CLK_PROVIDER_RETRY_MAX_RETRIES",
    "CLK_PROVIDER_RETRY_BACKOFF_S",
    "CLK_PROVIDER_RETRY_STAGE_MAX_RETRIES",
    "CLK_PROVIDER_RETRY_STAGE_BACKOFF_S",
    "CLK_SUPERVISE_MAX_CYCLES",
    "CLK_CONSENSUS_MAX_SAMPLES",
    "CLK_CONSENSUS_MAX_PARALLEL",
    "CLK_CASTING_MAX_DYNAMIC_ROLES",
    "CLK_AUTO_COMMIT",
    "CLK_VALIDATION_MAX_FILES_PER_BATCH",
    "CLK_VALIDATION_WARN_FILES_PER_BATCH",
    "CLK_META_PROMPT_DISPATCH",
    "CLK_META_PROMPT_ROLE",
    "CLK_REVIEW_PER_STAGE",
    "CLK_RECOVERY_MAX_PER_STAGE",
)


def test_env_example_documents_prior_knobs() -> None:
    """The 'Prior-knob reference' block in .env.example must enumerate
    every legacy CLK_* knob the kickoff mapper supports."""
    for var in _PRIOR_KNOBS:
        assert var in ENV_EXAMPLE, (
            f"{var} should appear in .env.example so users can see "
            "the full set of supported overrides in one place."
        )


def test_kickoff_handles_prior_knobs() -> None:
    """The kickoff env-var override block (clk_harness/kickoff.py) must recognise every prior
    CLK_* knob too — the docs claim parity, so the script must honor it."""
    for var in _PRIOR_KNOBS:
        assert var in KICKOFF, (
            f"{var} listed in .env.example but not handled by the kickoff env→config mapping — "
            "the override would silently no-op."
        )


# ---------------------------------------------------------------------------
# gauntlet block: the same four-place parity rule as robustness above
# ---------------------------------------------------------------------------

# GAUNTLET_LOOP is the documented short name for `enabled`; it does not follow
# the CLK_GAUNTLET_* pattern, so map it explicitly.
_GAUNTLET_ENV_ALIASES = {"enabled": ("GAUNTLET_LOOP", "CLK_ROBUSTNESS_GAUNTLET")}


def _gauntlet_env_vars_for(key: str) -> tuple:
    return _GAUNTLET_ENV_ALIASES.get(key, ("CLK_GAUNTLET_" + key.upper(),))


def test_gauntlet_defaults_exist() -> None:
    """DEFAULT_CLK_CONFIG must carry a ``gauntlet`` block."""
    assert "gauntlet" in DEFAULT_CLK_CONFIG, (
        "DEFAULT_CLK_CONFIG lost its gauntlet block (layer 12)"
    )
    required = {
        "enabled",
        "preset",
        "max_rounds",
        "scope",
        "critic",
        "answer_key",
        "final_verification",
        "accept_threshold",
        "supersede_auto_refine",
    }
    missing = required - set(DEFAULT_CLK_CONFIG["gauntlet"].keys())
    assert not missing, f"DEFAULT_CLK_CONFIG['gauntlet'] missing keys: {missing}"


def test_gauntlet_defaults_to_on_with_the_standard_preset() -> None:
    """The documented default. Changing it is a user-visible cost change."""
    block = DEFAULT_CLK_CONFIG["gauntlet"]
    assert block["enabled"] is True
    assert block["preset"] == "standard"


def test_env_example_documents_every_gauntlet_key() -> None:
    for key in DEFAULT_CLK_CONFIG["gauntlet"].keys():
        variants = _gauntlet_env_vars_for(key)
        assert any(v in ENV_EXAMPLE for v in variants), (
            f"none of {variants} documented in .env.example "
            f"(needed because DEFAULT_CLK_CONFIG['gauntlet']['{key}'] "
            "is a public knob)"
        )


def test_kickoff_maps_every_gauntlet_key() -> None:
    for key in DEFAULT_CLK_CONFIG["gauntlet"].keys():
        variants = _gauntlet_env_vars_for(key)
        assert any(v in KICKOFF for v in variants), (
            f"none of {variants} handled in the kickoff env→config mapping "
            "(clk_harness/kickoff.py), so the .env.example override would "
            "never take effect"
        )


def test_docs_document_every_gauntlet_key() -> None:
    """Every gauntlet knob must be named in MISSIONS.md or CONFIGURATION.md."""
    rl_start = MISSIONS_DOC.find("## Robustness loops")
    rl_end = MISSIONS_DOC.find("## Completion criteria", rl_start)
    assert rl_start != -1 and rl_end != -1
    missions_section = MISSIONS_DOC[rl_start:rl_end]
    assert "Gauntlet loop" in missions_section, (
        "docs/MISSIONS.md is missing the gauntlet layer under ## Robustness loops"
    )

    cost_start = CONFIG_DOC.find("## Cost guardrails")
    cost_end = CONFIG_DOC.find("## Customization", cost_start)
    assert cost_start != -1 and cost_end != -1
    config_section = CONFIG_DOC[cost_start:cost_end]

    combined = missions_section + config_section
    for key in DEFAULT_CLK_CONFIG["gauntlet"].keys():
        assert key in combined, (
            f"gauntlet knob '{key}' is not documented by name in "
            "docs/MISSIONS.md (Robustness loops) or "
            "docs/CONFIGURATION.md (Cost guardrails)"
        )


def test_kill_switches_are_documented() -> None:
    """All four documented off-switches must actually appear in the docs.

    The gauntlet is on by default and costs tokens, so a user who wants it
    off must be able to find out how without reading the source.
    """
    combined = MISSIONS_DOC + CONFIG_DOC + ENV_EXAMPLE
    for switch in ("--no-gauntlet", "GAUNTLET_LOOP", "/gauntlet off", "/clk-gauntlet"):
        assert switch in combined, f"kill switch '{switch}' is undocumented"


# ---------------------------------------------------------------------------
# Install-script narration: spot-check the doc comment is present
# ---------------------------------------------------------------------------


def test_install_local_header_mentions_layout() -> None:
    """The install_local.sh header should describe the layout the script
    creates — that's the only place that documentation lives."""
    text = (REPO / "scripts" / "install_local.sh").read_text(encoding="utf-8")
    for needle in (
        ".clk/venv",
        ".clk/site-packages",
        "CLK_PROJECT_ROOT",
        "pyproject.toml",
        "WHAT THIS SCRIPT DOES",
    ):
        assert needle in text, (
            f"scripts/install_local.sh header lost reference to '{needle}'"
        )


def test_run_loop_header_links_robustness_section() -> None:
    """scripts/run_loop.sh should point users at the docs section so
    the wrapper isn't a black box."""
    text = (REPO / "scripts" / "run_loop.sh").read_text(encoding="utf-8")
    assert "Robustness loops" in text or "robustness" in text.lower(), (
        "scripts/run_loop.sh should cross-reference the README's "
        "Robustness-loops section so callers know what wraps each iteration."
    )
