# Cognitive Loop Kernel (CLK)

Local-only multi-agent development harness. Drop `clk` into an empty
directory, capture an idea, and let a team of agents iterate the idea
into a working system through repeated agentic development cycles. The
chief casts the team dynamically per project, the agents emit machine-
parsed `ACTION:` blocks that the harness executes, and every change is
committed automatically.

## Why CLK

- **Local-first.** Everything lives under `.clk/` in the project
  directory. No global installs, no `sudo`.
- **Provider-agnostic.** Works with Claude Code, OpenAI Codex, Google
  Gemini, OpenWebUI (any OpenAI-compatible HTTP server), Pi, local
  Ollama, or a built-in dummy "shell" provider for testing.
- **Dynamic team.** A baseline of three agents (`chief`, `qa`, `ralph`)
  ships with the harness; the chief invents project-specific specialists
  on the fly — including `engineer` when an implementer is needed — writes
  their prompts, and authors the workflow YAML that wires them together.
- **Real actions, not just descriptions.** Agents emit `ACTION:` blocks
  (write/edit/append/delete/run/done) that the harness applies with
  path-safety checks, automatic backups, and per-agent git commits.
- **Self-healing.** When a stage's dependencies fail, the chief is
  dispatched in recovery mode (capped) to fix or re-cast rather than
  silently skipping.
- **Iterative by design.** Ships with Archon-style YAML workflows and
  a Ralph/gnhf-style improvement loop; the same ralph agent also drives
  Karpathy-style autoresearch cycles when the state has open questions.
- **Memory through git.** Every successful milestone (and every action
  batch) is committed with a structured message so future agent runs
  can mine the log for context. A separate `.clk/state/casting.log`
  records every roster decision, and `.clk/logs/session.log` mirrors
  the TUI status pane.

## Quick start

The fastest path is the kickoff script, which copies the harness into a
fresh `kickoff-<timestamp>/` directory, gives it its own git repo, and
launches the TUI dashboard. The source tree is never modified.

```bash
# Optional: copy .env.example to .env to set defaults non-interactively.
./kickoff.sh "A local-first journaling app that summarizes my week"

# Or omit the prompt and type your idea into the TUI:
./kickoff.sh
```

The TUI shows live agent cards (idle / working / done / failed), a
status log that updates in place, and a Claude-Code-style ``>`` input
field. Use it to type follow-ups; each message dispatches another
engineering cycle so the agents react to the new context.

| TUI command                          | Effect |
|--------------------------------------|--------|
| free text                            | first message becomes the idea, then auto-runs casting + `engineering`; later messages append to the conversation and re-cast + re-run |
| `/idea <text>`                       | replace the captured idea |
| `/cast`                              | force a fresh chief casting pass against the current state |
| `/roles list`                        | print the current roster (baseline + dynamic) |
| `/roles add NAME "role description"` | add a dynamic role (the chief usually does this for you) |
| `/roles drop NAME`                   | remove a dynamic role (baseline cannot be removed) |
| `/run [workflow]`                    | run a single workflow cycle (default `engineering`) |
| `/loop ralph 5`                      | start a Ralph refinement loop with 5 iterations |
| `/loop autoresearch 3`               | start a Karpathy-style research loop (ralph agent, research mode) |
| `/stop`                              | request the active loop to stop after the current iteration |
| `/abort`                             | SIGTERM any running CLI subprocess (use when an agent is genuinely hung; the heartbeat tells you when this is likely) |
| `/provider <name>`                   | switch the active provider (shell, claude, codex, gemini, pi, ollama, openwebui) |
| `/status`                            | log a status snapshot |
| `/quit`                              | exit the TUI |

PgUp/PgDn scroll the log pane; Backspace edits the input; Enter sends.
The input area wraps when you type past one row and the status log
word-wraps every entry. The bottom band shows running totals:
`agents=N :: tokens=Xk (in=Y / out=Z) :: peak_run=P :: files=N`.

CLI providers (`claude`, `codex`, `gemini`, `pi`) stream their
subprocess stdout/stderr live: every line the CLI prints (auth status,
"Connecting...", retries, etc.) appears in the status pane within
milliseconds, and each agent card has a "live" rotating view showing
PID + bytes received + the most recent line. The heartbeat fires every
~15s while an agent is working and tells you whether the subprocess is
actively streaming or silent — and if it's been silent for more than
two minutes it suggests typing `/abort`. So you can immediately tell
"this is just a slow model call" from "this is genuinely hung."

### Lower-level CLI

If you'd rather drive the harness without the TUI:

```bash
./scripts/install_local.sh           # local pip install (optional)
./scripts/clk init
./scripts/clk idea "A local-first journaling app that summarizes my week"
./scripts/clk plan
./scripts/clk run
./scripts/clk loop --max-iterations 10
./scripts/clk status
./scripts/clk providers
```

Set `CLK_NO_TUI=true` in your environment (or `.env`) to make `kickoff.sh`
fall back to this non-interactive pipeline.

