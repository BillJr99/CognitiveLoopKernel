"""Tests for the gauntlet loop (layer 12).

Mirrored by ``pi-extension/tests/gauntlet.test.ts`` so a behaviour drift
between the Python harness and the TypeScript extension shows up on both
sides — the presets, round caps, env-var precedence, boolean token sets,
and the ``VERDICT`` / ``SCORE`` / ``MATERIAL_DEFECTS`` contract are shared.

Three layers are covered:

* pure parsing and settings resolution (no provider, no I/O)
* the loop itself against a canned provider
* the wiring into ``AgentRunner.run`` and the workflow's auto-refine pass
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from clk_harness.config import DEFAULT_CLK_CONFIG, Paths
from clk_harness.orchestration import gauntlet as g
from clk_harness.orchestration import workflow as wf
from clk_harness.orchestration.agent import AgentRunner
from clk_harness.providers.base import AgentResponse

# A response comfortably above the quality validator's empty threshold.
GOOD = "This is a comfortably substantive response that clears the empty threshold."

# Critic replies: one that converges, one that demands another round.
CLEAN = "Nothing material found.\nMATERIAL_DEFECTS: 0\nVERDICT: accept\nSCORE: 0.95"
DIRTY = "The error path is untested.\nMATERIAL_DEFECTS: 2\nVERDICT: revise\nSCORE: 0.40"

# Each stage's objective opens with a `GAUNTLET :: <stage>` header. Match on
# that rather than on prose: the critique and verification bodies both
# mention "acceptance answer key" and "revisions" in passing.
M_KEY = "GAUNTLET :: acceptance answer key"
M_CRITIQUE = "GAUNTLET :: adversarial critique"
M_REVISE = "GAUNTLET :: revision"
M_VERIFY = "GAUNTLET :: final verification"
M_REPAIR = "GAUNTLET :: final repair"


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


# ---------------------------------------------------------------------------
# parse_bool
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("token", ["1", "true", "TRUE", " yes ", "y", "on", "enabled"])
def test_parse_bool_truthy(token: str) -> None:
    assert g.parse_bool(token, False) is True


@pytest.mark.parametrize("token", ["0", "false", "False", "no", "n", "off", "disabled"])
def test_parse_bool_falsy(token: str) -> None:
    assert g.parse_bool(token, True) is False


def test_parse_bool_keeps_default_on_junk() -> None:
    """A typo must not silently disable a loop that defaults to on.

    This is the whole reason the gauntlet does not reuse ``kickoff._bool``,
    which maps every unrecognized string to False.
    """
    assert g.parse_bool("ture", True) is True
    assert g.parse_bool("", True) is True
    assert g.parse_bool(None, True) is True
    # ...and it does not flip a default-off knob on either.
    assert g.parse_bool("ture", False) is False


# ---------------------------------------------------------------------------
# resolve_settings
# ---------------------------------------------------------------------------


def test_defaults_are_on_and_standard() -> None:
    s = g.resolve_settings({}, env={})
    assert s.enabled is True
    assert s.preset == "standard"
    assert s.rounds == 3


def test_config_block_defaults_match_the_dataclass() -> None:
    """DEFAULT_CLK_CONFIG and the module's fallback must not drift."""
    cfg = DEFAULT_CLK_CONFIG["gauntlet"]
    s = g.resolve_settings({"gauntlet": cfg}, env={})
    assert s.enabled is cfg["enabled"]
    assert s.preset == cfg["preset"]
    assert s.scope == cfg["scope"]
    assert s.critic == cfg["critic"]
    assert s.accept_threshold == cfg["accept_threshold"]
    assert s.supersede_auto_refine is cfg["supersede_auto_refine"]
    assert s.exclude_agents == cfg["exclude_agents"]


@pytest.mark.parametrize("value", ["False", "false", "0", "off", "no"])
def test_gauntlet_loop_env_disables(value: str) -> None:
    assert g.resolve_settings({}, env={"GAUNTLET_LOOP": value}).enabled is False


def test_robustness_family_env_disables() -> None:
    assert g.resolve_settings({}, env={"CLK_ROBUSTNESS_GAUNTLET": "off"}).enabled is False


