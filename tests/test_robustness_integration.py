"""Integration tests for the robustness loops.

Covers the cross-module wiring added in Layer 1-4:

* ``POST: question`` blocks parse target_agent + urgency.
* Workflow YAML parses ``refine:`` blocks.
* ``find_unanswered_questions`` filters answered questions correctly.
* The :class:`AgentRunner` chokepoint wrappers skip recursion when
  invoked under a meta-phase, and proactive auto-consensus fires on
  careful stages.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List

import pytest

from clk_harness.config import DEFAULT_CLK_CONFIG, Paths
from clk_harness.orchestration import blackboard as bb
from clk_harness.orchestration import workflow as wf
from clk_harness.orchestration.agent import AgentRunner
from clk_harness.providers.base import AgentResponse


@pytest.fixture
def paths(tmp_path: Path) -> Paths:
    p = Paths(root=tmp_path)
    p.ensure()
    return p


# ---------------------------------------------------------------------------
# Layer 1: response_quality is exercised in test_response_quality.py.
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Layer 2: blackboard Q&A protocol
# ---------------------------------------------------------------------------


def test_post_question_with_to_and_urgency(paths: Paths) -> None:
    text = (
        "POST: question\n"
        "TO: architect\n"
        "URGENCY: blocking\n"
        "BODY:\n"
        "Are user IDs opaque strings or ints?\n"
        "END_POST\n"
    )
    posted = bb.apply_post_blocks(paths, text, author="researcher", stage_id="s1")
    assert len(posted) == 1
    q = posted[0]
    assert q.target_agent == "architect"
    assert q.urgency == "blocking"
    assert q.post_type == "question"


def test_find_unanswered_questions(paths: Paths) -> None:
    q = bb.post(
        paths,
        author="researcher",
        body="What is the data model?",
        post_type="question",
        target_agent="architect",
        urgency="blocking",
    )
    assert q.id in [p.id for p in bb.find_unanswered_questions(paths)]
    # Answer it.
    bb.post(
        paths,
        author="architect",
        body="Use UUID strings.",
        post_type="answer",
        consumes=[q.id],
    )
    assert q.id not in [p.id for p in bb.find_unanswered_questions(paths)]


def test_find_unanswered_filters_by_target(paths: Paths) -> None:
    bb.post(
        paths, author="a", body="q1", post_type="question",
        target_agent="architect", urgency="blocking",
    )
    bb.post(
        paths, author="a", body="q2", post_type="question",
        target_agent="qa", urgency="blocking",
    )
    only_arch = bb.find_unanswered_questions(paths, target_agent="architect")
    assert len(only_arch) == 1
    assert only_arch[0].target_agent == "architect"


def test_async_urgency_parsed(paths: Paths) -> None:
    text = (
        "POST: question\n"
        "TO: engineer\n"
        "URGENCY: async\n"
        "BODY:\n"
        "When you next touch the auth module, consider X.\n"
        "END_POST\n"
    )
    posted = bb.apply_post_blocks(paths, text, author="critic", stage_id="s1")
    assert posted[0].urgency == "async"


# ---------------------------------------------------------------------------
# Layer 3: workflow stage `refine:` field
# ---------------------------------------------------------------------------


def test_workflow_stage_parses_refine_dict(tmp_path: Path) -> None:
    yaml_path = tmp_path / "engineering.yaml"
    yaml_path.write_text(
        """
name: engineering
description: test
stages:
  - id: design_spec
    agent: architect
    objective: Draft the spec.
    refine:
      critic: critic
      max_rounds: 4
      accept_threshold: 0.75
""",
        encoding="utf-8",
    )
    flow = wf.load_workflow(yaml_path)
    stage = flow.stages[0]
    assert stage.refine == {"critic": "critic", "max_rounds": 4, "accept_threshold": 0.75}


def test_workflow_stage_refine_absent_is_none(tmp_path: Path) -> None:
    yaml_path = tmp_path / "engineering.yaml"
    yaml_path.write_text(
        """
name: engineering
description: test
stages:
  - id: plain
    agent: engineer
    objective: Just do it.
