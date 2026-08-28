# What's new

Part of the [CLK documentation](../README.md). Release highlights, most recent first.

## What's new

If you've used CLK before, the highlights of this release:

- **The gauntlet loop — criteria before judgement, on every agent.** Every
  other critique layer judged output against a critic's in-the-moment
  opinion, so "good" got invented after the work was already done. The
  gauntlet inverts that: each agent and sub-agent writes **checkable
  acceptance criteria before its work is judged** (an `ANSWER_KEY:` block
  every bundled prompt now teaches), a critic then attacks the result
  against those criteria, the agent revises, and a final pass verifies —
  catching work that looks finished but quietly dropped a requirement.
  It wraps *every* dispatch, in both the Python harness and the Pi
  extension, and it never loses work: a broken critic, an empty critique,
  or a failed revision all fall back to the best run in hand.
  On by default at the `standard` preset (3 critique rounds, stopping
  early on a clean critique). Turn it off or retune it four ways:
  `clk --no-gauntlet <cmd>`, `GAUNTLET_LOOP=False`, `/gauntlet off` in the
  TUI, or `/clk-gauntlet off` in Pi — and `kickoff.sh --setup` now asks.
  Because it already threads a critic, it retires the `auto_refine` pass
  by default rather than critiquing the same work twice. See
  [Robustness loops](MISSIONS.md#12-gauntlet-loop-new).
- **Autonomous missions — one prompt to done.** `clk run` (and the TUI's first
  message) now drive the whole lifecycle autonomously: the chief writes a
  **charter**, authors a **living plan**, and walks discovery → … →
  deployment through chief-evaluated phase gates to a **code-gated** done.
  Reliability is enforced, not hoped for: a machine-checkable **done-gate**
  (tests/qa/ralph/deliverables, adaptive for test-less projects) makes
  `ACTION:done` a *request*; a **no-op guard** re-dispatches stages that changed
  no files; evaluation **auto-derives** a real command instead of vacuously
  passing; refinement is on for every producing stage by default; agents
  **deliberate** (blocking Q&A + self-reflection); and every boundary leaves a
  structured **git commit trace** with a per-cycle telemetry line. Use
  `clk mission "<idea>"` for the explicit form or `clk run --once` for a single
  cycle. See [Autonomous missions](MISSIONS.md#autonomous-missions).
- **Web dashboard (`clk web`).** A beautiful browser UI that mirrors the
  TUI: configure every feature and `.env` setting, kick off workflows,
  and watch the agents work in real time with live cards, a colour-coded
  activity timeline, and animated token/cost meters. See
  [Web dashboard](WEBUI.md#web-dashboard).
- **Guided mode.** A beginner-friendly step-by-step wizard in the web
  console: scan for available LLM providers, pick a model, describe your
  idea in plain language, watch a friendly progress view, browse and
  download the files, then loop with follow-up requests. First-time
  visitors land here automatically; the full console is one click away.
- **Files tab with git history.** Browse the live workspace, toggle to a
  commit **History** view (agent badge, relative time, +/− stats, colored
  diff per commit), time-travel any single file to a past version, and
  see **uncommitted changes** as a pseudo-entry with new/modified/deleted
  badges — files changed since the last commit carry an amber dot.
- **Work is never silently lost.** Failed stage validations no longer
  hard-reset the workspace by default (`validation.rollback_on_failure:
  careful` — only `careful: true` stages roll back, and even then the
  discarded work is preserved behind a `refs/clk/rollbacks/` snapshot
  ref). Agent `PATH:`s are resolved chroot-style, so absolute paths no
  longer cause writes to be silently skipped.
- **A chief that keeps going.** Supervise/review prompts now carry an
  explicit low-bar-to-continue / high-bar-to-stop asymmetry, stalled
  cycles trigger a one-shot chief **stall rescue** before the loop gives
  up (`supervise.stall_rescue`), unmet outputs contracts dispatch a
  chief recovery pass, and dynamic agents receive the full ACTION/POST
  protocol automatically so first dispatches comply.
- **Robustness loops by default.** Every meaningful dispatch is now
  scored after the provider returns; empty / malformed / contract-
  violating / low-confidence responses are re-dispatched with a repair
  preamble, escalating to a stochastic consensus fan-out on the final
  retry. Stages marked `careful: true` fan into N parallel samples
  proactively (configurable via `robustness.auto_consensus`). The
  critic-judge inner loop (`refine:` stage attribute, or default-on
  for careful stages) drives draft → critic → revise until the critic
  signs off. Ralph and autoresearch detect plateau / regression and
  escalate-then-reframe instead of burning the full iteration budget.
  Agents can ask peers directed clarifying questions via
  `POST: question TO: <peer> URGENCY: blocking` and the harness
  routes the answer inline. Everything is gated by
  `clk.config.json::robustness.*` (or `CLK_ROBUSTNESS_*` env vars) so
  you can throttle cost — see **Robustness loops** below.
- **The setup wizard explains itself.** `kickoff.sh --setup` is now a
  series of explain-then-ask blocks (provider, loop settings, tool
  detection, telegram, GitHub, git identity) — every question is
  preceded by a short block telling you what the value does. Modeled
  on `scripts/install_local.sh`'s narration style.
- **Tool auto-install.** Pick a provider whose CLI isn't installed and
  the wizard surfaces the canonical install command (`npm install -g
  …`, `curl -fsSL https://ollama.ai/install.sh | sh`, etc.) and asks
  before running it. The same registry powers `/install` from inside
  the TUI.
- **First-use configuration.** After install, every tool goes through
  the same four-step shape: auth → upstream route → model → verify.
  Pi prompts for its upstream provider (openrouter / anthropic /
  openai / google) and the right env-var receives your API key.
  Ollama runs `ollama list`, lets you pick a local model or pull a
  new one with progress streaming. Re-run any time via `/configure
  [tool]`.
- **GitHub integration.** The wizard offers to skip, link an existing
  repo, or create a new private one. A hardened `.gitignore` and a
  pre-push secret scanner protect against accidental `.env` /
  API-key leaks. `CLK_GITHUB_PUSH_ON_COMMIT=true` makes each agent
  commit push automatically.
- **Friendlier TUI.** First-run welcome banner, `/help` modal
  overlay (F1 or `?`), state-aware hint bar above the input,
  in-title USD cost estimate, narrative status snapshots, and
  follow-on suggestions after every workflow and loop ("next:
  `/loop ralph 5` to refine, `/undo` to revert, or type a follow-up
  message"). The user always knows the next move.
- **Recoverability everywhere.** Atomic `.env` and JSON writes with
  `.bak` rotation; `kickoff.sh --restore` swaps it back. Per-step
  resume in the wizard via `.clk/.setup-progress`. Crashed-session
  detection in the TUI surfaces "recovered from a crashed session"
  and points at the preserved `conversation.md`. `/undo` reverts the
  last clk-authored commit after explicit confirm.
- **`/doctor` and `/diag`.** Health-check every provider and config;
  `--fix` prompts before repairing. `/diag` builds a redacted
  tarball for bug reports — API keys are replaced with
  `<redacted: N chars>`.
- **`/tutorial`.** A 30-second sample idea against the `shell`
  provider so first-time users see agents working end-to-end without
  spending a cent.
- **Workspace management.** `./kickoff.sh --list`, `--clean 7d`,
  `/workspaces` inside the TUI. Old kickoff dirs no longer pile up.
- **Always-confirm policy.** Every install, push, undo, ollama pull,
  cost-cap crossing, or `--clean` removal asks `[y/N]` every single
  time. There is no "remember my answer" setting — by design.

See the **Recoverability**, **GitHub integration**, **Diagnostics**,
**Workspaces**, and **Cost guardrails** sections below for the full
walkthroughs.