def test_gauntlet_loop_wins_over_the_family_name() -> None:
    """GAUNTLET_LOOP is the documented short name and takes precedence."""
    env = {"CLK_ROBUSTNESS_GAUNTLET": "off", "GAUNTLET_LOOP": "true"}
    assert g.resolve_settings({}, env=env).enabled is True
    env = {"CLK_ROBUSTNESS_GAUNTLET": "on", "GAUNTLET_LOOP": "false"}
    assert g.resolve_settings({}, env=env).enabled is False


def test_env_overrides_the_config_file() -> None:
    cfg = {"gauntlet": {"enabled": True}}
    assert g.resolve_settings(cfg, env={"GAUNTLET_LOOP": "false"}).enabled is False


def test_cli_override_beats_everything() -> None:
    cfg = {"gauntlet": {"enabled": True}}
    env = {"GAUNTLET_LOOP": "true"}
    s = g.resolve_settings(cfg, env=env, cli_override={"enabled": False})
    assert s.enabled is False


def test_junk_env_value_does_not_disable() -> None:
    assert g.resolve_settings({}, env={"GAUNTLET_LOOP": "ture"}).enabled is True


@pytest.mark.parametrize("preset,rounds", [("quick", 1), ("standard", 3), ("rigorous", 5)])
def test_preset_round_caps(preset: str, rounds: int) -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_PRESET": preset})
    assert s.preset == preset
    assert s.rounds == rounds


def test_unknown_preset_falls_back_to_standard() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_PRESET": "nonsense"})
    assert s.preset == "standard"
    assert s.rounds == 3


def test_explicit_max_rounds_beats_the_preset() -> None:
    s = g.resolve_settings(
        {}, env={"CLK_GAUNTLET_PRESET": "quick", "CLK_GAUNTLET_MAX_ROUNDS": "4"},
    )
    assert s.rounds == 4


def test_junk_numeric_env_falls_back() -> None:
    s = g.resolve_settings(
        {}, env={"CLK_GAUNTLET_MAX_ROUNDS": "lots", "CLK_GAUNTLET_ACCEPT_THRESHOLD": "high"},
    )
    assert s.rounds == 3
    assert s.accept_threshold == 0.8


def test_focus_lenses_extend_the_preset() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_FOCUS": "accessibility, i18n"})
    lenses = s.lenses
    assert "accessibility" in lenses and "i18n" in lenses
    assert "correctness" in lenses


def test_csv_env_parses_exclude_agents() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_EXCLUDE_AGENTS": "critic, qa"})
    assert s.exclude_agents == ["critic", "qa"]


# ---------------------------------------------------------------------------
# enabled_for
# ---------------------------------------------------------------------------


def test_enabled_for_respects_the_kill_switch() -> None:
    s = g.resolve_settings({}, env={"GAUNTLET_LOOP": "false"})
    assert g.enabled_for(s, "engineer") is False


def test_enabled_for_excludes_the_critic() -> None:
    """The critic must not be put through its own gauntlet."""
    s = g.resolve_settings({}, env={})
    assert g.enabled_for(s, "critic") is False
    assert g.enabled_for(s, "engineer") is True


def test_enabled_for_scope_careful_only() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_SCOPE": "careful_only"})
    assert g.enabled_for(s, "engineer", {"careful": True}) is True
    assert g.enabled_for(s, "engineer", {"careful": False}) is False
    assert g.enabled_for(s, "engineer", {}) is False


def test_enabled_for_scope_producing_only() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_SCOPE": "producing_only"})
    assert g.enabled_for(s, "engineer", {"stage_outputs": ["spec"]}) is True
    assert g.enabled_for(s, "engineer", {"commit": True}) is True
    assert g.enabled_for(s, "engineer", {"commit": False}) is False


def test_enabled_for_scope_all_is_the_default() -> None:
    s = g.resolve_settings({}, env={})
    assert s.scope == "all"
    assert g.enabled_for(s, "engineer", {}) is True
    assert g.enabled_for(s, "chief", {}) is True


# ---------------------------------------------------------------------------
# parse_answer_key
# ---------------------------------------------------------------------------