""",
        encoding="utf-8",
    )
    flow = wf.load_workflow(yaml_path)
    assert flow.stages[0].refine is None


# ---------------------------------------------------------------------------
# Layer 1: AgentRunner chokepoint dispatch dispatch
# ---------------------------------------------------------------------------


class _FakeProvider:
    """Provider stub yielding pre-canned responses in order.

    Used to test the chokepoint without spawning real subprocesses.
    """

    def __init__(self, responses: List[AgentResponse]) -> None:
        self._responses = list(responses)
        self.calls: List[Dict[str, Any]] = []

    def describe(self) -> str:
        return "fake"

    def invoke(self, req):  # noqa: ANN001
        self.calls.append({"agent": req.agent, "prompt_len": len(req.prompt or "")})
        if not self._responses:
            return AgentResponse(ok=True, text="(default)")
        return self._responses.pop(0)


def _make_runner(paths: Paths, provider: _FakeProvider, clk_cfg_overrides: Dict[str, Any] | None = None) -> AgentRunner:
    agents_cfg = {
        "agents": {
            "chief": {"prompt": "chief.md", "provider": None, "role": "casting director"},
            "qa":    {"prompt": "qa.md",    "provider": None, "role": "validator"},
            "ralph": {"prompt": "ralph.md", "provider": None, "role": "loop driver"},
            "engineer": {"prompt": "engineer.md", "provider": None, "role": "implementer"},
            "architect": {"prompt": "architect.md", "provider": None, "role": "design"},
            "critic": {"prompt": "critic.md", "provider": None, "role": "judge"},
        }
    }
    providers_cfg = {"active": "fake", "providers": {"fake": {"type": "fake"}}}
    clk_cfg: Dict[str, Any] = json.loads(json.dumps(DEFAULT_CLK_CONFIG))  # deep copy
    clk_cfg["dry_run"] = False
    clk_cfg["provider_retry"] = {"max_retries": 0, "backoff_s": 0}
    if clk_cfg_overrides:
        clk_cfg.update(clk_cfg_overrides)
    runner = AgentRunner(paths, agents_cfg, providers_cfg, clk_cfg)
    # Override the provider loader to always return our stub.
    runner.get_provider = lambda name=None: provider  # type: ignore[method-assign]
    return runner


def test_debate_loop_runs_panel_and_revises(paths: Paths) -> None:
    """The debate panel fans out one critic per lens, then revises the worker
    when the panel asks for it, and accepts on the next round."""
    from clk_harness.orchestration.agent import AgentRun

    # 3 lenses revise (round 1) -> 1 worker revision -> 3 lenses accept (round 2).
    responses = (
        [AgentResponse(ok=True, text="Issue found.\nVERDICT: revise\nSCORE: 0.4")] * 3
        + [AgentResponse(ok=True, text="Revised the work substantively per the panel.")]
        + [AgentResponse(ok=True, text="Looks good now.\nVERDICT: accept\nSCORE: 0.9")] * 3
    )
    provider = _FakeProvider(responses)
    runner = _make_runner(paths, provider, {
        # sequential fan-out so the fake provider's call list isn't raced
        "consensus": {**DEFAULT_CLK_CONFIG["consensus"], "max_parallel": 1},
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "debate": "careful_only",
                       "debate_max_rounds": 2, "auto_refine": "off"},
    })
    wr = wf.WorkflowRunner(paths, runner)
    flow = wf.Workflow(name="engineering", description="", stages=[])
    stage = wf.WorkflowStage(id="implement", agent="engineer", objective="build it",
                             careful=True)
    assert wr._debate_enabled(stage)
    first = AgentRun(agent="engineer", objective="build it",
                     response=AgentResponse(ok=True, text="initial work"),
                     started_at="t0", finished_at="t1")
    final = wr._debate_loop(flow, stage, first, "cycle 1/1", dry_run=False)
    assert final.response.ok
    # 3 critics (r1) + 1 worker revision + 3 critics (r2) = 7 dispatches.
    assert len(provider.calls) == 7, [c["agent"] for c in provider.calls]
    # The worker (engineer) was re-dispatched for the revision.
    assert any(c["agent"] == "engineer" for c in provider.calls)


def test_quality_retry_fires_on_empty_response(paths: Paths) -> None:
    # First call: empty (should trigger retry). Second call: substantive.
    good = "Here is a substantive engineering plan with concrete steps."
    provider = _FakeProvider([
        AgentResponse(ok=True, text=""),
        AgentResponse(ok=True, text=good),
    ])
    runner = _make_runner(paths, provider, {
        "robustness": {
            **DEFAULT_CLK_CONFIG["robustness"],
            "auto_consensus": "off",
            "auto_refine": "off",
            "max_quality_retries": 1,
        },
        # Isolate the quality-retry path: the no-op guard would otherwise
        # re-dispatch the (action-less) engineer response on its own, and
        # the gauntlet would critique the recovered response.
        "noop_guard": {"enabled": False},
        "gauntlet": {"enabled": False},
    })
    run = runner.run("engineer", "Implement feature X.")
    assert run.response.text == good
    assert len(provider.calls) == 2, "expected one retry after empty first response"


def test_quality_retry_capped(paths: Paths) -> None:
    # All four calls return empty; runner gives up after max_quality_retries+1.
    provider = _FakeProvider([
        AgentResponse(ok=True, text="") for _ in range(10)
    ])
    runner = _make_runner(paths, provider, {
        "robustness": {
            **DEFAULT_CLK_CONFIG["robustness"],
            "auto_consensus": "off",
            "auto_refine": "off",
            "max_quality_retries": 2,
        }
    })
    runner.run("engineer", "Implement feature X.")
    # Initial + 2 retries = 3 attempts. (No escalation since auto_consensus=off.)
    assert len(provider.calls) == 3


def test_meta_phase_bypasses_quality_loop(paths: Paths) -> None:
    # An empty response inside a meta phase should NOT retry.
    provider = _FakeProvider([AgentResponse(ok=True, text="")])
    runner = _make_runner(paths, provider, {
        "robustness": {
            **DEFAULT_CLK_CONFIG["robustness"],
            "auto_consensus": "off",
            "auto_refine": "off",
            "max_quality_retries": 3,
        }
    })
    runner.run("engineer", "noop", extra={"phase": "refine_critic"})
    assert len(provider.calls) == 1


def test_should_auto_consensus_on_careful(paths: Paths) -> None:
    provider = _FakeProvider([])
    runner = _make_runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_consensus": "on_careful"}
    })
    assert runner._should_auto_consensus("engineer", {"careful": True})
    assert not runner._should_auto_consensus("engineer", {})
    assert not runner._should_auto_consensus("chief", {"careful": True})


def test_should_auto_consensus_off(paths: Paths) -> None:
    provider = _FakeProvider([])
    runner = _make_runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_consensus": "off"}
    })
    assert not runner._should_auto_consensus("engineer", {"careful": True})


def test_should_auto_consensus_always(paths: Paths) -> None:
    provider = _FakeProvider([])
    runner = _make_runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "auto_consensus": "always"}
    })
    assert runner._should_auto_consensus("engineer", {})
    assert runner._should_auto_consensus("ralph", {})
    assert not runner._should_auto_consensus("chief", {})


# ---------------------------------------------------------------------------
# TODOS: mutable per-turn checklist wiring
# ---------------------------------------------------------------------------


def test_todos_persisted_on_dispatch(paths: Paths) -> None:
    from clk_harness.orchestration import todos as td

    provider = _FakeProvider([
        AgentResponse(ok=True, text="TODOS:\n- [ ] step one\n- [~] step two\nEND_TODOS")
    ])
    runner = _make_runner(paths, provider)
    runner._dispatch_once("engineer", "obj", extra={}, dry_run=False)
    tl = td.todos_for(paths, "engineer")
    assert tl is not None
    assert [(i.status, i.text) for i in tl.items] == [("todo", "step one"), ("doing", "step two")]


def test_todos_skipped_in_meta_phase(paths: Paths) -> None:
    from clk_harness.orchestration import todos as td

    provider = _FakeProvider([AgentResponse(ok=True, text="TODOS:\n- [ ] x\nEND_TODOS")])
    runner = _make_runner(paths, provider)
    runner._dispatch_once("engineer", "obj", extra={"phase": "qa_answer"}, dry_run=False)
    assert td.todos_for(paths, "engineer") is None


def test_todos_reinjected_into_next_context(paths: Paths) -> None:
    """A persisted checklist is surfaced in that author's next $todos context."""
    from clk_harness.orchestration import todos as td

    provider = _FakeProvider([])
    runner = _make_runner(paths, provider)
    td.apply_todos_blocks(paths, "TODOS:\n- [~] wire parser\nEND_TODOS", author="engineer")
    ctx = runner._collect_context("obj", {"agent": "engineer"})
    assert "wire parser" in ctx["todos"]
    assert "[~]" in ctx["todos"]
    # A different author sees their own (empty) checklist, not the engineer's.
    other = runner._collect_context("obj", {"agent": "qa"})
    assert "wire parser" not in other["todos"]


