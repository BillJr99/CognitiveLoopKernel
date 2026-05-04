# CLK as a Pi extension

A lightweight [pi.dev](https://pi.dev) extension that brings the Cognitive Loop
Kernel orchestration model — dynamic agent casting, stochastic consensus,
Ralph refinement, and Karpathy-style autoresearch — into Pi behind a single
`/clk` command.

> **Experimental.** Companion to the Python [CLK harness](../README.md) in the
> parent repo, but standalone: this extension does not depend on that harness
> at runtime. It targets Pi natively. Use at your own risk.

## What it does

You type:

```text
/clk a local-first journaling app that summarizes my week
```

The extension:

1. Captures the idea, initialises a git repo if needed, and persists state
   under `.clk/state/`.
2. Hands control to the chief LLM with a CLK operator's manual (see
   [`src/prompts.ts`](src/prompts.ts)) that establishes standing rules:
   cast a team, dispatch via the `subagent` tool, apply parallel consensus
   on high-stakes decisions, run Ralph refinement after MVP, autoresearch
   on open questions, checkpoint after every win, revert on regression,
   call `clk_done` when every completion criterion is met.
3. Provides the chief with seven small tools — `clk_cast`, `clk_progress`,
   `clk_checkpoint`, `clk_branch`, `clk_revert`, `clk_merge`, `clk_done` —
   that handle persistence and git mechanics. `clk_branch` opens a per-
   iteration feature branch before each Ralph pass, `clk_merge` folds it into
   the home branch on success, and `clk_revert` discards the branch without
   merging when the iteration is rejected. Everything else (dispatch, fan-out,
   judging, refinement loops) is the chief driving the standard
   Pi/pi-subagents tools.

The extension itself is intentionally thin: orchestration policy lives in the
chief's prompt, not in TypeScript. To change CLK's behavior, edit
[`src/prompts.ts`](src/prompts.ts).

## Requirements

