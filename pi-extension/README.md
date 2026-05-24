# CLK as a Pi extension

A [pi.dev](https://pi.dev) extension that brings the full Cognitive Loop
Kernel orchestration model — dynamic agent casting, stochastic
consensus, Karpathy-style autoresearch, and Ralph refinement — into Pi
behind a single `/clk` command.

> **Experimental.** Companion to the Python [CLK harness](../README.md)
> in the parent repo, but standalone: this extension does not depend on
> that harness at runtime. It is a self-contained TypeScript port of
> the parts of `clk_harness/orchestration/` that make sense inside Pi.
> Use at your own risk.

## What it does

You type:

```text
/clk a local-first journaling app that summarizes my week
```

The extension:

1. Captures the idea, initialises a git repo if needed, and persists
   state under `.clk/state/`.
2. Installs hardened safety nets in the project (`.gitignore`,
   pre-push secret-scan hook).
3. Hands control to the chief LLM with an operator's manual (see
   [`src/prompts.ts`](src/prompts.ts)) that establishes standing rules
   for casting, dispatching, consensus, autoresearch, Ralph
   refinement, checkpointing, completion criteria, and error recovery.
4. Provides the chief with a suite of orchestration tools (see
   [Tool reference](#tool-reference)) that fan out parallel subagent
   samples via tmux, score every output with the same response-quality
   rules the Python harness uses, manage git branches for Ralph
   iterations, and persist progress.

Unlike the original incarnation of this extension, **orchestration
policy is now enforced in code**, not only in the chief's prompt:
`clk_consensus` actually spawns N parallel tmux sessions and scores
them; `clk_subagent_quality` actually re-rolls failures with a repair
preamble; `clk_autoresearch` actually alternates a researcher and
critic; `clk_ralph` actually creates the branch and runs the fan-out.
The chief can't accidentally skip these steps by misreading the
prompt — the tools enforce the shape.

## Commands

| Command | What it does |
|---|---|
| `/clk <idea>` | Start a CLK run. Casts a team, dispatches them, runs Ralph + autoresearch. |
| `/clk-abort` | End the active run. State is preserved for resume. |
| `/clk-help` | List every CLK command, every orchestration tool the chief uses, and the safety nets active in the workspace. |
| `/clk-doctor` | Health-check tmux, git, the `.clk/` layout, `.gitignore`, the pre-push hook, and (when a remote exists) the count of local commits not yet pushed. Pure environment checks; no Pi calls. |
| `/clk-undo` | Preview the last CLK commit; `/clk-undo confirm` creates a revert commit on top of it. Refuses if there are uncommitted changes. |

## Tool reference

The chief invokes these as `clk_*` tools — you do not call them from
slash commands. They are listed here so you know what your run is doing
when you read the progress log or notifications.

### Roster + status

| Tool | Purpose |
|---|---|
| `clk_cast` | Persist a roster of project-specific specialist roles (name, mission, persona). The chief authors the roster on the fly. |
| `clk_progress` | Append a one-line entry to `.clk/state/progress.md`. Used at every meaningful transition (dispatch / consensus / ralph / autoresearch / branch / merge / done / note). |

### Dispatch (pick the right one)

| Tool | Use when… |
|---|---|
| `clk_subagent({ agent, task, preferredModel? })` | Cheap, low-risk single-subagent dispatch with no quality gate. Reserve for genuinely throwaway work. |
| `clk_subagent_quality({ agent, task, maxRetries?, preferredModel?, minChars? })` | One subagent **scored by the quality detector**, with up to `maxRetries` automatic repair re-rolls. Default for any single-worker task where you'd rather catch bad output than propagate it. |
| `clk_consensus({ agent, task, samples?, preferredModel?, minChars? })` | Fan out N parallel samples (default 3, clamped 1..6), score each, return the highest-scoring winner plus every candidate. Use liberally for design choices, architecture, validation verdicts, security/perf reviews, ambiguous requirements. |
| `clk_autoresearch({ question, iterations?, preferredModel? })` | Bounded `researcher` + `critic` alternation (default 2 iterations, clamped 1..5). Each finding and critique is recorded on the progress log. Use before non-trivial implementation work whenever the optimal approach is unclear. |

### Iterative refinement

| Tool | Use when… |
|---|---|
| `clk_branch({ name })` | Manually open a feature branch for an iteration. Records the home branch automatically. |
| `clk_ralph({ iterationName, agent, task, samples?, preferredModel? })` | One-call Ralph iteration: creates `ralph/<iterationName>`, fans out a consensus dispatch, returns the winner. Chief then runs validation and calls `clk_merge` (accept) or `clk_revert` (reject). The branch creation + fan-out happen in one step and can't be skipped. |
| `clk_checkpoint({ message })` | Stage all working-tree changes and create a `[clk] <message>` commit. Returns the new HEAD SHA. When `CLK_GITHUB_PUSH_ON_COMMIT=true` and an `origin` remote exists, also runs `git push origin HEAD` best-effort. |
| `clk_merge({ message })` | Commit any pending changes on the feature branch, merge it into the home branch with `--no-ff`, return to home. Same auto-push behavior as `clk_checkpoint`. |
| `clk_revert({ reason })` | Commit any pending work on the rejected branch (preserving it), switch back to home without merging. The rejected branch is **never** deleted. |
| `clk_done({ reason })` | Mark the run complete. Writes `.clk/state/done.md`, ends the run lifecycle. Only call when every completion criterion in [`src/prompts.ts`](src/prompts.ts) is satisfied. |

### Response-quality scoring

Every `clk_subagent_quality`, `clk_consensus`, and `clk_autoresearch`
output is scored by [`src/quality.ts`](src/quality.ts) (TypeScript port
of `clk_harness/orchestration/response_quality.py`). The scorer flags:

- **empty** — body shorter than `minChars` (default 40)
- **refusal** — refusal phrases ("I cannot", "as an AI language model", ...) — marked non-recoverable so the harness escalates instead of re-rolling
- **malformed_action** — `ACTION:` headers without matching `END_ACTION`
- **malformed_post** — `POST:` headers without matching `END_POST`
- **outputs_missing** — declared output keys not present in any `POST` block's `PRODUCES:` list
- **low_confidence** — a parsed `CONFIDENCE: <0..1>` line below 0.5
- **needs_review_self** — a `NEEDS_REVIEW: true` line
- **confidence_missing** — no `CONFIDENCE:` line at all (only when the caller passes `requireConfidence: true`)

Each flag carries a short repair reason; on a recoverable failure the
caller re-dispatches with a preamble that quotes every reason back to
the worker so it fixes the specific issues rather than re-rolling at
random. Same protocol the Python harness uses in
`agent.py::_dispatch_with_quality_loop`.

## Safety nets

The extension installs the same safety nets the Python harness uses,
so running CLK from Pi is just as recoverable:

- **Hardened `.gitignore`.** On the first `/clk`, the extension writes
  a `.gitignore` that blocks `.env`, `.env.bak`, `.env.partial`,
  `*.pem`, `*.key`, `*_id_rsa*`, `/secrets/`, plus editor / OS junk.
  Existing `.gitignore` content is never clobbered.
- **Pre-push secret scanner.** A `.git/hooks/pre-push` hook (pure
  bash, no extra deps) scans the about-to-be-pushed objects for
  obvious API-key patterns (`ANTHROPIC_API_KEY=…`, `OPENAI_API_KEY=…`,
  `sk-…`, Slack `xoxb-…`, private-key headers). On a hit it aborts the
  push. Bypass once with `git push --no-verify`.
- **Atomic state writes.** Every state file under `.clk/state/`
  (`clk.json`, `idea.json`, `roster.json`, `done.md`) is written via
  `tmp+rename` with a `.bak` rotation, so a crash mid-write leaves
  either the old or the new file intact — never half.
- **`restoreBackup` primitive.** Exposed from `src/state.ts` for
  programmatic recovery of `.bak` snapshots.
- **AbortController cancellation.** `/clk-abort` and session shutdown
  fire a single abort signal that propagates to every in-flight tmux
  subagent session, every git operation, and every backoff sleep.

## Auto-push (opt-in)

Set `CLK_GITHUB_PUSH_ON_COMMIT=true` in the Pi environment to have the
extension auto-push after every `clk_checkpoint` and `clk_merge`. The
push is best-effort — on failure (no network, no upstream, rejected by
the pre-push hook), the run continues and the `clk-git` status bar
flips to `↑N` showing the count of unpushed commits.

When the env var is **unset** but the repo has an `origin` remote, the
`clk-git` status bar still surfaces an `↑N unpushed` hint so you know
what's accumulated locally. `/clk-doctor` includes the same count as
a `! warn` row.

The pre-push secret scanner runs *before* the auto-push leaves your
machine, so an accidental commit containing an API key still gets
blocked.

## Requirements

- Pi installed and on `PATH` (`pi --version` works).
- tmux installed and on `PATH` (`tmux -V` works). The extension spawns
  each subagent as a detached tmux pi session — this is how it
  achieves true process isolation without depending on any external
  Pi extension. Install: `brew install tmux` (macOS) or
  `apt install tmux` (Debian/Ubuntu). On `session_start` the
  extension checks for tmux and emits a one-time warning if it's
  missing.
- Git on `PATH` (the extension auto-runs `git init` in the project
  root if there's no repo yet).
- Node 20+ (Pi already requires this; only relevant if you want
  `AbortSignal.any` for the cleanest cancel behavior).

## Install

Three options. Pick whichever matches your workflow.

### Option A: Quick test (`-e`, no install)

Best for trying it out or iterating on the extension itself. Pi loads
the file directly and reloads on `/reload`:

```bash
pi -e /path/to/CognitiveLoopKernel/pi-extension/src/index.ts
```

### Option B: Project-local install

Per-project, version-controlled with the project that uses it:

```bash
mkdir -p .pi/extensions
ln -s /path/to/CognitiveLoopKernel/pi-extension .pi/extensions/clk
```

Pi auto-discovers `.pi/extensions/*/index.ts` on startup. The chief's
tools appear in every Pi session opened in this project.

### Option C: Global install (all projects)

Available everywhere you run Pi:

```bash
mkdir -p ~/.pi/agent/extensions
ln -s /path/to/CognitiveLoopKernel/pi-extension ~/.pi/agent/extensions/clk
```

Or list it explicitly in `~/.pi/agent/settings.json`:

```json
{
  "extensions": [
    "/path/to/CognitiveLoopKernel/pi-extension/src/index.ts"
  ]
}
```

## Usage

Cancel mid-turn with **Esc** (Pi's built-in) — that cancels the current
model call but leaves the CLK run lifecycle intact, so the chief can be
steered and continue. Use `/clk-abort` when you want to end the whole
run.

A typical first transcript looks like:

```text
> /clk a local-first journaling app that summarizes my week
[notification] CLK run started. The chief is taking over.
[chief] clk_cast({ engineer, ux_writer, summarizer, qa })
[chief] clk_consensus({ agent:"architect", samples:3,
                        task:"3 storage designs for offline-first journal" })
[harness] consensus :: 3 samples, winner #2 score=0.92 (all: #1=0.74 #2=0.92 #3=0.81)
[chief] clk_progress({ kind:"consensus", message:"3 samples for architect: ..." })
[chief] clk_autoresearch({ question:"sync model: append-only vs CRDT for journals?", iterations:2 })
[harness] autoresearch #1/2: investigating → critiquing
[harness] autoresearch #2/2: investigating → critiquing
[chief] clk_ralph({ iterationName:"iter-1-mvp", agent:"engineer", samples:3,
                    task:"... implement MVP from winning architecture ..." })
[harness] on branch ralph/iter-1-mvp, 3 samples, winner #1 score=0.88
[chief] bash({ command:"pytest -q" })
[chief] clk_merge({ message:"ralph win: MVP capture+persist+summarize" })
[harness] merged ralph/iter-1-mvp → main; clk-git: synced
... (Ralph iterations continue) ...
[chief] clk_done({ reason:"MVP runs; tests pass; README + deploy plan present" })
```

## State on disk

Everything CLK persists lives under `.clk/`:

```
.clk/
  state/
    idea.json      # captured idea + timestamp
    roster.json    # current cast: name, mission, persona per role
    progress.md    # human-readable timeline (one line per event)
    clk.json       # full state snapshot (idea + roster + progress + homeBranch)
    done.md        # written only when clk_done is called
    *.bak          # rotated previous version of any of the above
  subagents/<sid>/ # per-spawn scratch: task.md + stdout.txt; cleaned up on exit
  logs/
    <session-id>.log  # one log file per clk_subagent call; records spawn,
                      # tmux start/exit, abort, timeout, and the first 2000
                      # chars of output for post-mortem debugging
```

The roster, progress log, and full snapshot are also written to Pi's
session JSONL via `pi.appendEntry` — so they're replayed automatically
when you resume a session, and they survive a `pi --resume`.

Git commits made by `clk_checkpoint` and `clk_merge` carry a `[clk]`
prefix and are real commits in the project repo. The chief uses them
as Ralph-style baselines and reverts to them on regression.

## What you keep from the original CLK

- **Single command, idea-first.** `/clk <idea>` is the only entry
  point.
- **Dynamic casting.** The chief invents project-specific roles on the
  fly with personas and missions it authors itself, persisted to
  `roster.json`.
- **Stochastic consensus (code-enforced).** `clk_consensus` spawns N
  parallel tmux subagent samples, scores each via the same regex /
  contract rules the Python harness uses, and returns the highest-
  scoring winner. The chief can fan out at will rather than relying on
  the LLM to remember to emit parallel tool calls.
- **Quality re-dispatch (code-enforced).** `clk_subagent_quality` (and
  the consensus pipeline internally) re-roll on recoverable failures
  with a repair preamble that quotes the specific flags back to the
  worker.
- **Ralph refinement loop (code-enforced).** `clk_ralph` creates the
  feature branch and runs the fan-out in one tool call; the chief
  decides accept/reject afterwards via `clk_merge` / `clk_revert`.
  Failed iterations leave no trace on the home branch — the rejected
  branch is preserved for review and never deleted.
- **Karpathy-style autoresearch.** `clk_autoresearch` alternates a
  `researcher` and `critic` subagent for N bounded iterations,
  recording every finding and critique on the progress log.
- **Memory through git.** Every successful milestone is committed
  with a structured message so future agent runs can mine the log for
  context.

## What changes from the original CLK

| Original CLK (Python harness) | Pi extension |
|---|---|
| Provider-agnostic (claude / codex / gemini / ollama / openwebui / pi / shell) | Tied to Pi (which can route to its own upstream of choice). |
| Curses TUI dashboard with live agent cards + cost meter | Pi's single conversation stream + status-line entries (`clk-idea`, `clk-roster`, `clk-head`, `clk-branch`, `clk-last`, `clk-git`, `clk-run`, `clk-done`). |
| `ACTION:` block protocol for write / edit / append / delete / run | Pi's built-in `read` / `write` / `edit` / `bash` tools. |
| YAML workflows in `.clk/config/workflows/` driven by a workflow runner | The chief decides workflow on the fly using the orchestration tools. |
| Per-agent prompt files in `.clk/prompts/` | One operator's manual in `src/prompts.ts`; per-role personas live in `roster.json`. |
| Subprocess-piped provider adapters | tmux pi sessions (`clk_subagent` and the consensus fan-out spawn the same way). |
| Robustness loops gated by `clk.config.json::robustness.*` | The four orchestration tools (`clk_consensus`, `clk_subagent_quality`, `clk_autoresearch`, `clk_ralph`) implement the equivalent loops directly; their parameters (`samples`, `maxRetries`, `iterations`) act as per-call knobs. |
| `clk_harness/orchestration/response_quality.py` | Same rules, ported to `src/quality.ts`. |
| Telegram bot integration | Out of scope — use the Python harness for that. |
| REST API | Out of scope — use the Python harness for that. |

## Customising orchestration

Most policy still lives in [`src/prompts.ts`](src/prompts.ts) (when to
fan out, when to autoresearch, when to start Ralph, when to call
`clk_done`). Edit that file and `/reload` to change behavior.

Per-call parameters tune the in-code loops directly:

- `clk_consensus({ samples: 5 })` — 5 parallel samples (1..6).
- `clk_consensus({ minChars: 80 })` — stricter empty-flag threshold.
- `clk_subagent_quality({ maxRetries: 2 })` — up to 3 total dispatches.
- `clk_autoresearch({ iterations: 4 })` — 4 researcher+critic cycles.
- `clk_ralph({ samples: 5 })` — 5-way consensus per Ralph iteration.

The quality detector itself is configurable through
`scoreResponse(text, opts)` from [`src/quality.ts`](src/quality.ts) —
the same `ScoreOpts` shape is forwarded by every quality-gated tool.

## Error handling and resilience

The extension is designed to survive transient provider problems
without ending the run. Errors are classified into categories with a
defined recovery path:

| Category | Symptoms | Recovery |
|----------|----------|----------|
| **Rate limit** | HTTP 429, "too many requests", "quota exceeded" | Exponential backoff in `withRetry`, retried indefinitely (delay capped at 5 minutes) until the user aborts. The chief is also instructed to try a smaller / different model if the limit persists. |
| **Model unavailable** | HTTP 404, "model not found", "not available on free tier" | No retry — the chief falls back to a built-in Pi agent (`worker`, `researcher`, `scout`, `oracle`) or omits `preferredModel` and lets Pi choose. |
| **Privacy redaction** | `[REDACTED]` values, "privacy filter", "sensitive content blocked" | Tool params are checked for redaction markers before use; the tool returns a recovery hint asking the chief to retry without the sensitive field (or to write it to a file and pass the path). |
| **Max turns exhausted** | "max turns reached", "turn limit", "turn cap", "no more turns" | The chief re-dispatches the identical `clk_subagent` / `clk_subagent_quality` / `clk_consensus` call immediately. If the task exhausts turns twice in a row the chief splits it into two narrower sequential subtasks. |
| **Network / transient** | ECONNRESET, ETIMEDOUT, "socket hang up" | Same backoff-and-retry as rate limits. |
| **Quality-flagged output** | empty / malformed / contract-missing / low-confidence / NEEDS_REVIEW=true | `clk_subagent_quality` re-dispatches with a repair preamble up to `maxRetries`; `clk_consensus` picks the highest-scoring sample even if all are sub-threshold so the chief can see the spread and decide. Refusals are non-recoverable — surfaced to the chief instead of retried. |

### Where this is enforced

- **`src/errors.ts`** — `classifyError`, `isRetryable`, `looksRedacted`,
  `isMaxTurnsResult`, `withRetry` (exponential backoff), `recoveryHint`.
- **`src/quality.ts`** — `scoreResponse`, `repairHint`, `isRecoverable`,
  `summarise`.
- **`src/consensus.ts`** — `dispatchWithQuality` (single + retry),
  `runConsensus` (parallel fan-out + winner picking).
- **`src/index.ts`** — `pi.sendUserMessage` (the call that hands off to
  the chief) is wrapped with `withRetry`; abort-caused errors are
  distinguished from real errors so the run lifecycle is handled
  correctly.
- **`src/tools.ts`** — every `clk_*` tool `execute` function checks
  input parameters for redaction before acting and returns a
  descriptive error result (rather than throwing) when git operations
  fail, so the chief can decide how to proceed.
- **`src/prompts.ts`** — the chief's operator's manual still instructs
  how to react to error results from `clk_subagent` calls (Pi runtime
  errors that cannot be intercepted in TypeScript).

### Design principle

A single failed subagent call or tool invocation must never end the
run. The extension recovers what it can in TypeScript, then surfaces a
recovery hint to the chief so it can adapt its plan. Use `/clk-abort`
when you intentionally want to stop.

## Limitations / gotchas

- **Subagent depth is capped at one level.** Each spawned tmux pi
  session receives a preamble instructing it not to spawn further
  subagents and not to call `clk_*` tools. The chief (parent) may
  create grandchildren on a child's behalf — that is the maximum
  nesting depth. This is prompt-level enforcement, not a technical
  lock.
- **Concurrency lock.** Only one `/clk` run can be active per Pi
  session. Use `/clk-abort` first if you want to start over with a
  different idea.
- **Subagent timeout.** Each spawned tmux pi session has a 30-minute
  hard cap (`SUBAGENT_TIMEOUT_MS` in `src/subagent.ts`). Long-running
  experiments should be split into multiple bounded dispatches.
- **Output cap.** Subagent output is truncated at 80,000 characters
  before being returned to the chief; the first 2,000 characters of
  the full output are kept in `.clk/logs/<session>.log` for
  post-mortem.
- **No web TUI.** Pi runs in your terminal; this extension inherits
  that. The agent dashboard from the Python CLK is replaced by
  status-line entries.
- **`ctx.signal` is undefined when `/clk` fires** (the extension is
  invoked while Pi is idle), so the extension manages its own
  `AbortController` and merges it with per-tool signals. Esc +
  `/clk-abort` + session shutdown all wire through correctly.

## Repository layout

```
pi-extension/
  README.md
  package.json         # devDeps for editor type-checking; pi loads via jiti
  tsconfig.json
  src/
    index.ts           # entry: factory, /clk + /clk-abort + /clk-help +
                       #   /clk-doctor + /clk-undo, session_start replay
    prompts.ts         # the chief's operator's manual (the policy)
    tools.ts           # every clk_* tool — clk_cast, clk_progress,
                       #   clk_checkpoint, clk_branch, clk_revert,
                       #   clk_merge, clk_consensus, clk_subagent_quality,
                       #   clk_autoresearch, clk_ralph, clk_done
    subagent.ts        # clk_subagent + spawnSubagent (tmux pi spawner)
    consensus.ts       # dispatchWithQuality + runConsensus (parallel
                       #   sample fan-out + quality re-dispatch loop)
    quality.ts         # scoreResponse + repairHint (port of
                       #   clk_harness/orchestration/response_quality.py)
    git.ts             # init, checkpoint, branch, merge, revert,
                       #   safety-net installer, hasRemote, commitsAhead,
                       #   pushBestEffort (port of git_ops.py auto-push)
    state.ts           # .clk/state/* persistence + pi.appendEntry mirroring
                       #   (idea, roster, progress, homeBranch)
    abort.ts           # run-scoped AbortController + /clk-abort + shutdown bridge
    errors.ts          # error classification, backoff retry, redaction detection
    types.ts           # shared types (Roster, ProgressKind, ClkState)
  tests/
    errors.test.ts     # classifyError / withRetry / recoveryHint
    prompts.test.ts    # chief primer includes every clk_* tool name
    state.test.ts      # atomic writes + .bak rotation + round-trip
    git.test.ts        # real git binary: init, checkpoint, branch, merge,
                       #   revert, hasRemote, commitsAhead, pushBestEffort
    quality.test.ts    # every flag + repairHint + isRecoverable
    consensus.test.ts  # injected spawn: ok / retry / non-recoverable /
                       #   fan-out winner picking / clamping / errors /
                       #   maxParallel concurrency
    safety_nets.test.ts # gitignore + pre-push hook idempotence
    runtime_smoke.test.ts # real pi binary, when available (CI gates this)
    index.test.ts      # extension factory wires every tool + command,
                       #   firstLineShort handles multi-line ideas
```

## Testing

Inside `pi-extension/`:

```bash
npm ci                  # install dependencies
npm run typecheck       # tsc --noEmit
npm test                # 96 tests, ~2s, no network or pi required
npm run test:strict     # typecheck + tests in one go
```

The full suite runs entirely offline (consensus tests inject a fake
spawn function) so it's safe to run in CI without tmux or pi
installed. The `runtime_smoke.test.ts` self-skips when the real `pi`
binary isn't reachable.

## License

MIT. See the parent repo for the full notice.