def test_todos_end_to_end_prompt_when_template_seeded(paths: Paths) -> None:
    """With the shipped engineer template on disk, the checklist reaches the
    final rendered prompt (the $todos placeholder lives in _BASE_FOOTER)."""
    from clk_harness.orchestration import todos as td
    from clk_harness.templates.prompts import PROMPTS

    (paths.prompts / "engineer.md").write_text(PROMPTS["engineer.md"], encoding="utf-8")
    td.apply_todos_blocks(paths, "TODOS:\n- [~] wire parser\nEND_TODOS", author="engineer")
    provider = _FakeProvider([])
    runner = _make_runner(paths, provider)
    prompt = runner.render_prompt(runner.get_agent("engineer"), "obj", extra={})
    assert "wire parser" in prompt


# ---------------------------------------------------------------------------
# DELEGATE: context-isolated sub-agent wiring
# ---------------------------------------------------------------------------


def _delegate_run(agent: str, body: str):
    from clk_harness.orchestration.agent import AgentRun

    return AgentRun(
        agent=agent, objective="o",
        response=AgentResponse(ok=True, text=body),
        started_at="t0", finished_at="t1",
    )


def test_delegate_spawns_isolated_child_and_returns_one_result(paths: Paths) -> None:
    child_text = "POST: finding\nBODY:\nsecret child detail\nEND_POST\nSummary: built the helper"
    provider = _FakeProvider([AgentResponse(ok=True, text=child_text)])
    runner = _make_runner(paths, provider)
    parent = _delegate_run(
        "architect", "DELEGATE: h\nTO: engineer\nTASK:\nbuild helper\nEND_DELEGATE"
    )
    runner._apply_delegate(parent, {})
    # Exactly one child dispatch, to the named target.
    assert [c["agent"] for c in provider.calls] == ["engineer"]
    posts = bb.list_posts(paths)
    # The child's own POST block was suppressed (not persisted to the board).
    assert all(p.post_type != "finding" for p in posts)
    # Exactly one delegate_result, authored by the parent, carrying the child's
    # distilled output, and recorded on the parent run.
    results = [p for p in posts if p.post_type == "delegate_result"]
    assert len(results) == 1
    assert results[0].author == "architect"
    assert "child detail" in results[0].body
    assert results[0].id in parent.posts