def test_parse_answer_key_extracts_checks() -> None:
    checks = g.parse_answer_key(
        "Here is my plan.\n\n"
        "ANSWER_KEY:\n"
        "- tests_pass: `pytest -q` exits 0\n"
        "- handles_404: unknown ids return 404, not 500\n"
        "END_ANSWER_KEY\n\n"
        "Rest of the work."
    )
    assert [c.id for c in checks] == ["tests_pass", "handles_404"]
    assert checks[1].condition == "unknown ids return 404, not 500"


def test_parse_answer_key_missing_block() -> None:
    assert g.parse_answer_key("just some prose") == []
    assert g.parse_answer_key("") == []


def test_parse_answer_key_skips_unparseable_lines() -> None:
    """A bad line is skipped, not fatal to the whole key."""
    checks = g.parse_answer_key(
        "ANSWER_KEY:\n- ok_check: it works\n!!! garbage !!!\n# comment\n- b: fine\nEND_ANSWER_KEY"
    )
    assert [c.id for c in checks] == ["ok_check", "b"]


def test_parse_answer_key_dedupes_ids() -> None:
    checks = g.parse_answer_key("ANSWER_KEY:\n- a: first\n- A: second\nEND_ANSWER_KEY")
    assert len(checks) == 1
    assert checks[0].condition == "first"


def test_render_answer_key() -> None:
    assert g.render_answer_key([g.AnswerKeyCheck("a", "b")]) == "- a: b"
    assert "no acceptance checks" in g.render_answer_key([])


# ---------------------------------------------------------------------------
# parse_critique
# ---------------------------------------------------------------------------


def test_parse_critique_reads_all_three_fields() -> None:
    c = g.parse_critique(DIRTY)
    assert c.verdict == "revise"
    assert c.score == pytest.approx(0.4)
    assert c.material_defects == 2
    assert c.converged is False


def test_parse_critique_clean_converges() -> None:
    assert g.parse_critique(CLEAN).converged is True


def test_zero_material_defects_converges_without_accept() -> None:
    """"A clean critique is a valid outcome" — nits alone buy no more rounds."""
    c = g.parse_critique("Some nits.\nMATERIAL_DEFECTS: 0\nVERDICT: revise\nSCORE: 0.9")
    assert c.converged is True


def test_accept_below_threshold_is_not_an_accept() -> None:
    c = g.parse_critique("VERDICT: accept\nSCORE: 0.30", accept_threshold=0.8)
    assert c.verdict == "revise"
    assert c.material_defects >= 1
    assert c.converged is False


def test_missing_score_defaults_by_verdict() -> None:
    assert g.parse_critique("VERDICT: accept").score == pytest.approx(1.0)
    assert g.parse_critique("VERDICT: revise").score == pytest.approx(0.4)


def test_unparseable_critique_asks_for_revision() -> None:
    """Fail closed: a critic that said nothing structured has not accepted."""
    c = g.parse_critique("the critic rambled and said nothing structured")
    assert c.verdict == "revise"
    assert c.converged is False


def test_reject_never_converges() -> None:
    c = g.parse_critique("MATERIAL_DEFECTS: 0\nVERDICT: reject\nSCORE: 0.1")
    assert c.converged is False


def test_score_is_clamped() -> None:
    assert g.parse_critique("VERDICT: accept\nSCORE: 7").score == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# Objective builders
# ---------------------------------------------------------------------------


def test_objectives_carry_their_stage_header_and_the_key() -> None:
    s = g.resolve_settings({}, env={})
    checks = [g.AnswerKeyCheck("a", "it works")]
    crit = g.parse_critique(DIRTY)
    assert g.build_key_objective("do X", s).startswith(M_KEY)
    assert M_CRITIQUE in g.build_critique_objective("do X", GOOD, checks, s, 1)
    assert M_REVISE in g.build_revise_objective("do X", crit, checks, s, 1)
    assert M_VERIFY in g.build_verify_objective("do X", GOOD, checks, s)
    assert M_REPAIR in g.build_repair_objective("do X", crit, checks)
    # Every judged stage must quote the answer key it judges against.
    for text in (
        g.build_critique_objective("do X", GOOD, checks, s, 1),
        g.build_verify_objective("do X", GOOD, checks, s),
        g.build_repair_objective("do X", crit, checks),
    ):
        assert "- a: it works" in text


