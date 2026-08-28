# Autonomous missions & orchestration

Part of the [CLK documentation](../README.md). The mission lifecycle, chief supervisor, dynamic casting, the action protocol, workflows, loops, robustness layers, and completion criteria.

## Autonomous missions

Give CLK one objective and it drives the **whole lifecycle to a code-gated
"done" on its own** — no babysitting with separate `plan` / `run` / `loop`
commands. This is the plan→execute→evaluate→refine→iterate loop made
*reliable*: the guarantees that used to live only in prompt text are now
enforced in the harness.

**Autonomy is the default.** `clk run` (and the TUI's first message, and the
web/REST single-prompt flow) drive the full mission. `clk mission "<idea>"`
(alias `clk auto`) is the explicit form; `clk run --once` restores the legacy
single-workflow pass.

```bash
clk mission "a local-first journaling app that summarizes my week"
# charter → plan → discovery → product → engineering(+ralph) → validation
#         → deployment → done-gate → done_granted.md
```

### Charter first, then a living plan

1. **Charter** — before any work, the chief authors `.clk/state/charter.md`
   (+ `charter.json`): mission statement, scope, explicit non-goals, success
   criteria, constraints. The plan and the done-gate are *derived from it*, so
   "done" is judged against an up-front commitment instead of drifting.
2. **Living plan** — the chief emits a `PROPOSE_PLAN` block; the harness
   persists an ordered phase plan to `.clk/state/mission.json` (human mirror
   `MISSION.md`). After each phase a chief **phase-gate** returns
   `pass | repeat | revise | done`; `revise` re-plans the remaining phases, so
   the chief can reorder, insert, or skip phases as it learns.

Three nested loops: the per-stage **supervise/ralph** loops (unchanged) inside a
per-phase **repeat** loop inside the **phase-sequencing** loop — bounded by
`mission.max_phases` and `mission.max_total_cycles`.

### Reliability reinforcements (enforced in code, not prose)

- **Machine-checkable done-gate.** `ACTION:done` / `done.md` is now a *request*.
  The loop only stops when `done_gate.evaluate_done_gate` passes and writes
  `.clk/state/done_granted.md`. Default checks: tests green, deliverables on
  disk, a `POST: qa` PASS, a ralph refinement pass, plus any file-named charter
  success criteria. **Adaptive:** the tests-green check is auto-relaxed when no
  real test command can be derived (docs / research / content projects), so the
  gate is strict where it's meaningful but never deadlocks. Each `require_*` is
  an independent switch; `done_gate.enabled: false` restores legacy behavior.
- **No-op guard.** A producing stage (engineer/ralph, or any stage with an
  `outputs` contract) whose response changed **no files** is re-dispatched with
  an escalating "emit ACTION blocks now" preamble — descriptions stop counting
  as work. Kill: `noop_guard.enabled: false`.
- **Evaluation never silently skips.** A producing stage with no `validation:`
  no longer auto-passes — the harness derives a real command from the project
  shape (`pytest` / `npm test` / a `compileall` smoke). Kill:
  `validation.auto_derive: false`.
- **Ungated refinement.** `robustness.auto_refine` defaults to `all`, so every
  producing stage gets a critic pass without the chief having to mark it
  `careful`.
- **Deliberation — the team thinks.** Producing dispatches get a
  self-reflect preamble and an invitation to ask peers
  `POST: question TO: <peer> URGENCY: blocking`; a phase gate cannot `pass`
  while a blocking question is unanswered. Kill: `deliberation.enabled: false`.
- **Execution trace.** Structured `[clk:charter|plan|phase-start|phase-gate:*|done]`
  commits land at every boundary, so the git log *is* the audit trail. Kill:
  `mission.commit_trace: false`.
- **Observability.** Each cycle prints a one-line summary and writes a
  `loop_cycle_summary` event to `.clk/logs/activity.jsonl`, e.g.
  `cycle 3/60 | phase engineering | stages 5 (4 ok) | actions 7 | refine 1r | eval FAIL(2) | done-gate REJECT(no_qa_pass)`.

### Mission config (`clk.config.json`)