def test_delegate_child_can_do_real_work(paths: Paths) -> None:
    child_text = (
        "ACTION: write\nPATH: scratch/out.txt\nCONTENT:\nhello from child\nEND_ACTION\nDONE"
    )
    provider = _FakeProvider([AgentResponse(ok=True, text=child_text)])
    runner = _make_runner(paths, provider, {"auto_commit": False})
    parent = _delegate_run(
        "architect", "DELEGATE: w\nTO: engineer\nTASK:\nwrite it\nEND_DELEGATE"
    )
    runner._apply_delegate(parent, {})
    assert (paths.root / "scratch" / "out.txt").read_text(encoding="utf-8").strip() == "hello from child"


def test_delegate_depth_cap_blocks_grandchild(paths: Paths) -> None:
    provider = _FakeProvider([AgentResponse(ok=True, text="should-not-run")])
    runner = _make_runner(paths, provider)  # max_delegate_depth default 1
    parent = _delegate_run(
        "architect", "DELEGATE: x\nTO: engineer\nTASK:\nt\nEND_DELEGATE"
    )
    # Already one level deep -> at the cap, so no child is spawned.
    runner._apply_delegate(parent, {"delegate_chain": ["ralph"]})
    assert provider.calls == []
    assert [p for p in bb.list_posts(paths) if p.post_type == "delegate_result"] == []


def test_delegate_unknown_target_skipped(paths: Paths) -> None:
    provider = _FakeProvider([AgentResponse(ok=True, text="x")])
    runner = _make_runner(paths, provider)
    parent = _delegate_run(
        "architect", "DELEGATE: x\nTO: nonexistent\nTASK:\nt\nEND_DELEGATE"
    )
    runner._apply_delegate(parent, {})
    assert provider.calls == []


def test_delegate_self_and_cycle_skipped(paths: Paths) -> None:
    provider = _FakeProvider([AgentResponse(ok=True, text="x")])
    # self: architect delegating to architect (depth ok, self-skip fires).
    runner = _make_runner(paths, provider)
    runner._apply_delegate(
        _delegate_run("architect", "DELEGATE: x\nTO: architect\nTASK:\nt\nEND_DELEGATE"),
        {"delegate_chain": []},
    )
    # cycle: target already in the chain (raise depth so the cycle guard is reached).
    runner2 = _make_runner(paths, provider, {
        "robustness": {**DEFAULT_CLK_CONFIG["robustness"], "max_delegate_depth": 5}
    })
    runner2._apply_delegate(
        _delegate_run("architect", "DELEGATE: x\nTO: engineer\nTASK:\nt\nEND_DELEGATE"),
        {"delegate_chain": ["engineer"]},
    )
    assert provider.calls == []


def test_delegate_isolation_withholds_blackboard(paths: Paths) -> None:
    provider = _FakeProvider([])
    runner = _make_runner(paths, provider)
    bb.post(paths, author="researcher", body="PEER-SECRET-XYZ",
            post_type="finding", slug_hint="f")
    normal = runner._collect_context("obj", {"agent": "engineer"})["blackboard_digest"]
    isolated = runner._collect_context(
        "obj", {"agent": "engineer", "phase": "delegate"}
    )["blackboard_digest"]
    assert "PEER-SECRET-XYZ" in normal
    assert "PEER-SECRET-XYZ" not in isolated
    assert "isolated" in isolated.lower()
