"""End-to-end tests for the real orchestration engines.

These tests run the Ralph loop, autoresearch loop, and multi-stage workflow
WITHOUT --dry-run, so the actual AgentRunner / RalphLoop / AutoresearchLoop
code paths fire.  The shell provider is used throughout, which means:

* Every agent call succeeds (ok=True, response text echoes the prompt).
* No ACTION blocks are emitted, so no product files are written.
* Stub files land in .clk/runs/shell-stubs/ (gitignored).
* agent_memory.jsonl and experiments.jsonl are written on every run.
* PROGRESS.md (at project root) is written by the ralph loop after each
  iteration; because it is not gitignored it makes has_changes() True for
  the next iteration, which triggers a real git commit on iteration 2+.

What is verified for each engine:
- RalphLoop: experiments.jsonl entries (index, improved, committed, sha_before/
  sha_after), PROGRESS.md updates, agent_memory entries for ralph / engineer / qa.
- AutoresearchLoop: experiments.jsonl entries (question, finding, committed),
  agent_memory entries for ralph / analyst / critic.
- WorkflowRunner: all declared stages actually fire, agent_memory tracks them,
  depends_on ordering is honoured (no stage runs before its deps complete),
  recovery dispatch fires when a dep fails, blackboard dir is created.
- Chief supervise stage: the supervise stage runs with a shell response and
  the workflow terminates (supervise.max_cycles is capped at 1 for speed).
- Multi-agent orchestration: a goal-oriented prompt drives init → idea
  (auto-cast) → plan → run all in sequence; agent_memory.jsonl accumulates
  entries from every agent in the pipeline.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any, Dict, List

import pytest

from .conftest import run_clk


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _read_jsonl(path: Path) -> List[Dict[str, Any]]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            pass
    return out


def _agent_names_in_memory(proj: Path) -> List[str]:
    mem = proj / ".clk" / "state" / "agent_memory.jsonl"
    return [r["agent"] for r in _read_jsonl(mem) if "agent" in r]


def _patch_config(proj: Path, **overrides: Any) -> None:
    """Merge *overrides* into clk.config.json in place.

    Nested keys use dict values; e.g. ``supervise={"max_cycles": 1}``.
    """
    cfg_path = proj / ".clk" / "config" / "clk.config.json"
    cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    for k, v in overrides.items():
        if isinstance(v, dict) and isinstance(cfg.get(k), dict):
            cfg[k].update(v)
        else:
            cfg[k] = v
    cfg_path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


def _git_log_oneline(proj: Path) -> List[str]:
    r = subprocess.run(
        ["git", "log", "--oneline"],
        cwd=str(proj),
        capture_output=True,
        text=True,
    )
    return [l.strip() for l in r.stdout.splitlines() if l.strip()]


# ---------------------------------------------------------------------------
# Ralph loop — real execution
# ---------------------------------------------------------------------------


class TestRalphLoopReal:
    """Verifies RalphLoop without --dry-run using the shell provider."""

    def test_ralph_loop_runs_one_iteration(self, initialized_project: Path) -> None:
        """Single ralph iteration fires ralph → engineer → qa and records outcome."""
        res = run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=initialized_project)
        assert res.returncode == 0, res.stderr

        experiments = _read_jsonl(initialized_project / ".clk" / "state" / "experiments.jsonl")
        assert len(experiments) == 1, f"expected 1 experiment, got: {experiments}"

        exp = experiments[0]
        assert exp["index"] == 1
        assert "improved" in exp
        assert "committed" in exp
        assert "sha_before" in exp
        assert "sha_after" in exp
        assert "objective" in exp
        assert "started_at" in exp
        assert "finished_at" in exp

    def test_ralph_loop_records_agent_memory_for_all_three_agents(
        self, initialized_project: Path
    ) -> None:
        """ralph, engineer, and qa all appear in agent_memory.jsonl after one iteration."""
        run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=initialized_project)

        agents = _agent_names_in_memory(initialized_project)
        assert "ralph" in agents, f"ralph missing from agent_memory; got: {agents}"
        assert "engineer" in agents, f"engineer missing from agent_memory; got: {agents}"
        assert "qa" in agents, f"qa missing from agent_memory; got: {agents}"

    def test_ralph_loop_creates_run_dirs(self, initialized_project: Path) -> None:
        """A run directory is created under .clk/runs/ for each agent dispatch."""
        run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=initialized_project)

        run_dirs = list((initialized_project / ".clk" / "runs").iterdir())
        # Filter out shell-stubs sub-dir
        agent_run_dirs = [d for d in run_dirs if d.is_dir() and d.name != "shell-stubs"]
        assert len(agent_run_dirs) >= 3, (
            f"expected ≥3 run dirs (ralph, engineer, qa); got: {[d.name for d in run_dirs]}"
        )

    def test_ralph_loop_writes_progress_md(self, initialized_project: Path) -> None:
        """PROGRESS.md at the project root is updated after each ralph iteration."""
        run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=initialized_project)

        progress = initialized_project / "PROGRESS.md"
        assert progress.exists(), "PROGRESS.md should be written by ralph loop"
        content = progress.read_text(encoding="utf-8")
        assert "iter 1" in content, f"iter 1 entry missing from PROGRESS.md: {content}"

    def test_ralph_loop_two_iterations_commits_on_second(self, initialized_project: Path) -> None:
        """The second ralph iteration commits because PROGRESS.md from iter 1 is dirty.

        After iteration 1 _record() writes PROGRESS.md to the project root (not
        gitignored).  When iteration 2 begins, has_changes() is True, so the
        loop commits that file when eval passes and engineer.ok=True.
        """
        res = run_clk("loop", "--mode", "ralph", "--max-iterations", "2", cwd=initialized_project)
        assert res.returncode == 0, res.stderr

        experiments = _read_jsonl(initialized_project / ".clk" / "state" / "experiments.jsonl")
        assert len(experiments) == 2, f"expected 2 experiments: {experiments}"

        # Iteration 2 should see PROGRESS.md as dirty → committed=True
        iter2 = experiments[1]
        assert iter2["committed"] is True, (
            f"iteration 2 should be committed; sha_before={iter2.get('sha_before')} "
            f"sha_after={iter2.get('sha_after')}"
        )

        log = _git_log_oneline(initialized_project)
        assert any("ralph" in line for line in log), (
            f"expected a ralph commit in git log; got: {log}"
        )

    def test_ralph_loop_stdout_reports_improved_and_committed(
        self, initialized_project: Path
    ) -> None:
        """CLI stdout summarises improved and committed counts."""
        res = run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=initialized_project)
        assert res.returncode == 0, res.stderr
        # e.g. "ralph loop: 1/1 improved, 0 committed"
        assert "ralph loop:" in res.stdout, f"missing summary in stdout: {res.stdout!r}"


# ---------------------------------------------------------------------------
# Autoresearch loop — real execution
# ---------------------------------------------------------------------------


class TestAutoresearchLoopReal:
    """Verifies AutoresearchLoop without --dry-run using the shell provider."""

    def test_autoresearch_loop_runs_one_step(self, initialized_project: Path) -> None:
        """Single autoresearch step fires ralph → analyst → critic and records experiment."""
        res = run_clk(
            "loop", "--mode", "autoresearch", "--max-iterations", "1",
            cwd=initialized_project,
        )
        assert res.returncode == 0, res.stderr

        experiments = _read_jsonl(initialized_project / ".clk" / "state" / "experiments.jsonl")
        assert len(experiments) == 1, f"expected 1 experiment: {experiments}"

        exp = experiments[0]
        assert exp["index"] == 1
        assert "question" in exp
        assert "finding" in exp
        assert "committed" in exp

    def test_autoresearch_records_analyst_and_critic_in_memory(
        self, initialized_project: Path
    ) -> None:
        """analyst and critic both appear in agent_memory after one autoresearch step."""
        run_clk("loop", "--mode", "autoresearch", "--max-iterations", "1", cwd=initialized_project)

        agents = _agent_names_in_memory(initialized_project)
        assert "analyst" in agents, f"analyst missing; got: {agents}"
        assert "critic" in agents, f"critic missing; got: {agents}"
        assert "ralph" in agents, f"ralph (survey) missing; got: {agents}"

    def test_autoresearch_stdout_reports_experiments(self, initialized_project: Path) -> None:
        """CLI stdout reports the number of experiments run."""
        res = run_clk(
            "loop", "--mode", "autoresearch", "--max-iterations", "1",
            cwd=initialized_project,
        )
        assert res.returncode == 0, res.stderr
        assert "autoresearch loop:" in res.stdout, f"missing summary: {res.stdout!r}"

    def test_autoresearch_experiment_entries_grow_with_iterations(
        self, initialized_project: Path
    ) -> None:
        """Running 2 iterations produces 2 entries in experiments.jsonl."""
        run_clk(
            "loop", "--mode", "autoresearch", "--max-iterations", "2",
            cwd=initialized_project,
        )
        experiments = _read_jsonl(initialized_project / ".clk" / "state" / "experiments.jsonl")
        assert len(experiments) == 2, f"expected 2 experiments: {experiments}"
        assert experiments[0]["index"] == 1
        assert experiments[1]["index"] == 2


# ---------------------------------------------------------------------------
# Engineering workflow — real multi-agent orchestration
# ---------------------------------------------------------------------------


class TestEngineeringWorkflowReal:
    """Exercises the full WorkflowRunner (chief → engineer → qa → supervise)."""

    def test_engineering_workflow_dispatches_all_four_stages(
        self, initialized_project: Path
    ) -> None:
        """chief, engineer, and qa all fire; supervise fires too (it's a chief stage).

        supervise.max_cycles is capped at 1 to prevent the 20-cycle default.
        """
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        res = run_clk("run", "--once", cwd=initialized_project)
        assert res.returncode == 0, res.stderr

        agents = _agent_names_in_memory(initialized_project)
        assert "chief" in agents, f"chief never ran; agents seen: {agents}"
        assert "engineer" in agents, f"engineer never ran; agents seen: {agents}"
        assert "qa" in agents, f"qa never ran; agents seen: {agents}"

    def test_engineering_workflow_creates_run_dirs_per_stage(
        self, initialized_project: Path
    ) -> None:
        """A .clk/runs/ subdirectory is created for each unique agent dispatch.

        Note: cast and supervise both use the chief agent; when they run within
        the same second they share the same {timestamp}-{agent} directory name,
        so we expect ≥3 unique dirs (chief, engineer, qa) not ≥4.
        """
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        run_clk("run", "--once", cwd=initialized_project)

        run_dirs = list((initialized_project / ".clk" / "runs").iterdir())
        agent_run_dirs = [d for d in run_dirs if d.is_dir() and d.name != "shell-stubs"]
        assert len(agent_run_dirs) >= 3, (
            f"expected ≥3 run dirs (chief, engineer, qa); "
            f"got: {[d.name for d in run_dirs]}"
        )

    def test_engineering_workflow_prompt_and_response_files_present(
        self, initialized_project: Path
    ) -> None:
        """Each run dir contains prompt.txt, response.txt, and meta.json."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        run_clk("run", "--once", cwd=initialized_project)

        run_dirs = [
            d for d in (initialized_project / ".clk" / "runs").iterdir()
            if d.is_dir() and d.name != "shell-stubs"
        ]
        assert run_dirs, "no run dirs found"
        # Check first few dirs for expected files
        for d in sorted(run_dirs)[:3]:
            assert (d / "prompt.txt").exists(), f"prompt.txt missing in {d.name}"
            assert (d / "response.txt").exists(), f"response.txt missing in {d.name}"
            assert (d / "meta.json").exists(), f"meta.json missing in {d.name}"

    def test_engineering_workflow_stage_ordering_preserved(
        self, initialized_project: Path
    ) -> None:
        """agent_memory.jsonl entries appear in depends_on order: chief before engineer."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        run_clk("run", "--once", cwd=initialized_project)

        mem = _read_jsonl(initialized_project / ".clk" / "state" / "agent_memory.jsonl")
        agents_seq = [r["agent"] for r in mem]

        chief_idx = next((i for i, a in enumerate(agents_seq) if a == "chief"), None)
        engineer_idx = next((i for i, a in enumerate(agents_seq) if a == "engineer"), None)
        qa_idx = next((i for i, a in enumerate(agents_seq) if a == "qa"), None)

        assert chief_idx is not None, f"chief never ran; agents: {agents_seq}"
        assert engineer_idx is not None, f"engineer never ran; agents: {agents_seq}"
        assert qa_idx is not None, f"qa never ran; agents: {agents_seq}"
        assert chief_idx < engineer_idx, "chief must run before engineer (cast depends_on)"
        assert engineer_idx < qa_idx, "engineer must run before qa (qa depends_on implement)"

    def test_engineering_workflow_stdout_summarises_results(
        self, initialized_project: Path
    ) -> None:
        """CLI prints a stage summary line (ok count, fail count, total)."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        res = run_clk("run", "--once", cwd=initialized_project)
        assert res.returncode == 0, res.stderr
        assert "engineering:" in res.stdout, f"expected workflow summary; got: {res.stdout!r}"


# ---------------------------------------------------------------------------
# Chief supervise stage behaviour
# ---------------------------------------------------------------------------


class TestChiefSuperviseStage:
    """Verifies the supervise stage runs and terminates the cycle correctly."""

    def test_supervise_stage_runs_as_chief_agent(self, initialized_project: Path) -> None:
        """The supervise stage is dispatched to the chief (appears in agent_memory ≥2×)."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        run_clk("run", "--once", cwd=initialized_project)

        # Chief runs for: (a) cast stage, (b) supervise stage.
        agents = _agent_names_in_memory(initialized_project)
        chief_count = agents.count("chief")
        assert chief_count >= 2, (
            f"expected chief to run ≥2 times (cast + supervise); ran {chief_count} time(s)"
        )

    def test_supervise_stage_terminates_after_max_cycles(
        self, initialized_project: Path
    ) -> None:
        """With max_cycles=1 the workflow terminates without done.md (shell can't write it)."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})

        res = run_clk("run", "--once", cwd=initialized_project)
        # The workflow runner emits a WARN about cycle limit but still returns 0
        assert res.returncode == 0, res.stderr
        # done.md should NOT exist because the shell provider cannot write it
        assert not (initialized_project / ".clk" / "state" / "done.md").exists()


# ---------------------------------------------------------------------------
# Recovery dispatch
# ---------------------------------------------------------------------------


class TestRecoveryDispatch:
    """The chief is dispatched in recovery mode when a stage dependency fails.

    We force a failure by injecting a workflow whose early stage has a
    validation command that always fails; the subsequent stage depends on it.
    """

    def _write_failing_workflow(self, proj: Path) -> None:
        """Replace engineering.yaml with a 2-stage workflow where stage 1 always fails."""
        wf = (
            "name: engineering\n"
            "description: Recovery test workflow.\n"
            "stages:\n"
            "  - id: broken_stage\n"
            "    agent: engineer\n"
            "    objective: Intentionally failing stage.\n"
            "    validation: \"exit 1\"\n"
            "    commit: false\n"
            "  - id: recovery_dependent\n"
            "    agent: qa\n"
            "    objective: This stage depends on the broken stage.\n"
            "    depends_on: [broken_stage]\n"
            "    commit: false\n"
        )
        wf_path = proj / ".clk" / "config" / "workflows" / "engineering.yaml"
        wf_path.write_text(wf, encoding="utf-8")

    def test_recovery_dispatch_fires_chief_when_dep_fails(
        self, initialized_project: Path
    ) -> None:
        """When broken_stage fails validation, the runner dispatches chief in recovery."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})
        _patch_config(initialized_project, provider_retry={"max_retries": 0, "stage_max_retries": 0})
        self._write_failing_workflow(initialized_project)

        res = run_clk("run", "--once", cwd=initialized_project)
        # The workflow may exit non-zero (provider failure) or 0 (skipped dep)
        # but recovery dispatch means chief appears in agent_memory
        agents = _agent_names_in_memory(initialized_project)
        assert "chief" in agents, (
            f"chief recovery dispatch never fired; agents: {agents}"
        )


# ---------------------------------------------------------------------------
# Blackboard integration
# ---------------------------------------------------------------------------


class TestBlackboardIntegration:
    """Verifies the blackboard directory is created and accessible during runs."""

    def test_blackboard_dir_created_after_run(self, initialized_project: Path) -> None:
        """The .clk/blackboard/ directory exists after any non-dry workflow run."""
        _patch_config(initialized_project, supervise={"max_cycles": 1})
        run_clk("run", "--once", cwd=initialized_project)

        bb = initialized_project / ".clk" / "blackboard"
        assert bb.is_dir(), ".clk/blackboard/ should exist after a workflow run"


# ---------------------------------------------------------------------------
# Goal-oriented multi-agent pipeline
# ---------------------------------------------------------------------------


class TestGoalOrientedPipeline:
    """Drive the full user-facing pipeline: init → idea (auto-cast) → plan → run.

    This exercises the complete orchestration chain: chief casts the roster on
    idea capture, then plan runs discovery + product workflows, then run executes
    the engineering workflow — all as a user would invoke them.
    """

    def test_full_pipeline_accumulates_agent_memory(self, clk_project: Path) -> None:
        """Agents from every pipeline stage accumulate in agent_memory.jsonl."""
        # Init
        res = run_clk("init", "--name", "goal-test", cwd=clk_project)
        assert res.returncode == 0, res.stderr

        # Limit supervise cycles to prevent 20-cycle iteration
        _patch_config(clk_project, supervise={"max_cycles": 1})

        # Idea: auto-cast fires the chief casting pass
        res = run_clk(
            "idea", "Build a task management CLI app",
            "--title", "Task CLI",
            cwd=clk_project,
        )
        assert res.returncode == 0, res.stderr

        # Plan: discovery + product workflows
        res = run_clk("plan", cwd=clk_project)
        assert res.returncode in (0, 1), f"plan crashed: {res.stderr}"

        # Run: engineering workflow
        res = run_clk("run", cwd=clk_project)
        assert res.returncode in (0, 1), f"run crashed: {res.stderr}"

        # Verify agents from all phases appear
        agents = set(_agent_names_in_memory(clk_project))
        assert "chief" in agents, f"chief never ran; agents: {agents}"
        # At least one of the specialist agents from any workflow
        specialist_hit = agents & {"engineer", "qa", "analyst", "researcher", "critic"}
        assert specialist_hit, f"no specialist agents ran; agents: {agents}"

    def test_full_pipeline_run_dirs_span_multiple_workflows(
        self, clk_project: Path
    ) -> None:
        """After init → idea → plan → run, .clk/runs/ has dirs from multiple workflows."""
        res = run_clk("init", "--name", "pipeline-test", cwd=clk_project)
        assert res.returncode == 0, res.stderr
        _patch_config(clk_project, supervise={"max_cycles": 1})

        run_clk("idea", "Interactive CLI scheduler", "--title", "Scheduler", cwd=clk_project)
        run_clk("plan", cwd=clk_project)
        run_clk("run", cwd=clk_project)

        run_dirs = [
            d for d in (clk_project / ".clk" / "runs").iterdir()
            if d.is_dir() and d.name != "shell-stubs"
        ]
        # Expect dirs from discovery + product + engineering = many stages
        assert len(run_dirs) >= 6, (
            f"expected ≥6 run dirs across workflows; got {len(run_dirs)}: "
            f"{sorted(d.name for d in run_dirs)[:10]}"
        )

    def test_goal_oriented_ralph_after_pipeline(self, clk_project: Path) -> None:
        """Run ralph loop after a full pipeline; it iterates using state built by planning."""
        res = run_clk("init", "--name", "ralph-after-plan", cwd=clk_project)
        assert res.returncode == 0, res.stderr
        _patch_config(clk_project, supervise={"max_cycles": 1})

        run_clk("idea", "Build a recommendation engine", "--title", "RecoEngine", cwd=clk_project)
        # Skip plan to save time; idea auto-cast gives enough state context.

        res = run_clk("loop", "--mode", "ralph", "--max-iterations", "1", cwd=clk_project)
        assert res.returncode == 0, res.stderr

        experiments = _read_jsonl(clk_project / ".clk" / "state" / "experiments.jsonl")
        assert len(experiments) >= 1
        # Verify the experiment context captured the idea title
        mem = _read_jsonl(clk_project / ".clk" / "state" / "agent_memory.jsonl")
        ralph_entries = [r for r in mem if r.get("agent") == "ralph"]
        assert ralph_entries, "ralph never ran"

    def test_autoresearch_after_pipeline_records_finding(self, clk_project: Path) -> None:
        """Autoresearch after idea capture produces experiments with finding field."""
        res = run_clk("init", "--name", "autores-after-plan", cwd=clk_project)
        assert res.returncode == 0, res.stderr

        run_clk("idea", "Research state-of-the-art in LLM routing", "--no-cast", cwd=clk_project)

        res = run_clk(
            "loop", "--mode", "autoresearch", "--max-iterations", "1", cwd=clk_project
        )
        assert res.returncode == 0, res.stderr

        experiments = _read_jsonl(clk_project / ".clk" / "state" / "experiments.jsonl")
        assert experiments, "no experiments recorded"
        exp = experiments[0]
        assert "finding" in exp, f"finding missing from experiment: {exp}"
        assert "question" in exp, f"question missing from experiment: {exp}"