```jsonc
"mission":      { "max_phases": 12, "max_iterations_per_phase": 3,
                  "max_total_cycles": 60, "phase_gate": true,
                  "refine_required": true, "charter_first": true,
                  "commit_trace": true, "telemetry_stdout": true,
                  "default_phases": ["discovery","product","engineering","validation","deployment"] },
"done_gate":    { "enabled": true, "require_tests_green": true,
                  "require_deliverables": true, "min_deliverable_files": 1,
                  "require_qa_pass": true, "require_ralph_pass": true,
                  "forbid_todo_markers": false },
"noop_guard":   { "enabled": true, "max_redispatch": 2,
                  "producing_agents": ["engineer","ralph"] },
"deliberation": { "enabled": true, "encourage_questions": true,
                  "require_open_questions_resolved": true,
                  "self_reflect_preamble": true, "min_debate_rounds": 1 }
```

Every knob above is also overridable from the environment via the matching
`CLK_MISSION_*`, `CLK_DONE_GATE_*`, `CLK_NOOP_GUARD_*`, and
`CLK_DELIBERATION_*` variables (see `.env.example`; `kickoff.sh` maps them into
`clk.config.json`). CLI overrides: `clk run --max-phases N --max-cycles M`,
`clk run --once`, `clk mission "<idea>" --resume` (continue a persisted plan).

## Chief supervisor loop

The default `engineering` workflow ends with a `supervise` stage where
the chief evaluates whether the user's prompt has been fully addressed.
The chief either:

- emits `ACTION: done` with a one-line reason — writes
  `.clk/state/done.md` and terminates the loop, or
- emits `PROPOSE_WORKFLOW` with the next iteration's stages — the
  workflow runner picks them up and runs another cycle.

The prompts enforce an explicit asymmetry: a **low bar to continue**
(any single trigger — missing tests, no ralph pass on the latest
output, open TODOs, stale docs, any nameable improvement — starts the
next cycle immediately) and a **high bar to stop** (`ACTION: done`
requires every done-checklist item: deliverables on disk, tests
passing, a QA PASS, a ralph refinement pass, docs updated). So no agent
is ever truly "done" until the chief proves completion. Capped at
`clk.config.json::supervise.max_cycles` (default 100).

Stall handling: a cycle with no commits, no file writes, and/or an
explicit `PROGRESS: no` self-report counts against
`supervise.max_consecutive_no_progress` (default 8). Hitting the cap
dispatches the chief once in **stall-rescue** mode (restructure the
plan, unblock, or justify done) before the loop gives up — disable via
`supervise.stall_rescue: false`.

## Dynamic agents (casting)

The harness ships with three baseline agents that cannot be removed:

- `chief` — decomposes objectives, casts the team, authors workflow YAML.
- `qa` — default validator.
- `ralph` — drives both the Ralph refinement loop and Karpathy-style
  autoresearch cycles; the mode is inferred from the current project state.

Everything else is dynamic. On the first user message, the chief is
auto-dispatched with the captured idea and casts the project-specific team,
including `engineer` when an implementer is needed (e.g. `data_steward`,
`ml_evaluator`, `api_contract`, `ux_writer`, `security_auditor`).

The name `engineer` is reserved: the harness actively rejects any attempt
to create `engineering`, `coder`, `developer`, or other aliases, and
reports the denial directly to the chief via its `$casting_feedback` context
so it learns to use `engineer` directly. Each role decision is
applied immediately and persisted to `.clk/config/agents.json` plus
`.clk/state/casting.log` (JSONL, one entry per add/update/remove).

Type `/cast` in the TUI to force a re-cast at any time, or run
`clk cast` from the CLI. To inspect or edit by hand:
`clk roles list|add --name X --role "..."|remove --name X`.

Agents communicate via a **blackboard** at `.clk/blackboard/` — short
markdown POST blocks each agent emits at the end of its run, filtered
into peers' prompts based on each stage's `inputs:` selectors.
Directed clarifying questions are a special POST type
(`POST: question TO: <peer> URGENCY: blocking`) routed inline by the
harness — see **Robustness loops** for the protocol details and depth
caps.

## Action protocol

Agents drive real changes by emitting `ACTION:` blocks the harness
parses and applies — descriptions alone do nothing. Supported kinds:

- `ACTION: write` / `edit` / `append` / `delete` — file mutations
  (paths must resolve inside the project root; originals are backed up
  to `.clk/backups/<run_id>/`). Paths are resolved **chroot-style**: a
  leading `/` maps to the project root and a fully-qualified workspace
  path has the root prefix stripped, so agents that emit absolute paths
  don't silently lose their work. Escapes (`../`) and `.clk/` stay
  rejected.
- `ACTION: run` — shell command, runs in project root, output captured
  to the log; rejects `sudo` and obvious-foot-gun patterns.
- `ACTION: done` — writes `.clk/state/done.md`, signaling the loops to
  stop.

Every agent run that mutates files produces an immediate structured
git commit (`[agent] objective` with files, commands, token totals in
the body). A cap from `clk.config.json::validation.max_files_per_batch`
(default 25) limits damage from a runaway agent.

## Self-healing on unmet deps

When a workflow stage's dependencies fail, the harness dispatches the
chief in *recovery mode* with the exact failure reasons (agent error,
validation output) and asks them to either re-cast the workflow,
emit `ACTION` blocks that fix the upstream failure, or `PROPOSE_ROLE`
a specialist that can. Capped at 3 recovery passes per stage
(configurable via `clk.config.json::recovery::max_per_stage`).

Two more recovery paths run automatically:

- **Unmet outputs contracts.** When a stage's declared `outputs:` keys
  never land in any POST block's `PRODUCES` line, the chief gets a
  recovery dispatch to fill the gap (re-dispatch the worker, post a
  substitute, or accept it) instead of letting downstream stages consume
  missing inputs. Toggle via `recovery.dispatch_on_unmet_outputs`.
- **Failed validations keep the work.** A failed stage validation no
  longer hard-resets the workspace by default — the failure is recorded
  and later cycles repair forward, so batch-committed files stay on disk
  and visible in the Files tab. Policy via
  `validation.rollback_on_failure`: `never` | `careful` (default — only
  `careful: true` stages roll back) | `always` (legacy). When a rollback
  does run, the discarded work is first preserved behind a
  `refs/clk/rollbacks/<stage>-<ts>` ref so it stays recoverable in git.

This section is about *dependency and stage* failures. *Content*
failures — empty, malformed, or low-confidence agent output that
nonetheless returned `ok=True` — are handled by the response-quality
re-dispatch loop documented in **Robustness loops** above.

## Workflows

YAML workflows live in `.clk/config/workflows/`. The default
`engineering.yaml` is intentionally minimal (chief → engineer → qa);
the chief overwrites it on first cast with a project-tailored cycle.
The bundled scaffolds:

- `discovery.yaml` - validate problem, users, landscape.
- `product.yaml` - PRD + technical architecture.
- `engineering.yaml` - baseline cycle; chief replaces this per project.
- `validation.yaml` - drive toward a green test suite.
- `deployment.yaml` - deployment recipe + checklist.
- `ralph_loop.yaml` - single Ralph iteration (use `clk loop` to repeat).

Stage schema:

```yaml
- id: implement
  agent: engineer
  objective: Implement the smallest vertical slice.
  depends_on: [architect]
  validation: "pytest -q"
  commit: true
```

When `validation` is set, the command must exit 0 before the harness
will commit. Failed validations leave the working tree untouched (and
in the Ralph loop, are reverted to the pre-iteration HEAD).

## Loops

Ralph runs in two modes (selected automatically based on project state,
or forced via `/loop`):

- **Refinement mode (`/loop ralph N`, default).** Each iteration: ralph
  picks one measurable improvement, the engineer implements it, QA
  validates, and the harness commits or reverts.
- **Autoresearch mode (`/loop autoresearch N`).** Each iteration: ralph
  surveys state, picks the highest-value open question, designs and runs
  a small experiment, and records the learning regardless of pass/fail.

Both modes respect `max_iterations` and stop early when
`.clk/state/done.md` is created. Both also auto-detect plateau and
regression and adapt — see **Robustness loops** below.

## Robustness loops

CLK leans into the loop: every dispatch is wrapped in self-correcting
behavior so the harness does not just accept the first thing a
sub-agent returns. This section is a single index of every loop the
harness runs — old and new — with the config knob that tunes each
one and the activity-log event you can grep for in `.clk/logs/`.