def test_critique_objective_quotes_the_preset_lenses() -> None:
    s = g.resolve_settings({}, env={"CLK_GAUNTLET_PRESET": "rigorous"})
    text = g.build_critique_objective("do X", GOOD, [], s, 1)
    assert "counterexamples" in text
    assert "1/5" in text, "the round counter should show the preset's cap"


def test_long_candidates_are_truncated_for_the_critique_prompt() -> None:
    s = g.resolve_settings({}, env={})
    text = g.build_critique_objective("do X", "x" * 10_000, [], s, 1)
    assert "truncated" in text
    assert len(text) < 10_000


# ---------------------------------------------------------------------------
# The loop, against a canned provider
# ---------------------------------------------------------------------------


class _ScriptedProvider:
    """Answers based on which gauntlet stage the objective belongs to.

    Lets a test assert on *which* stages ran without depending on the
    exact ordering of a flat response list.
    """

    def __init__(self, **by_stage: str) -> None:
        self.by_stage = by_stage
        self.objectives: List[str] = []

    def describe(self) -> str:
        return "scripted"

    def invoke(self, req):  # noqa: ANN001
        obj = req.prompt or ""
        self.objectives.append(obj)
        for marker, key in (
            (M_KEY, "key"), (M_CRITIQUE, "critique"), (M_REVISE, "revise"),
            (M_VERIFY, "verify"), (M_REPAIR, "repair"),
        ):
            if marker in obj:
                return AgentResponse(ok=True, text=self.by_stage.get(key, GOOD))
        return AgentResponse(ok=True, text=self.by_stage.get("worker", GOOD))

    def count(self, marker: str) -> int:
        return sum(1 for o in self.objectives if marker in o)


def _runner(paths: Paths, provider: Any, overrides: Dict[str, Any] | None = None) -> AgentRunner:
    agents_cfg = {
        "agents": {
            "chief": {"prompt": "chief.md", "provider": None, "role": "casting"},
            "qa": {"prompt": "qa.md", "provider": None, "role": "validator"},
            "ralph": {"prompt": "ralph.md", "provider": None, "role": "loop"},
            "engineer": {"prompt": "engineer.md", "provider": None, "role": "implementer"},
            "critic": {"prompt": "critic.md", "provider": None, "role": "judge"},
        }
    }
    providers_cfg = {"active": "fake", "providers": {"fake": {"type": "fake"}}}
    clk_cfg: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CLK_CONFIG))
    clk_cfg["dry_run"] = False
    clk_cfg["provider_retry"] = {"max_retries": 0, "backoff_s": 0}
    # Isolate the gauntlet from the layers beneath it so the dispatch counts
    # below describe the gauntlet alone.
    clk_cfg["robustness"] = {
        **DEFAULT_CLK_CONFIG["robustness"],
        "auto_consensus": "off",
        "auto_refine": "off",
        "max_quality_retries": 0,
    }
    clk_cfg["noop_guard"] = {"enabled": False}
    if overrides:
        clk_cfg.update(overrides)
    runner = AgentRunner(paths, agents_cfg, providers_cfg, clk_cfg)
    runner.get_provider = lambda name=None: provider  # type: ignore[method-assign]
    return runner


def _loop(runner: AgentRunner, paths: Paths, **env: str) -> g.GauntletLoop:
    return g.GauntletLoop(runner, g.resolve_settings(runner.clk_cfg, env=env), paths)


def _candidate(text: str = GOOD):
    from clk_harness.orchestration.agent import AgentRun

    return AgentRun(
        agent="engineer",
        objective="build it",
        response=AgentResponse(ok=True, text=text),
        started_at="t0",
        finished_at="t1",
    )