The shell/dummy provider is the default and always works, so you can
exercise the entire harness with no API keys. Switch providers by
editing `.clk/config/providers.json`, via the TUI's `/provider` command,
or:

```bash
./scripts/clk configure --set default_provider=claude
```

## Docker

The harness ships with a `Dockerfile`. Kickoff directories are created under
`workspace/` inside the container; mount a volume there to keep them after
the container exits.

The default mode is the interactive TUI dashboard — run with `-it` so the
container has a terminal. If no `.env` is present it will prompt for provider
and settings before launching. Pass your idea as the first argument to skip
the prompt and go straight to the engineering workflow.

### First-run setup

Run the setup wizard to create your `.env` before starting a session. The
wizard copies `.env.example` → `.env` (if absent), then walks you through
every setting: provider, API keys, git identity, etc.

Inside Docker the `.env` lives at `/app/.env`, which is outside the workspace
volume. Bind-mount a file on your host so the config persists across runs:

```bash
# Create an empty config file on the host (once)
touch ~/clk.env

# Run the wizard — writes into the bind-mounted file
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk --setup

# Subsequent runs load the config automatically
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk "My idea here"
```

`--setup` also works locally (outside Docker) and updates `./kickoff.sh`'s
own `.env` in-place.

### Build

```bash
docker build -t clk .
```

### Run (interactive TUI — default)

**Named volume** — kickoffs persist in a Docker-managed volume across runs:

```bash
docker volume create clk-workspace

docker run --rm -it \
  -v clk-workspace:/app/workspace \
  clk "A local-first journaling app that summarises my week"
```

**Host directory** — kickoffs written directly to a directory on your machine:

```bash
docker run --rm -it \
  -v /path/to/my/projects:/app/workspace \
  clk "A local-first journaling app that summarises my week"
```

**Anonymous volume** — Docker allocates a temporary volume that is
automatically removed when the container exits (`--rm` handles cleanup):

```bash
docker run --rm -it \
  -v /app/workspace \
  clk "A local-first journaling app that summarises my week"
```

**Ephemeral** — no volume at all; kickoffs exist only inside the container's
writable layer and are lost when it exits:

```bash
docker run --rm -it clk "A local-first journaling app that summarises my week"
```

### Provider and authentication

Pass any `CLK_*` variable or API key with `-e`:

```bash
docker run --rm -it \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=claude \
  -e CLK_AUTH_MODE=apikey \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  clk "A local-first journaling app that summarises my week"
```

For `ollama` or `openwebui` running on the host, use `host.docker.internal`
as the endpoint (macOS/Windows) or `--network host` (Linux):

```bash
docker run --rm -it \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=ollama \
  -e CLK_OLLAMA_ENDPOINT=http://host.docker.internal:11434 \
  clk "A local-first journaling app that summarises my week"
```

### Non-interactive / CI mode

For scripted or CI use, skip the TUI entirely. The pipeline runs
`init → idea → plan → run → loop` without any curses UI:

```bash
docker run --rm \
  -v clk-workspace:/app/workspace \
  -e CLK_NO_TUI=true \
  -e CLK_PROVIDER=claude \
  -e CLK_AUTH_MODE=apikey \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  clk "A local-first journaling app that summarises my week"
```

## Layout

The package itself:

```
clk_harness/
  cli.py                 # argparse entrypoint
  config.py              # paths, default configs, JSON load/save
  git_ops.py             # init, commit, revert, status helpers
  providers/             # claude, codex, pi, ollama, shell adapters
  orchestration/         # agent runner, workflow runner, ralph loop (refinement + autoresearch)
  templates/             # bundled prompts and workflows
  utils/                 # logging
scripts/
  clk                    # launcher (prefers .clk/venv/bin/python)
  install_local.sh       # creates .clk/venv and installs PyYAML
  run_loop.sh            # convenience wrapper around clk loop
```

The harness state, written by `clk init` and grown by every command:

```
.clk/
  config/
    clk.config.json      # project-wide config (incl. casting + recovery caps)
    providers.json       # provider registry + active provider
    agents.json          # agent -> prompt + provider mapping (mutable)
    workflows/*.yaml     # Archon-style workflows (chief authors per project)
  prompts/               # editable prompt templates (one per agent;
                         # dynamic roles get a generated file here)
  state/
    idea.json            # captured idea
    system_brief.md      # initial brief
    prd.json             # product manager output
    progress.md          # human-readable timeline
    decisions.md         # decisions log
    experiments.jsonl    # per-iteration outcomes
    agent_memory.jsonl   # all agent invocations (incl. token usage)
    casting.log          # JSONL of every roster decision (add/update/remove)
    done.md              # written only when completion criteria met
  logs/
    activity.jsonl       # detailed agent activity log
    session.log          # mirror of the TUI status pane
    <cmd>-<ts>.log       # per-command log files
  runs/                  # per-invocation prompt + response capture
  tools/                 # locally-cloned external tools (e.g. pi)
  venv/                  # local python venv
  backups/               # safety copies of overwritten files (per run)
```