All knobs live under `clk.config.json::robustness.*` (and the
parallel `CLK_ROBUSTNESS_*` env-var family — see `.env.example`).
Every layer has an off-switch so you can throttle cost.

### 1. Provider retry (existing)

Transient provider errors (rate limits, timeouts, "no endpoints
available", HTTP 429) are retried with exponential backoff before the
response surfaces at the workflow layer.

- Code: `clk_harness/orchestration/agent.py::AgentRunner._should_retry_provider`
- Config: `clk.config.json::provider_retry.{max_retries, backoff_s}`
- Logged events: `provider_attempt`, `provider_retry`
- Kill switch: set `provider_retry.max_retries: 0`

### 2. Stage retry (existing)

When a workflow stage fails with a retryable provider error after the
inner provider-retry budget is exhausted, the workflow runner retries
the entire stage with a larger backoff before giving up on the stage.

- Code: `workflow.py::WorkflowRunner._is_retryable_stage_error`
- Config: `clk.config.json::provider_retry.{stage_max_retries, stage_backoff_s}`
- Logged events: `workflow_stage_retry`
- Kill switch: set `provider_retry.stage_max_retries: 0`

### 3. Supervise cycles (existing)

The chief's `supervise` stage decides whether the user's prompt has
been fully addressed; if not, it emits a `PROPOSE_WORKFLOW` and the
whole workflow re-runs. See **Chief supervisor loop** for the full
description.

- Config: `clk.config.json::supervise.max_cycles` (default 20)
- Kill switch: set `supervise.max_cycles: 1`

### 4. Recovery on unmet deps (existing)

When a stage's dependencies fail, the chief is dispatched in recovery
mode to re-cast, remediate, or accept the gap. See **Self-healing on
unmet deps**. This handles *dependency* failures; *content* failures
are handled by Layer 6 below.

- Config: `clk.config.json::recovery.max_per_stage` (default 3)

### 5. Review & checkpoint stages (existing)

Stages marked `phase: review` automatically receive a chief-authored
review prompt containing the upstream stages' POST blocks, and the
chief emits a verdict (continue / redirect / abort). Stages marked
`careful: true` add a post-stage checkpoint and (when configured)
trigger meta-prompt drafting on dispatch.

Example:

```yaml
- id: design_spec
  agent: architect
  careful: true
  outputs: [design_brief]
  objective: Draft the API contract.
- id: review_design
  agent: chief
  phase: review
  depends_on: [design_spec]
```

- Config: `clk.config.json::review.per_stage` (apply to *every* stage)
- Logged events: `workflow_checkpoint`, `consensus_coalesced`

### 6. Auto-quality re-dispatch (new)

After every dispatch, the response is scored against
`response_quality`:

- empty / sub-threshold text
- malformed `ACTION:` or `POST:` blocks
- missing declared `outputs` (the stage's contract keys)
- self-reported low confidence (`CONFIDENCE: <0..1>` parsed from the
  response)
- refusal patterns (treated as not-recoverable — surfaces to the
  chief instead of retrying blindly)

Recoverable failures are re-dispatched with a repair preamble that
quotes the specific reasons back to the worker, up to
`robustness.max_quality_retries`. On the final retry, when
`auto_consensus` is not `"off"`, the dispatch escalates to a
stochastic consensus fan-out rather than another single-shot retry.

- Code: `orchestration/response_quality.py`, `agent.py::_dispatch_with_quality_loop`
- Config: `robustness.{max_quality_retries, min_response_chars}`
- Logged events: `agent_quality_retry`, `agent_quality_final`
- Kill switch: `robustness.max_quality_retries: 0`

### 7. Stochastic consensus, opt-in + automatic (existing + new)

Any agent can emit `PROPOSE_CONSENSUS` to fan a question into N
independent samples; the harness runs them in parallel, logs them,
and dispatches the chief to coalesce. New in this release:
`robustness.auto_consensus` makes the fan-out automatic.

| `auto_consensus`         | Behavior                                                                 |
|--------------------------|--------------------------------------------------------------------------|
| `off`                    | Only `PROPOSE_CONSENSUS` triggers fan-out (legacy behavior).             |
| `on_careful` *(default)* | Stages marked `careful: true` fan out automatically.                     |
| `always`                 | Every non-chief dispatch fans out (×N samples — most expensive setting). |

Cost: a fan-out costs roughly N + 1 dispatches (N samples + 1 chief
coalescing). Caps at `consensus.max_samples` (default 6) and
`consensus.max_parallel` (default 4).

- Logged events: `consensus_started`, `consensus_sample_dispatch`,
  `consensus_samples_completed`, `consensus_coalesced`
- Kill switch: `robustness.auto_consensus: "off"`

### 8. Inter-agent clarifying Q&A (new)

Agents emit:

```
POST: question
TO: architect
URGENCY: blocking
BODY:
Are user IDs opaque strings or integers?
END_POST
```

With `URGENCY: blocking`, the harness dispatches the target peer
immediately to answer; the peer's `POST: answer` lists the question
id in its `CONSUMES`, and the asker sees the answer in the next
blackboard digest. `URGENCY: async` records the question for the
chief to schedule in a later cycle.

Chain depth is capped at `robustness.max_qa_depth` (default 3) so a
question can't trigger an unbounded chain of clarifications.

- Code: `agent.py::_route_blocking_questions`, `blackboard.py`
- Config: `robustness.{max_qa_depth, qa_parallel_judges}`
- Logged events: `qa_dispatch`, `qa_chain_capped`, `qa_chain_cycle`,
  `qa_target_unknown`
- Kill switch: omit the `TO:` field in your `POST: question` blocks;
  no protocol-level off-switch (Q&A is opt-in per post).

### 9. Critic-judge refinement (new)

Stages may declare a refinement loop that threads a critic between
worker rounds. The critic scores the worker's output 0..1; if below
the accept threshold, the worker is re-dispatched with the critic's
feedback until accept or `max_rounds` is reached.

```yaml
- id: design_spec
  agent: architect
  refine:
    critic: critic
    max_rounds: 4
    accept_threshold: 0.8
  objective: Draft the spec.
```

When the stage has no explicit `refine:` block, `robustness.auto_refine`
decides whether one round runs anyway:

| `auto_refine`              | Behavior                                                |
|----------------------------|---------------------------------------------------------|
| `off`                      | Only stages with `refine:` use the inner loop.          |
| `careful_only`             | Stages marked `careful: true` get one critic pass.      |
| `all` *(default)*          | Every non-chief, non-qa, non-critic stage gets a critic pass — so refinement fires without relying on the chief marking stages careful. |

The critic's last two lines must be:

```
VERDICT: accept   # or `revise`
SCORE: <0..1>
```

- Code: `workflow.py::WorkflowRunner._refine_loop`
- Config: `robustness.{auto_refine, refine_max_rounds,
  refine_accept_threshold}`
- Logged events: `refine_critic_verdict`
- Kill switch: `robustness.auto_refine: "off"` AND remove any
  `refine:` blocks from your workflow YAML.

### 10. Adaptive Ralph & autoresearch (new)

Both loops record every iteration's outcome to
`.clk/state/experiments.jsonl`. After `robustness.plateau_window`
consecutive iterations without measurable improvement, the loop:

1. **Escalates** — the next iteration's dispatches carry
   `careful=true` in their extra, which (via Layer 7) fans them into
   stochastic consensus.
2. **Reframes** — the chief is dispatched with a "plateau dispatch"
   prompt asking it to re-cast roles or re-author the workflow with a
   qualitatively different approach (new metric, new experiment
   family) rather than another marginal tweak.
3. **Terminates gracefully** — if escalation + reframe fail to break
   the plateau across two more iterations, `done.md` is written with
   reason "plateau" rather than burning the full iteration budget.

Regression (last iteration failed after at least one earlier success
in the window) triggers an additional critic dispatch on the failing
diff before the next plan, so the next iteration starts from an
informed view of what broke.

Autoresearch additionally gains an evaluator gate (previously only in
Ralph): if the analyst's writes break the build, the working tree is
reverted rather than committed.

Both loops also short-circuit when a planner or surveyor returns
empty / unrecoverable output; rather than commit garbage, the
iteration is recorded with `improved=False`.

- Code: `ralph_loop.py::RalphLoop`, `autoresearch_loop.py::AutoresearchLoop`
- Config: `robustness.{plateau_window, plateau_action}`
  (`escalate_then_reframe` | `escalate_only` | `reframe_only` | `off`)
- Logged events: `ralph_plateau_detected`, `ralph_plateau_escalate`,
  `ralph_plateau_terminated`, `ralph_regression_detected`,
  `ralph_iteration_skipped_low_quality`,
  `autoresearch_step_skipped_low_quality`, `autoresearch_revert`
- Kill switch: `robustness.plateau_action: "off"`

### 11. Adversarial debate panel (new)

Instead of a single critic, a stage can be refined by a **panel of
adversarial critics** that each take a distinct lens, try to *break* the work,
and engage with each other across rounds before the worker revises. This
catches failure modes a single reviewer misses (a correctness reviewer won't
think like a security reviewer).

Each round fans out one critic per lens **in parallel** (reusing the
`critic` agent with a lens-specific adversarial prompt); the worker output is
kept only when a **majority of lenses accept** and the mean score clears
`refine_accept_threshold`. Otherwise the combined critiques drive a revision,
and the next round's critics see the prior panel transcript (posted to the
blackboard as `post_type: debate`) so they can reinforce, refute, or concede
each other's points. Bounded by `debate_max_rounds`.

```yaml
- id: implement
  agent: engineer
  refine:
    mode: debate
    critics: [correctness, security, performance]
    max_rounds: 2
  objective: Implement the slice.
```

When a stage has no explicit `refine: {mode: debate}`, `robustness.debate`
decides whether the panel runs anyway (`off` | `careful_only` *(default)* |
`all`). The debate panel takes precedence over the single-critic loop
(layer 9) when both would apply. Built-in lenses: `correctness`, `security`,
`simplicity`, `performance`, `robustness`, `tests`, `ux` (configure via
`robustness.debate_lenses`).

- Code: `workflow.py::WorkflowRunner._debate_loop` / `_dispatch_lens_critic`
- Config: `robustness.{debate, debate_lenses, debate_max_rounds}`
- Logged events: `debate_round`
- Kill switch: `robustness.debate: "off"` AND remove any `refine: {mode: debate}` blocks.

### 12. Gauntlet loop (new)

Every layer above judges output against a critic's in-the-moment opinion,
so "good" gets invented after the work is already done. The gauntlet
inverts that order: **the acceptance criteria are written down before the
work is judged**, and the result is verified against those same criteria
rather than against a fresh opinion.

It wraps *every* non-meta dispatch — workflow stages, Ralph and
autoresearch iterations, mission phases, `DELEGATE` children, consensus
winners, and TUI/Telegram/WebUI dispatches alike — because they all pass
through `AgentRunner.run`.

One turn through the gauntlet:

1. **Answer key.** The worker's own `ANSWER_KEY:` block is used when it
   emitted one (free — every bundled prompt now teaches the grammar).
   Otherwise one `phase: gauntlet_key` dispatch derives it.
2. **Candidate 0.** The existing dispatch path, unchanged. Auto-consensus
   (layer 7) and the quality-retry loop (layer 6) still run underneath.
3. **Adversarial critique.** `phase: gauntlet_critique`, judged against the
   key, each finding classified material or non-material.
4. **Revise and iterate.** `phase: gauntlet_revise`, until no material
   defect remains or the preset's round cap is reached.
5. **Final verification.** `phase: gauntlet_verify` against the original
   objective plus every key check, with exactly one bounded repair.

Critics end their response with three lines the harness parses:

```
MATERIAL_DEFECTS: <integer>
VERDICT: accept   # or: revise
SCORE: <0..1>
```

`MATERIAL_DEFECTS: 0` converges even without an `accept` verdict — a clean
critique is a valid outcome, and cosmetic nits alone must not buy another
expensive round. Conversely an `accept` scored below
`accept_threshold` is *not* treated as an accept, and an unparseable
critique fails closed to `revise` rather than passing the work through.

Presets cap the critique/revision rounds:

| `preset`              | Rounds | Lenses                                                        |
|-----------------------|--------|---------------------------------------------------------------|
| `quick`               | 1      | requirements, correctness                                     |
| `standard` *(default)*| 3      | + reasoning, hidden assumptions, edge cases, feasibility       |
| `rigorous`            | 5      | + counterexamples, internal consistency, evidence quality, …   |

Rounds stop early on a clean critique, so the cap is a worst case rather
than the usual spend. `gauntlet.max_rounds` overrides the preset's cap for a
single dispatch (`0` = use the preset, so the default resolves to 3).

**The session budget.** The round cap bounds one dispatch and resets on the
next, so on its own it does not bound a long mission with hundreds of
stages. `gauntlet.max_dispatches` (default **500**, `0` = unlimited) caps the
gauntlet's dispatches across the whole session. Once spent, dispatches
return their candidate unwrapped and the loop logs
`gauntlet_budget_exhausted` — work already done is kept, nothing is lost.

**Interaction with layer 9.** The gauntlet already threads a critic, so
`gauntlet.supersede_auto_refine` (default true) retires the
`auto_refine`-driven critic pass rather than critiquing the same work
twice. An explicit `refine:` block in workflow YAML is user intent and
still runs; set `supersede_auto_refine: false` to stack both.

**Safety.** The loop never loses work: a failed candidate, an empty
candidate, a critic that raises, a critic that returns nothing, or a
revision that fails all fall back to the best run already in hand. The
gauntlet can only improve a dispatch or leave it untouched.

Set the intensity four ways, highest precedence first:

```bash
clk --no-gauntlet run              # or: clk run --no-gauntlet
clk run --gauntlet-preset rigorous
clk run --gauntlet-rounds 2            # exact round cap, ignoring the preset
clk run --gauntlet-max-dispatches 100  # session budget (0 = unlimited)
GAUNTLET_LOOP=False clk run        # or CLK_ROBUSTNESS_GAUNTLET=off
/gauntlet off                      # in the TUI, at runtime
/clk-gauntlet rigorous             # in the Pi extension, at runtime
```

`kickoff.sh --setup` also asks whether to run the gauntlet and at which
preset.

- Code: `orchestration/gauntlet.py`, `agent/runner.py::_maybe_gauntlet`
  (mirrored by `pi-extension/src/gauntlet.ts`)
- Config: `clk.config.json::gauntlet.{enabled, preset, max_rounds,
  max_dispatches, scope, exclude_agents, critic, answer_key,
  final_verification, accept_threshold, supersede_auto_refine, focus}`
- Logged events: `gauntlet_started`, `gauntlet_key`, `gauntlet_critique`,
  `gauntlet_converged`, `gauntlet_round_cap`, `gauntlet_verify`,
  `gauntlet_repaired`, `gauntlet_final`, `gauntlet_budget_exhausted`
- Kill switch: `--no-gauntlet`, `GAUNTLET_LOOP=False`, or
  `gauntlet.enabled: false`

### Putting it together

A typical "careful" engineering stage now runs:

1. Stage dispatched with `careful: true`.
2. `auto_consensus=on_careful` → N samples fan out in parallel.
3. Chief coalesces the samples.
4. The gauntlet (layer 12) wraps the result: acceptance criteria →
   adversarial critique → revision → verification. Because it ran,
   `supersede_auto_refine` retires the `auto_refine` critic pass, so the
   work is critiqued once rather than twice. (With the gauntlet disabled,
   `auto_refine=all` scores the coalesced output instead and the worker is
   revised until the critic accepts or `max_rounds` is hit.)
5. Stage validation runs.
6. Checkpoint (if enabled) — chief CONTINUE / REDIRECT / ABORT
   verdict.
7. Outputs contract check; warn if any declared key was not posted.

Tracing this in `.clk/logs/`:

```
grep -E '"event":"(consensus_|refine_|gauntlet_|workflow_checkpoint|agent_quality_)' \
    .clk/logs/activity.jsonl | jq .
```
## Completion criteria

CLK considers the system "done" when `.clk/state/done.md` exists. By
convention you create it only when:

- the MVP runs locally,
- the test suite passes,
- the README explains setup,
- a deployment plan exists,
- a deployment checklist exists,
- at least one user-facing interaction path exists.