def test_loop_stops_after_one_round_on_a_clean_critique(paths: Paths) -> None:
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN, key="ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY")
    runner = _runner(paths, provider)
    # rigorous caps at 5, but a clean critique should stop at 1.
    loop = _loop(runner, paths, CLK_GAUNTLET_PRESET="rigorous")
    final = loop.run("engineer", "build it", _candidate())
    assert provider.count(M_CRITIQUE) == 1
    assert provider.count(M_REVISE) == 0
    assert final.response.text == GOOD


def test_loop_revises_and_stops_at_the_round_cap(paths: Paths) -> None:
    provider = _ScriptedProvider(
        critique=DIRTY, verify=CLEAN, revise="revised " + GOOD,
        key="ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY",
    )
    runner = _runner(paths, provider)
    loop = _loop(runner, paths, CLK_GAUNTLET_PRESET="quick")  # cap 1
    final = loop.run("engineer", "build it", _candidate())
    assert provider.count(M_CRITIQUE) == 1
    assert provider.count(M_REVISE) == 1
    assert final.response.text.startswith("revised ")


@pytest.mark.parametrize("preset,expected", [("quick", 1), ("standard", 3), ("rigorous", 5)])
def test_loop_honors_the_preset_cap(paths: Paths, preset: str, expected: int) -> None:
    provider = _ScriptedProvider(critique=DIRTY, verify=CLEAN, revise=GOOD + " r")
    runner = _runner(paths, provider)
    loop = _loop(runner, paths, CLK_GAUNTLET_PRESET=preset)
    loop.run("engineer", "build it", _candidate())
    assert provider.count(M_CRITIQUE) == expected


def test_loop_reuses_the_workers_own_answer_key(paths: Paths) -> None:
    """A key the worker already wrote is free; do not pay to derive one."""
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    loop = _loop(runner, paths)
    candidate = _candidate("ANSWER_KEY:\n- mine: the worker wrote this\nEND_ANSWER_KEY\n\n" + GOOD)
    loop.run("engineer", "build it", candidate)
    assert provider.count(M_KEY) == 0


def test_loop_derives_a_key_when_the_worker_supplied_none(paths: Paths) -> None:
    provider = _ScriptedProvider(
        key="ANSWER_KEY:\n- derived: from the critic\nEND_ANSWER_KEY",
        critique=CLEAN, verify=CLEAN,
    )
    runner = _runner(paths, provider)
    loop = _loop(runner, paths)
    loop.run("engineer", "build it", _candidate())
    assert provider.count(M_KEY) == 1
    # The derived check must reach the critique prompt.
    critique_objs = [o for o in provider.objectives if M_CRITIQUE in o]
    assert "- derived: from the critic" in critique_objs[0]


def test_answer_key_can_be_turned_off(paths: Paths) -> None:
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    loop = _loop(runner, paths, CLK_GAUNTLET_ANSWER_KEY="false")
    loop.run("engineer", "build it", _candidate())
    assert provider.count(M_KEY) == 0


def test_loop_repairs_once_when_verification_finds_a_defect(paths: Paths) -> None:
    provider = _ScriptedProvider(
        critique=CLEAN, verify=DIRTY, repair="repaired " + GOOD,
        key="ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY",
    )
    runner = _runner(paths, provider)
    loop = _loop(runner, paths)
    final = loop.run("engineer", "build it", _candidate())
    assert provider.count(M_REPAIR) == 1, "exactly one bounded repair, never a second"
    assert final.response.text.startswith("repaired ")


def test_final_verification_can_be_turned_off(paths: Paths) -> None:
    provider = _ScriptedProvider(critique=CLEAN)
    runner = _runner(paths, provider)
    loop = _loop(runner, paths, CLK_GAUNTLET_FINAL_VERIFICATION="false")
    loop.run("engineer", "build it", _candidate())
    assert provider.count(M_VERIFY) == 0


def test_loop_skips_a_failed_candidate(paths: Paths) -> None:
    from clk_harness.orchestration.agent import AgentRun

    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    loop = _loop(runner, paths)
    failed = AgentRun(
        agent="engineer", objective="build it",
        response=AgentResponse(ok=False, text="", error="boom"),
        started_at="t0", finished_at="t1",
    )
    final = loop.run("engineer", "build it", failed)
    assert final is failed
    assert provider.objectives == [], "nothing to critique — no dispatches"