## Providers

| Provider    | Detection                                | Notes |
|-------------|------------------------------------------|-------|
| `shell`     | always available                         | dummy; echoes prompts and writes stub files. Use for tests, CI, dry runs. |
| `claude`    | `claude` on PATH                         | runs `claude --print` non-interactively. Add `"args": ["--print", "--output-format", "json"]` to `providers.json` to get real token counts. |
| `codex`     | `codex` on PATH                          | runs `codex exec`. |
| `gemini`    | `gemini` on PATH                         | runs the Google Gemini CLI; prompt fed on stdin. |
| `pi`        | `pi` on PATH or `.clk/tools/pi/bin/pi`   | extensible terminal harness. |
| `ollama`    | TCP reachable at `endpoint`              | local-only LLM via HTTP. |
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

The other providers don't need this knob: `shell` and `pi` have no
remote auth, `ollama` is local, and `openwebui` always uses an explicit
bearer token configured at kickoff time.

## Layout

The kickoff dir lays the agents' work out as a normal project tree
with all harness machinery folded under `.clk/`:

```
kickoff-<ts>/
  src/, tests/, README.md ...   # the project the agents are building
                                # (agents write directly to project root)
  scripts/clk                   # convenience launcher shim
  KICKOFF.md                    # provenance manifest
  .clk/                         # ALL harness state — sandboxed off
    harness/clk_harness/        # harness sources copied from parent
    harness/scripts/            # original launcher / installer
    harness/pyproject.toml      # package metadata for pip install -e
    config/                     # clk.config.json, providers.json, agents.json
    state/                      # idea.json, prd.json, decisions.md ...
    prompts/                    # per-agent system prompts
    blackboard/                 # cross-agent shared scratchpad (POST blocks land here)
    runs/                       # per-dispatch prompt + response logs
    backups/                    # pre-write copies of mutated files
    cache/, logs/, venv/        # local-only artifacts
```

ACTION blocks resolve relative to the project root. The harness rejects
any path that resolves into `.clk/` so agents can't accidentally (or
intentionally) write into harness state. `run` commands cwd into the
project root. To share findings across agents, workers emit POST
blocks; the harness routes those into `.clk/blackboard/` even though
agents cannot write there directly.

The kickoff `.gitignore` keeps `.clk/` out of git except for the
curated state files (`idea.json`, `system_brief.md`, `prd.json`,
`decisions.md`, `progress.md`, `casting.log`, `done.md`, plus the
blackboard) so `git log` in the kickoff dir tells the project's story
without harness chatter. Deleting `.clk/` resets the harness without
touching the project tree.

## Chief supervisor loop

The default `engineering` workflow ends with a `supervise` stage where
the chief evaluates whether the user's prompt has been fully addressed.
The chief either:

- emits `ACTION: done` with a one-line reason — writes
  `.clk/state/done.md` and terminates the loop, or
- emits `PROPOSE_WORKFLOW` with the next iteration's stages — the
  workflow runner picks them up and runs another cycle.

So no agent is ever truly "done" until the chief signals completion.
Capped at `clk.config.json::supervise.max_cycles` (default 5) to avoid
runaway loops.

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

## Action protocol

Agents drive real changes by emitting `ACTION:` blocks the harness
parses and applies — descriptions alone do nothing. Supported kinds:

- `ACTION: write` / `edit` / `append` / `delete` — file mutations
  (paths must resolve inside the project root; originals are backed up
  to `.clk/backups/<run_id>/`).
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
`.clk/state/done.md` is created.

## Completion criteria

CLK considers the system "done" when `.clk/state/done.md` exists. By
convention you create it only when:

- the MVP runs locally,
- the test suite passes,
- the README explains setup,
- a deployment plan exists,
- a deployment checklist exists,
- at least one user-facing interaction path exists.

## Customization

- Edit prompts in `.clk/prompts/` to change agent behavior.
- Edit `.clk/config/agents.json` to bind specific agents to specific
  providers (e.g. `engineer` -> `claude`, `researcher` -> `ollama`).
- Edit `.clk/config/workflows/*.yaml` to add new stages or new
  workflows. Reference any new workflow with `clk run --workflow NAME`.
- `clk configure --set key=value` updates `.clk/config/clk.config.json`.

## Safety

- Failed work is never silently deleted. The Ralph loop reverts via
  `git reset --hard <pre-iter-sha>`; failed agent outputs remain in
  `.clk/runs/<run_id>/`.
- Operations that touch more than 5 files are logged before execution
  (warning) and refused above 25 (configurable).
- All exceptions are logged with `[location] message` and a full
  traceback.

## Dry-run mode

Every loop and workflow command accepts `--dry-run`. Providers honor it
and skip side effects. Use it to preview prompt rendering and stage
ordering without writing files or committing.

## License

MIT.
