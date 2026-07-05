# Providers

Part of the [CLK documentation](../README.md). Provider adapters, authentication modes, Ollama, the Pi provider, and the Pi extension.

## Providers

| Provider    | Detection                                | Notes |
|-------------|------------------------------------------|
`shell`     | always available                         | dummy; echoes prompts and writes stub files. Use for tests, CI, dry runs. |
| `claude`    | `claude` on PATH                         | runs `claude --print` non-interactively. Add `"args": ["--print", "--output-format", "json"]` to `providers.json` to get real token counts. |
| `codex`     | `codex` on PATH                          | runs `codex exec`. |
| `gemini`    | `gemini` on PATH                         | runs the Google Gemini CLI; prompt fed on stdin. |
| `pi`        | `pi` on PATH or `.clk/tools/pi/bin/pi`   | pi.dev terminal harness; supports model selection, OpenRouter, and any API-key provider. See below. |
| `ollama`    | TCP reachable at `endpoint`              | local-only LLM via HTTP. **Use a ≥14B model** (e.g. `qwen3:14b`) — see [Ollama provider](#ollama-provider) for why. |
| `openwebui` | TCP reachable at `endpoint`              | any OpenAI-compatible server. Configure `endpoint`, `api_key`, `model` in `providers.json`; kickoff offers a numbered model picker fetched from `/api/models`. |

`./scripts/clk providers` prints availability as JSON. Customize per
provider in `.clk/config/providers.json`.

### Authentication: CLI vs API key

For the CLI-driven providers (`claude`, `codex`, `gemini`) you can
choose how authentication works at kickoff:

- `CLK_AUTH_MODE=cli` (default) — spawn the provider's local CLI as a
  subprocess and trust whatever auth that CLI already has. If you've
  run `claude login` / `codex login` / Gemini sign-in, no API key is
  required and kickoff will *not* prompt for one. Persisted to
  `providers.json` as `"mode": "cli"`.
- `CLK_AUTH_MODE=apikey` — call the upstream HTTP API directly (no
  local CLI is spawned at all). Kickoff prompts for the standard env
  var (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `GEMINI_API_KEY` /
  `GOOGLE_API_KEY`) and stores it in `providers.json` along with
  `"mode": "api"`. Each provider has a built-in HTTP client (Anthropic
  Messages, OpenAI Chat Completions, Gemini `generateContent`).

The other providers don't need this knob: `shell` and `ollama` are
local, `openwebui` uses an explicit bearer token, and `pi` has its own
authentication model described below.

### Ollama provider

Ollama is local and free — no API key, no rate limits — which makes
it tempting to default to. The catch is that **CLK asks the chief to
emit machine-parseable YAML workflows**, and small open-weight models
(≤8B parameters) are inconsistent at this. Specifically, the chief
will occasionally produce a `PROPOSE_WORKFLOW` block where a list
item contains an unquoted colon (e.g. `[type:finding,
stage:create_file]`), which YAML can't parse.

What you'll see when this happens:

```
[workflow] PROPOSE_WORKFLOW parse failed: mapping values are not
allowed here :: keeping prior workflow
[supervise] cycle N/M no progress (workflow still has zero new stages)
```

The harness handles this safely — it detects the bad YAML, refuses
to clobber the existing workflow file, falls back to the bundled
engineering template, and the supervise loop keeps the run alive
until its cap. But the visible symptom is a loop that "spins" without
forward progress, which is frustrating.

**Recommendation: use `qwen3:14b` or larger as the minimum.** It
follows the structured-output instructions reliably enough that the
chief's proposals parse on the first try. Pull it with:

```bash
ollama pull qwen3:14b
```

Other ≥14B options that work well: `llama3.1:70b`, `qwen2.5-coder:32b`,
`deepseek-r1:14b`. Models ≤8B (`llama3.2`, `gemma2`, `qwen2.5:7b`,
`phi3`) are fine for chat but flaky for workflow generation — they'll
get through some cycles cleanly but fail the YAML contract often
enough that the loop won't make steady progress.

Memory rule of thumb: a 14B Q4 model needs ~10 GB of RAM/VRAM; 32B
needs ~20 GB; 70B needs ~40 GB. The setup wizard's ollama section
streams `ollama pull` progress so you can see download size before
it lands.

### Pi provider

`pi` (from [pi.dev](https://pi.dev)) is an extensible terminal
harness. CLK drives it as a subprocess, piping the prompt on stdin and
capturing stdout as the agent response.

**Model selection**

Pass a model to `pi` via `CLK_PI_MODEL`:

```bash
CLK_PI_MODEL=openrouter/free      # free tier via OpenRouter
CLK_PI_MODEL=openrouter/auto      # let OpenRouter pick the best available free model
CLK_PI_MODEL=anthropic/claude-3-5-sonnet  # specific model via OpenRouter
```

Leave `CLK_PI_MODEL` blank to use pi's own active profile or default.
The value is forwarded to pi as `pi --model <value>`.

**API keys**

Pi reads provider-specific environment variables — one per backend.
Two settings control this:

| Setting | Purpose |
|---|---|
| `CLK_PI_KEY_TYPE` | The provider your key belongs to (default: `openrouter`) |
| `CLK_PI_API_KEY` | The actual key value |

The harness derives the env var name by convention:
`{CLK_PI_KEY_TYPE.upper()}_API_KEY`. So:

| `CLK_PI_KEY_TYPE` | Env var set for pi |
|---|---|
| `openrouter` | `OPENROUTER_API_KEY` |
| `openai` | `OPENAI_API_KEY` |
| `anthropic` | `ANTHROPIC_API_KEY` |
| `mistral` | `MISTRAL_API_KEY` |
| any future provider | `{NAME}_API_KEY` automatically |

This means new providers require no code changes — just set
`CLK_PI_KEY_TYPE` to the provider name and `CLK_PI_API_KEY` to your key.

Leave `CLK_PI_API_KEY` blank if you have already run `pi login` and pi
has its own stored credentials.

**Interactive pi setup**

If you need to run `pi login`, configure a profile, or verify your
setup interactively, kickoff offers to open pi's TUI before launching
the harness. You'll be prompted at the end of the pi configuration
questions during both `--setup` and a normal kickoff run (when `pi` is
on PATH). Exit pi normally when done and kickoff will continue.

This is useful for first-time Docker sessions where pi has no stored
credentials yet:

```bash
# Run the setup wizard — it will offer to open pi if found on PATH
./kickoff.sh --setup
```

Pi's own state (credentials, profiles) is stored in pi's own config
directory (e.g. `~/.pi/`) — no extra Docker volume is required for
CLK's harness state, but if you want pi credentials to persist across
container restarts, mount the pi config directory:

```bash
docker run --rm -it \
  -v ~/.pi:/root/.pi \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=pi \
  -e CLK_PI_MODEL=openrouter/free \
  -e CLK_PI_KEY_TYPE=openrouter \
  -e OPENROUTER_API_KEY=sk-or-... \
  clk "My idea"
```

Alternatively, pass the API key directly via `CLK_PI_API_KEY` and skip
`pi login` altogether — kickoff will set the right env var for you.
## Pi extension

A native [pi.dev](https://pi.dev) extension that brings the full CLK
orchestration model — dynamic casting, stochastic consensus, Ralph
refinement, and Karpathy-style autoresearch — into Pi behind a single
`/clk` command. No Python harness required at runtime.

The TypeScript extension now ports the harness's response-quality
scoring and consensus fan-out as **real tools** (`clk_consensus`,
`clk_subagent_quality`, `clk_autoresearch`, `clk_ralph`) rather than
relying on chief compliance — every parallel sample is scored by the
same rules `clk_harness/orchestration/response_quality.py` uses, the
winner is picked in code, and Ralph branches are created by the tool so
the protocol can't be skipped.

It also ports the **supervise loop** as a run watchdog: every chief
turn that ends without `clk_done` gets re-prompted with the run state,
consecutive no-progress turns trigger a one-shot stall-rescue prompt,
and a cycle cap bounds token spend — so a run keeps iterating without
the user babysitting it. `clk_merge`/`clk_done` accept `validate` shell
commands and **refuse** on a non-zero exit, and `clk_ralph` refuses a
fourth identical attempt after three consecutive reverted iterations
(plateau guard). See [`pi-extension/README.md`](pi-extension/README.md)
for the full tool reference, state layout, error handling, and
customisation notes.

**Requirements:** Pi on `PATH`; tmux on `PATH`; Git on `PATH`.

**Install:**

| Option | Command | When to use |
|--------|---------|-------------|
| Quick test | `pi -e /path/to/CognitiveLoopKernel/pi-extension/src/index.ts` | Try it out; reloads on `/reload` |
| Project-local | `mkdir -p .pi/extensions && ln -s /path/to/CognitiveLoopKernel/pi-extension .pi/extensions/clk` | Version-controlled per project |
| Global | `mkdir -p ~/.pi/agent/extensions && ln -s /path/to/CognitiveLoopKernel/pi-extension ~/.pi/agent/extensions/clk` | Available in every Pi session |

**Commands:**

| Command | Effect |
|---------|--------|
| `/clk <idea>` | Capture the idea and hand off to the chief. The watchdog keeps the chief iterating until `clk_done`. |
| `/clk-resume` | Continue an interrupted run (session restart, abort, or watchdog stall-stop) from persisted state with a fresh stall budget. |
| `/clk-abort` | End the active run. State is preserved; `/clk-resume` continues it later. |
| `/clk-help` | List every CLK slash command, every orchestration tool the chief uses, and the active safety nets. |
| `/clk-doctor` | Health-check tmux, git, the workspace `.clk/` layout, the pre-push hook, and (when a remote exists) the count of local commits not yet pushed. |
| `/clk-undo` | Preview the last CLK commit; `/clk-undo confirm` creates a revert commit on top of it. |

**Orchestration tools the chief uses (you don't call these directly):**

| Tool | Purpose |
|---|---|
| `clk_cast` | Persist a roster of project-specific specialist roles. |
| `clk_subagent` | Raw single-subagent dispatch via a detached tmux pi session. |
| `clk_subagent_quality` | One subagent + automatic repair-preamble re-rolls on quality failures. |
| `clk_consensus` | Fan out N parallel samples (default 3, max 6), score each, return the winner plus every candidate's score. |
| `clk_autoresearch` | Bounded researcher + critic alternation; each iteration recorded on the progress log. |
| `clk_ralph` | Create a `ralph/<iter>` branch and run a consensus fan-out in one call; chief then calls `clk_merge` or `clk_revert`. Refuses a 4th attempt after 3 consecutive reverts (plateau guard) until the chief acknowledges with a different approach. |
| `clk_branch` / `clk_merge` / `clk_revert` / `clk_checkpoint` | Git plumbing for the Ralph iteration cycle. `clk_merge({ validate })` runs the command first and refuses the merge on a non-zero exit. |
| `clk_progress` | Append a one-line entry to `.clk/state/progress.md`. |
| `clk_done` | Mark the run complete and write `.clk/state/done.md`. `clk_done({ validate: [...] })` refuses completion while any command fails. |

**Optional env vars:**

| Variable | Effect |
|---|---|
| `CLK_GITHUB_PUSH_ON_COMMIT=true` | After every `clk_checkpoint` and `clk_merge`, run `git push origin HEAD` best-effort and surface an `↑N` ahead counter if the push fails. Same env var as the Python TUI. |
| `CLK_STALL_CAP` | Consecutive no-progress chief turns before the watchdog's one-shot stall-rescue prompt (default 3). |
| `CLK_MAX_AUTO_CONTINUES` | Hard cap on watchdog auto-continuations per run (default 100) — the extension's `supervise.max_cycles`. |

A typical session:

```text
> /clk a local-first journaling app that summarizes my week
[CLK run started. The chief is taking over.]
[chief casts engineer, ux_writer, summarizer, qa]
[chief calls clk_consensus({agent:"architect", samples:3, task:"... storage design ..."})]
[harness fans out 3 parallel tmux pi subagents, scores each, returns the winner]
[chief calls clk_autoresearch({question:"sync model: append-only vs CRDT?"})]
[chief calls clk_ralph({iterationName:"iter-1-mvp", agent:"engineer", task:"... build MVP ..."})]
[chief calls bash: pytest -q]
[chief calls clk_merge: "ralph win: MVP capture+persist+summarize"]
[chief calls clk_done: "MVP runs; tests pass; README + deploy plan present"]
```