- Pi installed and on `PATH` (`pi --version` works).
- The [`pi-subagents`](https://github.com/nicobailon/pi-subagents) extension,
  which provides the `subagent` tool the chief dispatches through. It's
  declared as an npm `dependency` of this extension and a `postinstall`
  hook runs `pi install npm:pi-subagents` on your behalf, so installing
  this extension via `pi install` registers it automatically. If you load
  the extension via symlink or `pi -e` (Options A/B/C below) — neither of
  which runs `npm install` — install it yourself:

  ```bash
  pi install npm:pi-subagents
  ```

  On `session_start` the extension checks whether `pi-subagents` is
  resolvable and, if not, emits a one-time warning notification with the
  install command above.

- Git on `PATH` (the extension auto-runs `git init` in the project root if
  there's no repo yet).
- Node 20+ (Pi already requires this; only relevant if you want
  `AbortSignal.any` for the cleanest cancel behavior).

## Install

Three options. Pick whichever matches your workflow.

### Option A: Quick test (`-e`, no install)

Best for trying it out or iterating on the extension itself. Pi loads the
file directly and reloads on `/reload`:

```bash
pi -e /path/to/CognitiveLoopKernel/pi-extension/src/index.ts
```

### Option B: Project-local install

Per-project, version-controlled with the project that uses it:

```bash
mkdir -p .pi/extensions
ln -s /path/to/CognitiveLoopKernel/pi-extension .pi/extensions/clk
```

Pi auto-discovers `.pi/extensions/*/index.ts` on startup. The chief's tools
appear in every Pi session opened in this project.

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

| Command         | Effect                                                                 |
|-----------------|------------------------------------------------------------------------|
| `/clk <idea>`   | Capture the idea and hand off to the chief. Resumes if state exists.   |
| `/clk-abort`    | End the active run. Cancels the chief's current turn and signals all in-flight subagents to stop. State on disk is preserved; you can `/clk` again later. |

Cancel mid-turn with **Esc** (Pi's built-in) — that cancels the current model
call but leaves the CLK run lifecycle intact, so the chief can be steered and
continue. Use `/clk-abort` when you want to end the whole run.

A typical first transcript looks like:

```text
> /clk a local-first journaling app that summarizes my week
[notification] CLK run started. The chief is taking over.
[chief] (calls clk_cast with engineer, ux_writer, summarizer, qa)
[chief] (calls subagent: scout to understand existing layout)
[chief] (calls subagent x3 in parallel: 3 architectures for storage)
[chief] (calls subagent: oracle to judge architectures)
[chief] (calls clk_progress: consensus → SQLite + JSON sidecar)
[chief] (calls subagent: worker to implement MVP)
[chief] (calls bash: pytest -q)
[chief] (calls clk_checkpoint: "MVP: capture + persist entries")
[chief] (enters Ralph loop ...)
...
[chief] (calls clk_done: "MVP runs; tests pass; README + deploy plan + checklist + CLI all present")
```

## State on disk

Everything CLK persists lives under `.clk/`:

```
.clk/
  state/
    idea.json      # captured idea + timestamp
    roster.json    # current cast: name, mission, persona per role
    progress.md    # human-readable timeline (one line per event)
    clk.json       # full state snapshot (idea + roster + progress)
    done.md        # written only when clk_done is called
  logs/            # reserved for future per-command logs
```

The roster, progress log, and full snapshot are also written to Pi's session
JSONL via `pi.appendEntry` — so they're replayed automatically when you
resume a session, and they survive a `pi --resume`.

Git commits made by `clk_checkpoint` carry a `[clk]` prefix and are real
commits in the project repo. The chief uses them as Ralph-style baselines and
reverts to them on regression.

## What you keep from the original CLK

- **Single command, idea-first.** `/clk <idea>` is the only entry point.
- **Dynamic casting.** The chief invents project-specific roles on the fly
  with personas and missions it authors itself, persisted to `roster.json`.
- **Stochastic consensus.** High-stakes decisions fan out to parallel
  candidates (Pi runs sibling tool calls concurrently by default), then a
  judge subagent picks or synthesizes.
- **Ralph refinement loop.** Pre-iteration checkpoint → dispatch → validate
  → commit-or-revert. Failed iterations leave no trace in the working tree.
- **Autoresearch loop.** When stuck on open questions, the chief designs and
  runs small experiments (researcher / scout / spike) and records learnings
  regardless of outcome.
- **Self-healing.** Repeated failure triggers consensus on root cause and
  optionally a fresh `clk_cast` to add a specialist who can fix the upstream
  issue.

## What changes from the original CLK

| Original CLK | Pi extension |
|---|---|
| Provider-agnostic (claude/codex/gemini/ollama/openwebui/pi) | Tied to Pi |
| Curses TUI dashboard with live agent cards | Pi's single conversation stream + status-line entries |
| `ACTION:` block protocol for write/edit/append/delete/run | Pi's built-in `read`/`write`/`edit`/`bash` tools |
| YAML workflows in `.clk/config/workflows/` | None — the chief decides workflow on the fly |
| Per-agent prompt files in `.clk/prompts/` | One operator's manual in `src/prompts.ts`; per-role personas live in `roster.json` |
| Subprocess-piped agents | In-session and pi-subagents children |

## Customising orchestration

All policy lives in [`src/prompts.ts`](src/prompts.ts). Edit that file and
`/reload` to change behavior. Useful knobs:

- Add or remove standing rules.
- Change consensus sample counts (default: 3–5).
- Change Ralph soft cap (default: ~10 iterations per stretch).
- Change completion criteria.
- Change how the chief prefixes dynamic personas onto `delegate` tasks.

The `clk_*` tools are intentionally minimal mechanics. Resist the urge to
encode policy in them — Pi extensions get the most leverage when the LLM
makes the decisions and the extension just provides primitives + persistence.

## Error handling and resilience

The extension is designed to survive transient provider problems without ending
the run. Errors are classified into four categories, each with a defined
recovery path:

| Category | Symptoms | Recovery |
|----------|----------|----------|
| **Rate limit** | HTTP 429, "too many requests", "quota exceeded" | Exponential backoff, retried indefinitely (delay capped at 5 minutes) until the run is aborted. The chief is also instructed to try a smaller / different model if the limit persists. |
| **Model unavailable** | HTTP 404, "model not found", "not available on free tier" | No retry — the chief falls back to a built-in Pi agent (`worker`, `researcher`, `scout`, `oracle`) or omits `preferredModel` and lets Pi choose. |
| **Privacy redaction** | `[REDACTED]` values, "privacy filter", "sensitive content blocked" | Tool params are checked for redaction markers before use; the tool returns a recovery hint asking the chief to retry without the sensitive field (or to write it to a file and pass the path). |
| **Max turns exhausted** | "max turns reached", "turn limit", "turn cap", "no more turns" | The chief re-dispatches the identical `subagent` call immediately without asking for confirmation. If the task exhausts turns twice in a row the chief splits it into two narrower sequential subtasks. |
| **Network / transient** | ECONNRESET, ETIMEDOUT, "socket hang up" | Same backoff-and-retry as rate limits. |

### Where this is enforced

- **`src/errors.ts`** — `classifyError` (now includes `max_turns`), `isRetryable`,
  `looksRedacted`, `isMaxTurnsResult`, `withRetry` (exponential backoff helper),
  and `recoveryHint` (human-readable guidance returned to the chief as tool output).
- **`src/index.ts`** — `pi.sendUserMessage` (the call that hands off to the
  chief) is wrapped with `withRetry`; abort-caused errors are distinguished
  from real errors so the run lifecycle is handled correctly.
- **`src/tools.ts`** — every `clk_*` tool `execute` function checks input
  parameters for redaction before acting and returns a descriptive error result
  (rather than throwing) when git operations fail, so the chief can decide how
  to proceed.
- **`src/prompts.ts`** — rule 8 (max-turns: re-dispatch immediately or split
  the task) and rule 10 (other provider errors) in the chief's operator's manual
  instruct it how to handle error results from `subagent` calls (which happen
  inside Pi's runtime and cannot be intercepted in TypeScript).

### Design principle

A single failed subagent call or tool invocation must never end the run. The
extension recovers what it can in TypeScript, then surfaces a recovery hint to
the chief so it can adapt its plan. Use `/clk-abort` when you intentionally
want to stop.

## Limitations / gotchas

- **Subagent depth is capped.** The extension sets
  `PI_SUBAGENT_MAX_DEPTH=3` so consensus operations can spawn a judge
  subagent that itself uses helpers. If you find the chief running into
  depth caps, raise it via the env var.
- **Children don't have CLK tools.** Spawned child sessions don't get
  `subagent`, `clk_*`, or the pi-subagents skill (pi-subagents enforces
  this). The chief is the sole orchestrator. Don't try to delegate
  orchestration.
- **Concurrency lock.** Only one `/clk` run can be active per Pi session.
  Use `/clk-abort` first if you want to start over with a different idea.
- **`ctx.signal` is undefined when `/clk` fires** (the extension is
  invoked while Pi is idle), so the extension manages its own
  `AbortController` and merges it with per-tool signals. Esc + `/clk-abort`
  + session shutdown all wire through correctly.
- **No web TUI.** Pi runs in your terminal; this extension inherits that.
  The agent dashboard from the Python CLK is replaced by status-line
  entries (`clk-roster`, `clk-head`, `clk-last`, `clk-run`, `clk-done`).

## Repository layout

```
pi-extension/
  README.md
  package.json         # devDeps for editor type-checking; pi loads via jiti
  tsconfig.json
  src/
    index.ts           # entry: factory, /clk + /clk-abort, session_start replay
    prompts.ts         # the chief's operator's manual (the policy)
    tools.ts           # clk_cast, clk_progress, clk_checkpoint,
                       # clk_branch, clk_revert, clk_merge, clk_done
    state.ts           # .clk/state/* persistence + pi.appendEntry mirroring
                       # (tracks idea, roster, progress, homeBranch)
    git.ts             # checkpoint, revertTo, head, abortMerge helpers
    abort.ts           # run-scoped AbortController + /clk-abort + shutdown bridge
    errors.ts          # error classification, backoff retry, redaction detection
    types.ts           # shared types
```

## License

MIT. See the parent repo for the full notice.