def test_loop_skips_an_empty_candidate(paths: Paths) -> None:
    """Critiquing an empty string just burns tokens to rediscover it is empty."""
    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    loop = _loop(runner, paths)
    final = loop.run("engineer", "build it", _candidate("   "))
    assert provider.objectives == []
    assert final.response.text == "   "


def test_loop_keeps_the_candidate_when_the_critic_raises(paths: Paths) -> None:
    class _Exploding:
        def describe(self) -> str:
            return "exploding"

        def invoke(self, req):  # noqa: ANN001
            raise RuntimeError("provider exploded")

    runner = _runner(paths, _Exploding())
    loop = _loop(runner, paths)
    candidate = _candidate()
    final = loop.run("engineer", "build it", candidate)
    assert final.response.text == GOOD, "a broken critic must never lose the work"


def test_loop_keeps_the_last_good_candidate_when_a_revision_fails(paths: Paths) -> None:
    class _FailRevisions:
        def describe(self) -> str:
            return "fail-revisions"

        def invoke(self, req):  # noqa: ANN001
            obj = req.prompt or ""
            if M_REVISE in obj:
                return AgentResponse(ok=False, text="", error="revision failed")
            if M_CRITIQUE in obj:
                return AgentResponse(ok=True, text=DIRTY)
            return AgentResponse(ok=True, text=GOOD)

    runner = _runner(paths, _FailRevisions())
    loop = _loop(runner, paths, CLK_GAUNTLET_FINAL_VERIFICATION="false")
    final = loop.run("engineer", "build it", _candidate())
    assert final.response.text == GOOD


def test_loop_falls_back_when_no_critic_is_cast(paths: Paths) -> None:
    """A dynamic roster may not have a critic; self-audit beats skipping."""
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    runner.agents_cfg = {"agents": {"engineer": {"prompt": "engineer.md"}}}
    loop = _loop(runner, paths)
    loop.run("engineer", "build it", _candidate())
    assert provider.count(M_CRITIQUE) == 1


# ---------------------------------------------------------------------------
# Session dispatch budget
# ---------------------------------------------------------------------------


def test_max_dispatches_defaults_to_500() -> None:
    assert g.resolve_settings({}, env={}).max_dispatches == 500
    assert DEFAULT_CLK_CONFIG["gauntlet"]["max_dispatches"] == 500


def test_max_dispatches_is_configurable() -> None:
    assert g.resolve_settings({}, env={"CLK_GAUNTLET_MAX_DISPATCHES": "42"}).max_dispatches == 42
    assert g.resolve_settings({"gauntlet": {"max_dispatches": 7}}, env={}).max_dispatches == 7
    s = g.resolve_settings({}, env={}, cli_override={"max_dispatches": 3})
    assert s.max_dispatches == 3


def test_max_dispatches_junk_env_falls_back() -> None:
    assert g.resolve_settings({}, env={"CLK_GAUNTLET_MAX_DISPATCHES": "many"}).max_dispatches == 500


def test_dispatch_budget_spends_and_exhausts() -> None:
    b = g.DispatchBudget(2)
    assert b.spend() is True
    assert b.spend() is True
    assert b.spend() is False
    assert b.exhausted is True
    b.reset()
    assert b.used == 0 and b.exhausted is False


def test_budget_exhaustion_is_reported_once() -> None:
    """A long run must not write hundreds of identical log lines."""
    b = g.DispatchBudget(1)
    assert b.claim_report() is True
    assert b.claim_report() is False
    assert b.claim_report() is False
    b.reset()
    assert b.claim_report() is True, "a fresh run reports again"


def test_loop_logs_budget_exhaustion_once(paths: Paths) -> None:
    import json as _json

    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    settings = g.resolve_settings(runner.clk_cfg, env={})
    budget = g.DispatchBudget(1)
    for _ in range(10):
        g.GauntletLoop(runner, settings, paths, budget=budget).run(
            "engineer", "build it", _candidate(),
        )
    log = paths.logs / "activity.jsonl"
    events = []
    if log.exists():
        for line in log.read_text(encoding="utf-8").splitlines():
            try:
                events.append(_json.loads(line).get("event"))
            except ValueError:
                pass
    assert events.count("gauntlet_budget_exhausted") == 1, (
        f"expected a single exhaustion notice, got {events.count('gauntlet_budget_exhausted')}"
    )


