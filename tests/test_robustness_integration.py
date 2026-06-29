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

from clk_harness.config import Paths, DEFAULT_CLK_CONFIG
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
        # re-dispatch the (action-less) engineer response on its own.
        "noop_guard": {"enabled": False},
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