def test_dispatch_budget_zero_is_unlimited() -> None:
    b = g.DispatchBudget(0)
    for _ in range(1000):
        assert b.spend() is True
    assert b.exhausted is False


def test_loop_stops_spending_at_the_budget(paths: Paths) -> None:
    provider = _ScriptedProvider(
        critique=DIRTY, revise="revised " + GOOD, key="ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY",
    )
    runner = _runner(paths, provider)
    settings = g.resolve_settings(runner.clk_cfg, env={"CLK_GAUNTLET_PRESET": "rigorous"})
    # key + critique + revise = 3, then the budget stops the next critique.
    loop = g.GauntletLoop(runner, settings, paths, budget=g.DispatchBudget(3))
    final = loop.run("engineer", "build it", _candidate())
    assert len(provider.objectives) == 3, "must not dispatch past the budget"
    assert final.response.text.startswith("revised "), "work done before the cap is kept"


def test_budget_spans_dispatches_not_just_one(paths: Paths) -> None:
    """A round cap resets per dispatch; only a session budget bounds a run."""
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    settings = g.resolve_settings(runner.clk_cfg, env={})
    budget = g.DispatchBudget(3)
    for _ in range(5):
        loop = g.GauntletLoop(runner, settings, paths, budget=budget)
        loop.run("engineer", "build it", _candidate())
    assert budget.used <= 3, f"budget overspent: {budget.used}"


def test_runner_shares_one_budget_across_dispatches(paths: Paths) -> None:
    """Held on the runner, so a long mission cannot get a fresh cap per stage."""
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider, {
        "gauntlet": {**DEFAULT_CLK_CONFIG["gauntlet"], "max_dispatches": 4},
    })
    for _ in range(6):
        runner.run("engineer", "build it")
    budget = runner._gauntlet_budget(runner.gauntlet_settings())
    assert budget.used <= 4, f"budget overspent across dispatches: {budget.used}"


def test_runner_rebuilds_the_budget_when_the_limit_changes(paths: Paths) -> None:
    """The TUI's /gauntlet can retune mid-session; the cap must follow."""
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    first = runner._gauntlet_budget(runner.gauntlet_settings())
    assert first.limit == 500
    runner.clk_cfg["gauntlet"] = {**DEFAULT_CLK_CONFIG["gauntlet"], "max_dispatches": 9}
    second = runner._gauntlet_budget(runner.gauntlet_settings())
    assert second.limit == 9
    assert second is not first


# ---------------------------------------------------------------------------
# Wiring into AgentRunner.run
# ---------------------------------------------------------------------------


def test_runner_wraps_a_normal_dispatch(paths: Paths) -> None:
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN, key="ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY")
    runner = _runner(paths, provider)
    runner.run("engineer", "build it")
    assert provider.count(M_CRITIQUE) == 1


def test_runner_skips_the_gauntlet_when_disabled(paths: Paths, monkeypatch) -> None:
    monkeypatch.setenv("GAUNTLET_LOOP", "false")
    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    runner.run("engineer", "build it")
    assert provider.count(M_CRITIQUE) == 0
    assert len(provider.objectives) == 1, "exactly one dispatch, as before the gauntlet existed"


def test_runner_skips_the_gauntlet_via_config(paths: Paths) -> None:
    provider = _ScriptedProvider()
    runner = _runner(paths, provider, {"gauntlet": {"enabled": False}})
    runner.run("engineer", "build it")
    assert len(provider.objectives) == 1


def test_meta_phases_bypass_the_gauntlet(paths: Paths) -> None:
    """A gauntlet dispatch must not re-enter the gauntlet."""
    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    for phase in g.GAUNTLET_PHASES:
        assert phase in AgentRunner._META_PHASES, f"{phase} missing from the recursion firewall"
    provider.objectives.clear()
    runner.run("engineer", "build it", extra={"phase": g.PHASE_CRITIQUE})
    assert len(provider.objectives) == 1


def test_dry_run_bypasses_the_gauntlet(paths: Paths) -> None:
    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    runner.run("engineer", "build it", dry_run=True)
    assert provider.count(M_CRITIQUE) == 0


def test_runner_marks_the_run_when_the_gauntlet_ran(paths: Paths) -> None:
    """The workflow layer reads this to retire its auto_refine critic pass.

    The marker rides on the returned ``AgentRun``, not on the caller's
    ``extra`` dict — ``AgentRunner.run`` copies that dict, so a mutation
    there would never reach the workflow runner.
    """
    provider = _ScriptedProvider(critique=CLEAN, verify=CLEAN)
    runner = _runner(paths, provider)
    run = runner.run("engineer", "build it", extra={"stage_id": "implement"})
    assert run.gauntlet_ran is True


def test_run_is_not_marked_when_the_gauntlet_is_off(paths: Paths) -> None:
    provider = _ScriptedProvider()
    runner = _runner(paths, provider, {"gauntlet": {"enabled": False}})
    run = runner.run("engineer", "build it")
    assert run.gauntlet_ran is False


def test_runner_gauntlet_settings_reflects_live_config(paths: Paths) -> None:
    """The TUI's /gauntlet mutates clk_cfg in place; it must take effect."""
    provider = _ScriptedProvider()
    runner = _runner(paths, provider)
    assert runner.gauntlet_settings().enabled is True
    runner.clk_cfg["gauntlet"] = {"enabled": False}
    assert runner.gauntlet_settings().enabled is False


# ---------------------------------------------------------------------------
# supersede_auto_refine
# ---------------------------------------------------------------------------


def test_gauntlet_retires_the_auto_refine_pass(paths: Paths) -> None:
    provider = _ScriptedProvider()
    runner = _runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_refine": "all"},
    })
    wr = wf.WorkflowRunner(paths, runner)
    stage = wf.WorkflowStage(id="implement", agent="engineer", objective="build it")
    assert wr._refine_enabled(stage) is True, "auto_refine=all normally fires"
    assert wr._refine_enabled(stage, gauntlet_ran=True) is False


def test_explicit_refine_block_still_runs_under_the_gauntlet(paths: Paths) -> None:
    """An explicit refine: block is user intent and outranks the default."""
    provider = _ScriptedProvider()
    runner = _runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_refine": "all"},
    })
    wr = wf.WorkflowRunner(paths, runner)
    stage = wf.WorkflowStage(
        id="implement", agent="engineer", objective="build it",
        refine={"critic": "critic", "max_rounds": 2},
    )
    assert wr._refine_enabled(stage, gauntlet_ran=True) is True


def test_supersede_can_be_turned_off_to_stack_both(paths: Paths) -> None:
    provider = _ScriptedProvider()
    runner = _runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_refine": "all"},
        "gauntlet": {**DEFAULT_CLK_CONFIG["gauntlet"], "supersede_auto_refine": False},
    })
    wr = wf.WorkflowRunner(paths, runner)
    stage = wf.WorkflowStage(id="implement", agent="engineer", objective="build it")
    assert wr._refine_enabled(stage, gauntlet_ran=True) is True


# ---------------------------------------------------------------------------
# The prompt-level half
# ---------------------------------------------------------------------------


def test_every_bundled_prompt_teaches_the_answer_key() -> None:
    from clk_harness.templates.prompts import PROMPTS

    for name, body in PROMPTS.items():
        assert "ANSWER_KEY:" in body, f"{name} is missing the gauntlet block"
        assert "END_ANSWER_KEY" in body, f"{name} has a truncated gauntlet block"


def test_prompt_block_round_trips_through_the_parser() -> None:
    """The example the prompt shows agents must parse with our own parser."""
    from clk_harness.templates.prompts import PROMPTS

    assert g.parse_answer_key(
        "ANSWER_KEY:\n"
        "- tests_pass: `pytest -q` exits 0\n"
        "END_ANSWER_KEY"
    )
    # And the literal grammar in the prompt uses the same delimiters.
    body = PROMPTS["engineer.md"]
    assert "ANSWER_KEY:" in body and "END_ANSWER_KEY" in body
