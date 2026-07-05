# Cognitive Loop Kernel (CLK)

Local-only multi-agent development harness. Drop `clk` into an empty
directory, capture an idea, and let a team of agents iterate the idea
into a working system through repeated agentic development cycles. The
chief casts the team dynamically per project, the agents emit machine-
parsed `ACTION:` blocks that the harness executes, and every change is
committed automatically.

> **Experimental software — use at your own risk.**
> CLK is a research prototype. It is not intended for, and has not been
> evaluated or deemed suitable for, any particular purpose, production
> use, or critical workload. No warranty is provided, express or implied.
> By using this software you accept all associated risks.
>
> Contributions, bug reports, and ideas are very welcome — feel free to
> open an issue or pull request!

## What's new

If you've used CLK before, the highlights of this release:

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
  cycle. See [Autonomous missions](#autonomous-missions).
- **Web dashboard (`clk web`).** A beautiful browser UI that mirrors the
  TUI: configure every feature and `.env` setting, kick off workflows,
  and watch the agents work in real time with live cards, a colour-coded
  activity timeline, and animated token/cost meters. See
  [Web dashboard](#web-dashboard).
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

## Pick your path

Skim this matrix to jump straight to the right tutorial. Every path
goes through the same `kickoff.sh --setup` wizard at some point, so once
you've configured CLK in one place you can mix and match the rest.

| Platform / mode                      | Tutorial                                                  |
|--------------------------------------|-----------------------------------------------------------|
| Local Linux / macOS / WSL (Python)   | [Quick start](#quick-start) → [Lower-level CLI](#lower-level-cli) |
| Browser dashboard (point & click)    | [Web dashboard](#web-dashboard)                          |
| Docker container (build locally)     | [Docker](#docker) → [First-run setup](#first-run-setup)   |
| Pre-built image from GHCR            | [Docker → Pull from GHCR](#pull-from-ghcr)                |
| Raspberry Pi (`pi` runtime)          | [Pi extension](#pi-extension)                             |
| REST API (drive CLK from code)       | [REST API](#rest-api)                                     |
| Chat-control from your phone         | [Telegram Bot](#telegram-bot)                             |

Every tutorial ends with a **"You should now see…"** verification step.
If something differs, check the **Troubleshooting** notes inline in the
section you followed.

## Quick start

The fastest path is `clk kickoff`, which copies the harness into a
fresh `workspace/kickoff-<timestamp>/` directory, gives it its own git repo, and
launches the TUI dashboard. The source tree is never modified.
(`./kickoff.sh` still works — it is now a thin wrapper that execs
`clk kickoff` with the same flags, so existing scripts keep running.)

> **Want chat control?** After running `--setup` once, see the
> [Telegram Bot](#telegram-bot) section to drive CLK from your phone with
> live status updates.

```bash
# Optional: copy .env.example to .env to set defaults non-interactively.
clk kickoff "A local-first journaling app that summarizes my week"

# First time? Run the setup wizard to create your .env:
clk kickoff --setup

# Or omit the prompt and type your idea into the TUI:
clk kickoff
```

`clk kickoff` reads all settings from `.env` (and optional CLI overrides) and
requires no interactive prompts during a normal run. If required config is
missing it prints exactly what's needed and offers to run `--setup` for you.

```bash
# CLI overrides (override any .env value for a single run)
clk kickoff --provider claude --max-iterations 10 "My idea"
clk kickoff --no-tui "My idea"

# Re-run setup at any time to update your .env:
clk kickoff --setup

# No install? ./kickoff.sh is an equivalent entry point from a bare clone.
./kickoff.sh --no-tui "My idea"
```

The TUI shows live agent cards (idle / working / done / failed), a
status log that updates in place, and a Claude-Code-style ``>`` input
field. Use it to type follow-ups; each message dispatches another
engineering cycle so the agents react to the new context.

| TUI command                          | Effect |
|--------------------------------------|--------|
| free text                            | first message becomes the idea, then auto-runs casting + `engineering`; later messages append to the conversation and re-cast + re-run |
| `/help` (or F1, or `?` when empty)   | open the in-place help overlay with every command listed |
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
| `/provider <name>`                   | switch the active provider; verifies it's reachable and warns if not |
| `/install [tool]`                    | install a missing provider CLI (claude, pi, ollama, …) via the registry in `scripts/install_tool.sh` |
| `/configure [tool]`                  | (re-)run a tool's first-use config — auth, upstream route, model picking |
| `/github`                            | inspect the current remote and link instructions for adding one |
| `/undo`                              | preview the last clk-authored commit; `/undo confirm` reverts it |
| `/doctor [--fix]`                    | health-check every provider, config, and git state; `--fix` prompts before repairing |
| `/diag`                              | bundle the logs, last 3 runs, and a redacted `.env` into `clk-diag-<ts>.tar.gz` for bug reports |
| `/tutorial`                          | run a 30-second sample idea on the `shell` provider — costs nothing |
| `/workspaces list\|rename\|switch\|clean` | manage past kickoff dirs under `workspace/` |
| `/status`                            | print a narrative session snapshot (idea, agents, tokens, files, per-provider cost) |
| `/quit`                              | exit the TUI |

PgUp/PgDn scroll the log pane; Backspace edits the input; Enter sends.
The input area wraps when you type past one row and the status log
word-wraps every entry. A one-line hint bar above the input adapts to
state: if no idea is captured yet it says "type your idea, or
`/tutorial`, or `/help`"; if a run failed with a missing CLI it says
"try `/install <provider>` to fix"; if an agent is working it points
at `/abort`. You always know your next move.

The title bar shows: project, active provider, current phase, total
tokens, **estimated USD cost for the session** (via the per-provider
table in `clk_harness/pricing.py`), files written, and a `↑N` counter
for commits not yet pushed to the GitHub remote (when configured).

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

## Web dashboard

Everything the TUI does, in your browser — and then some. The web
dashboard is a React single-page app served by CLK's own FastAPI server.
It lets you **configure every feature and `.env` setting**, **kick off
agent workflows**, and — the star of the show — **watch what the agents
are doing in real time** with live agent cards, a colour-coded activity
timeline, animated token/cost meters, and a prompt/response inspector.

### Launch it

```bash
pip install "clk-harness[api]"

# Build the UI bundle (needs Node/npm) the first time, then serve it.
clk web --build

# Subsequent runs (bundle already built) just serve it and open a browser:
clk web
```

`clk web` runs a uvicorn server (default `http://127.0.0.1:8001`) and
opens your browser. Flags:

| Flag         | Purpose                                                        |
|--------------|----------------------------------------------------------------|
| `--build`    | Compile the React bundle first (`npm ci && npm run build`).    |
| `--no-build` | Never auto-build, even if the bundle is missing.               |
| `--host`     | Bind host (default `CLK_API_HOST` or `127.0.0.1`).             |
| `--port`     | Bind port (default `CLK_API_PORT` or `8001`).                  |
| `--no-open`  | Don't open a browser window.                                   |

> The pre-built Docker image already contains the compiled bundle, so
> inside a container you can run `clk web --no-build` directly.

To open the dashboard from the Docker image without kicking off a run
(the default `kickoff.sh` entrypoint starts an agent — `clk web` does
not), override the entrypoint and publish the port:

```bash
docker run --rm -it \
  -p 127.0.0.1:8001:8001 \
  -e CLK_API_HOST=0.0.0.0 \
  -v "$(pwd):/workspaces" \
  --entrypoint clk \
  clk web --no-open --no-build
```

Then browse to `http://localhost:8001`. Notes:

- `--entrypoint clk` replaces `kickoff.sh`, so nothing runs until you
  press **Run** in the UI.
- `CLK_API_HOST=0.0.0.0` lets the container's server accept the forwarded
  connection (it binds loopback-only *inside* the container by default).
  The `127.0.0.1:` prefix on `-p` publishes the port to your host's
  loopback only, so the unauthenticated UI isn't reachable from other
  machines on your network. Drop the prefix (`-p 8001:8001`) only if you
  deliberately want LAN access and have firewalled appropriately.
- `-v "$(pwd):/workspaces"` bind-mounts the current directory as the
  workspace root — no named volumes — and `--rm` discards the image's
  anonymous volumes on exit.
- `--no-build` serves the bundle already baked into the image (no npm at
  runtime); `--no-open` skips the in-container browser launch.

### Tutorial: from `docker run` to your first shipped feature

This walks the whole loop — start the server, configure a provider, kick
off a job, and watch (and steer) the agents — entirely in the browser.

**1. Start the server.** Using the published image (nothing is built or
run until you ask):

```bash
docker run --rm -it \
  -p 127.0.0.1:8001:8001 \
  -e CLK_API_HOST=0.0.0.0 \
  -e CLK_ENV_FILE=/workspaces/.env \
  -v "$(pwd):/workspaces" \
  --entrypoint clk \
  ghcr.io/billjr99/cognitiveloopkernel:latest web --no-open --no-build
```

Open **http://localhost:8001**. The `CLK_ENV_FILE=/workspaces/.env` line
makes your settings persist to `./.env` on the host (see
[Configuring `.env`](#configuring-env-where-settings-live) below).

**2. Create a workspace.** In the left rail under **Workspaces**, click
the **＋** and name your project (e.g. `markdown-cli`). A workspace is one
isolated project directory; the whole UI focuses on one at a time.

**3. Configure a provider.** Open the **Configure** tab:
   - On **Providers**, pick the **active** provider and click **make
     active**. **This matters:** the default active provider is `shell` —
     a *stub that echoes prompts and never calls an LLM*. If you leave it
     on `shell`, runs will complete instantly and "do things" without ever
     touching your model (the Health strip flags this). Choose a real
     provider (claude / codex / gemini / pi / ollama / openwebui).
   - For HTTP providers (**ollama**, **openwebui**), set the `endpoint`,
     then click **models** next to the `model` field — CLK probes the
     endpoint and offers a **dropdown of installed models** (falling back
     to a text box if the endpoint is unreachable, which also tells you
     the server isn't reachable from where CLK runs).
   - On **.env (global)**, set any keys your provider needs — e.g.
     `ANTHROPIC_API_KEY` (secrets show as `••••••••` and are preserved on
     save). Click **Save**.
   - Auth: set `CLK_AUTH_MODE` to `apikey` to use the keys above, or `cli`
     to trust a provider CLI you've already logged in to.

   > **Running CLK in Docker with a local Ollama/OpenWebUI?** A
   > `localhost` endpoint points at the *container*, not your host. CLK
   > auto-retries `host.docker.internal`, but the host must be reachable —
   > run the container with `--add-host=host.docker.internal:host-gateway`
   > on Linux (Docker Desktop adds it automatically). The model dropdown is
   > the quickest way to confirm the endpoint resolves.

**4. Kick off a job.** Open the **Run** tab:
   - Type your idea / problem statement (e.g. *"Build a Markdown-to-HTML
     CLI with a parser, renderer, and golden-file tests"*).
   - Choose a mode: **Run workflow** (one development cycle — pick a
     workflow like `engineering`), **Loop** (iterative ralph /
     autoresearch with an iteration count), **Plan** (discovery + product
     passes), or **Set idea** (just capture it).
   - Click **Start**. A raw-output panel streams stdout; the structured
     view comes alive on the other tabs.

**5. Watch the team work.** Three tabs give you live visibility:
   - **Dashboard** — the "now happening" banner, per-agent cards (status,
     tokens, cost, last thought), the colour-coded activity timeline,
     token/cost charts, and a files-changed list.
   - **Think** — a live, timestamped feed of every **dispatch**, **prompt**,
     and **response**. Filter by type and expand any entry to read the full
     prompt or the agent's full response inline (or pop the full inspector).
   - **Files** — browse everything the agents created. Click a file to view
     and **edit** it (Save writes back to the workspace).

**6. Steer them — follow up in chat.** On the **Files** tab, the bottom
panel is a chat with the agents. Select a file for context, type a
follow-up (e.g. *"add error handling and a test for empty input"*), pick a
workflow, and **Send**. Each message seeds a new workflow run scoped to
your request and streams the agents' work straight back into the thread —
so you can iterate on the generated code conversationally.

That's the full loop: configure → run → watch → edit/steer → repeat, all
from `http://localhost:8001`.

### What you can do from the browser

- **Workspaces** — create, switch between, and delete isolated projects
  from the left rail. The whole UI focuses on one active workspace at a
  time, just like the TUI.
- **Run** — capture an idea and launch a workflow (`run`), an iterative
  `loop` (ralph / autoresearch with an iteration count), `plan`, or just
  set the idea. A raw-output tab streams stdout while the structured
  view animates on the Dashboard.
- **Dashboard** — a live **"now happening"** banner, per-agent cards
  (status, runs, tokens, cost, last "thought", activity meter), a
  filterable real-time **activity timeline** (dispatches, prompts,
  responses, actions, retries, commits…), **token & cost charts**, and a
  files-changed list. Click any timeline event to inspect the full prompt
  and response.
- **Think** — a dedicated, live **thinking & dispatching** feed: every
  dispatch, prompt, and response as a timestamped row, filterable by type
  and expandable to the full text inline (or in the inspector).
- **Files** — browse the files the agents generated, **view and edit** them
  in-browser (Save writes back to the workspace), and **chat with the
  agents**: each follow-up message seeds a workflow run scoped to your
  request (optionally with a selected file as context) and streams the
  result back into the thread. A **History** toggle shows the commit
  timeline (agent badge parsed from the commit subject, relative time,
  +/− line stats); clicking a commit opens its changed-file list and a
  colored diff. The file editor's history button **time-travels a single
  file** to any past version (read-only, "Back to latest" banner). When
  the working tree is dirty, an **Uncommitted changes** entry tops the
  history with new/modified/deleted badges and the working-tree diff,
  and changed files carry an amber dot in the list. The file list reads
  live from disk (2 s refresh), so it always shows the latest state —
  committed or not.
- **Guided mode** — a full-screen step-by-step wizard for newcomers:
  provider discovery (Ollama/OpenWebUI probed with a docker-host
  fallback, CLI providers detected on PATH or unlocked by an API key) →
  model pick → plain-language idea → friendly progress view → files →
  follow-up loop. First visit with no workspaces lands here; the
  sidebar's sparkle button or "Advanced mode" toggles between the wizard
  and the full console mid-run without losing the workspace.
- **Configure** — tabbed settings for the global **`.env`** (grouped,
  typed widgets; secrets masked with `••••••••` and preserved on save),
  per-workspace **harness config** (`clk.config.json`), **providers**
  (pick the active one, edit endpoints/keys), and the **agent roster**.
  A health strip surfaces `doctor` findings (missing keys, unavailable
  providers) at a glance.

### How the live view works

Under the hood the dashboard streams the harness's structured event log
(`.clk/logs/activity.jsonl`) over Server-Sent Events
(`GET /api/workspaces/{id}/activity/stream`) and folds it into a snapshot
(`GET /api/workspaces/{id}/snapshot`) that mirrors the TUI's model. The
connection auto-reconnects, so you can leave the tab open across runs.

### Configuring `.env` (where settings live)

The **Configure → .env (global)** tab edits a single `.env` file shared by
all workspaces (provider, API keys, git identity, feature flags…). The API
injects it into every agent subprocess, so edits take effect on the **next
run** without restarting the server. The tab header shows exactly which
file it's editing.

CLK resolves that path as follows:

1. **`CLK_ENV_FILE`** — if set, this exact path wins (`~` is expanded).
2. Otherwise, `<package-dir>/../.env`.

In an installed image the fallback resolves next to the installed
package (e.g. `…/site-packages/.env`) — not where you'd want it. **Set
`CLK_ENV_FILE` to a path inside your bind mount** so the file lives on your
host and persists across containers:

```bash
-e CLK_ENV_FILE=/workspaces/.env  -v "$(pwd):/workspaces"
```

The file is created on first save (you don't need to pre-create it); just
make sure the parent directory exists.

### Secrets & network safety

`.env` editing includes API keys. Secret-looking values
(`*_API_KEY`, `*_TOKEN`, …) are **masked** in every response and never
echoed back; saving an unchanged masked field preserves the stored value.
A single-key reveal endpoint exists but is **disabled by default** — set
`CLK_API_ALLOW_REVEAL=1` to enable it. The server binds to loopback
(`127.0.0.1`) by default; only set `CLK_API_HOST=0.0.0.0` on a trusted,
isolated network (there is no built-in auth).

### Developing the UI

The source lives in `webui/` (Vite + React + TypeScript). For hot-reload
development against a running server:

```bash
clk web --no-open            # serve the API on :8001 in one terminal
cd webui && npm install && npm run dev   # Vite dev server on :5173 (proxies /api)
```

`npm run build` emits the bundle to `clk_harness/webui_dist/` (shipped in
the wheel via `package-data`); `npm test` runs the Vitest suite.

## REST API

CLK ships a FastAPI-based HTTP server that exposes a subset of CLI
commands programmatically — specifically: `init`, `idea`, `plan`, `run`,
`loop`, and `status` (see `/api/capabilities` for the authoritative list).
Use it to integrate CLK into your own tooling, drive it from a web UI,
or orchestrate it from CI pipelines without spawning a terminal.

### Install

```bash
pip install "clk-harness[api]"
```

### Start the server

The REST API starts **automatically in the background** whenever you run
any `clk` sub-command (provided the optional `[api]` extras are installed).
A `[clk] REST API listening on http://…` banner is printed to stderr at
startup.  You can also start it standalone:

```bash
# Using the console-script entry point (recommended)
clk-api

# Or via the module entry point
python -m clk_harness.api

# Or via uvicorn directly
uvicorn clk_harness.api:app --host 0.0.0.0 --port 8001
```

The server listens on port `8001` by default.  Override with
`CLK_API_PORT=<port>`.

### Security and network bind address

> **Warning: the REST API has no authentication and binds to `0.0.0.0`
> (all interfaces) by default.**  This default suits sandbox / container
> environments where network isolation is provided by the runtime.
> **Do not expose the API port to an untrusted network without additional
> access controls.**  For local development, restrict the server to
> loopback (`127.0.0.1`) using the mechanisms below.

When the CLI starts, the REST API auto-starts on a background daemon thread
and prints a `[clk]` banner to stderr.  Override the bind address or disable
the API entirely:

| Mechanism | Effect |
|---|---|
| `CLK_API_HOST=127.0.0.1` | Restrict the API to loopback (recommended for local dev) |
| `CLK_API_PORT=<port>` | Change the listen port (default `8001`) |
| `clk --no-api <cmd>` | Skip the background API for this invocation |
| `CLK_DISABLE_API=1` | Disable the background API for all CLI invocations |

If the optional `[api]` extras (`fastapi`, `uvicorn`) are not installed,
the background thread is silently skipped and the CLI works normally.

### Quick curl example

```bash
# Health check
curl http://localhost:8001/api/healthz

# Create a workspace
WS=$(curl -s -X POST http://localhost:8001/api/workspaces \
  -H 'Content-Type: application/json' \
  -d '{"name": "my-project"}' \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['workspace_id'])")

# Capture an idea
TASK=$(curl -s -X POST http://localhost:8001/api/research \
  -H 'Content-Type: application/json' \
  -d "{\"command\":\"idea\",\"args\":[\"A local-first journaling app\"],\"workspace_id\":\"$WS\"}" \
  | python3 -c "import sys,json; print(json.load(sys.stdin)['task_id'])")

# Stream live output
curl -sN http://localhost:8001/api/research/$TASK/stream
```

See [docs/REST_API.md](docs/REST_API.md) for the full endpoint reference,
SSE event format, and more examples.

## Docker

The harness ships with a `Dockerfile`. Kickoff directories are created under
`workspace/` inside the container; mount a volume there to keep them after
the container exits.

The default mode is the interactive TUI dashboard — run with `-it` so the
container has a terminal. If no `.env` is present it will prompt for provider
and settings before launching. Pass your idea as the first argument to skip
the prompt and go straight to the engineering workflow.

> **`install_local.sh` is not needed inside Docker.** The `Dockerfile` runs
> `pip install -e .` at image-build time, so all Python dependencies are
> already present. Keep `CLK_RUN_INSTALL=false` (the default) — setting it to
> `true` in a Docker environment would redundantly re-create a `.clk/venv`
> that the container doesn't need.

All examples below assume the image is tagged `clk` locally — either
build it from source or pull a prebuilt image and re-tag it (see the next
two sections).

### Build

```bash
docker build -t clk .
```

### Pull from GHCR

Prebuilt images are published to GitHub Container Registry on every push to
`main` (tagged `latest` and `main`), every semver tag (`vX.Y.Z` → `X.Y.Z`,
`X.Y`), and every commit (`sha-<short>`):

```bash
docker pull ghcr.io/billjr99/cognitiveloopkernel:latest
docker tag ghcr.io/billjr99/cognitiveloopkernel:latest clk
```

The `docker tag` step lets every later command in this README refer to the
image simply as `clk`. If you'd rather not re-tag, substitute
`ghcr.io/billjr99/cognitiveloopkernel:latest` for `clk` in the examples
below.

### Configuration via .env

`kickoff.sh` loads `/app/.env` at startup, so any setting that can be
configured via `CLK_*` env vars (provider, API keys, git identity, etc.)
can also live in a single file. There are two ways to provide it:

**Bind-mount a host file at `/app/.env`** — recommended when you want the
setup wizard's edits to persist back to disk:

```bash
touch ~/clk.env                  # create empty file first (Docker quirk)
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk "My idea here"
```

**Pass it via `--env-file`** — simpler when the file is read-only config:

```bash
docker run --rm -it \
  --env-file ~/clk.env \
  -v clk-workspace:/app/workspace \
  clk "My idea here"
```

The bind-mount approach is required if you want to use `--setup` (the wizard
writes back into `/app/.env`); `--env-file` only injects vars at start.

### First-run setup

Run the setup wizard to create your `.env`. The wizard is structured
as a series of **explain-then-ask** blocks — each section tells you
what the value does before asking for it, modeled on the
`scripts/install_local.sh` narration style. Sections (in order):

1. **Provider** — pick the AI that writes code (`shell`, `claude`,
   `codex`, `gemini`, `pi`, `ollama`, `openwebui`). One-liner per
   choice.
2. **Loop settings** — max iterations, project name, install flag,
   TUI/no-TUI. The **install flag** (`CLK_RUN_INSTALL`) controls
   whether `scripts/install_local.sh` runs inside each kickoff
   directory to create a local `.clk/venv`. **Leave it `false`
   (the default) when running in Docker** — the image already has
   all Python dependencies installed at build time, so the local
   venv step is unnecessary.
3. **Auth mode** — only for CLI providers; `cli` reuses your local
   `claude login` / `codex login` / `gemini login`, `apikey`
   prompts for a key directly.
4. **Tool detection + auto-install** — checks whether the chosen
   provider's CLI is on PATH; if not, surfaces the canonical install
   command and asks before running it. Backed by
   `scripts/install_tool.sh`'s registry — same commands the TUI's
   `/install` uses.
5. **First-use configure** — auth → upstream route → model →
   verify. Pi picks `openrouter` / `anthropic` / `openai` / `google`
   and sets the right `{ROUTE}_API_KEY` env var. Ollama runs
   `ollama list`, lets you pick a local model or pull a new one
   (progress streamed). State recorded in
   `.clk/state/configured-tools.json` so the wizard knows not to
   re-prompt next time.
6. **Telegram** — same flow as before. Says yes here triggers the
   dedicated bot wizard at `scripts/telegram_setup_wizard.sh`.
7. **GitHub** — optional remote (skip / existing / create); writes a
   hardened `.gitignore` and a pre-push secret scan hook. See
   [GitHub integration](#github-integration).
8. **Git identity** — `CLK_GIT_NAME` / `CLK_GIT_EMAIL` for the
   in-container fallback.

**Atomic writes.** Every answer is persisted to `.env` immediately
via `env_set` (sourced from `scripts/lib_env.sh`). The previous
content rotates to `.env.bak`. If the wizard crashes mid-flow, the
next run looks at `.clk/.setup-progress` and offers to resume from
the last completed step. To undo a bad wizard run entirely, run
`./kickoff.sh --restore`.

**Always-confirm.** Every install, push, ollama pull, and
destructive step asks `[y/N]` every single time. Pressing Enter
defaults to the safe option.

```bash
# Create an empty config file on the host (once)
touch ~/clk.env

# Run the wizard — writes into the bind-mounted file
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk --setup
```

`--setup` also works locally (outside Docker) and updates `./kickoff.sh`'s
own `.env` in-place.

### Run (interactive TUI — default)

**Named volume** — kickoffs persist in a Docker-managed volume across runs:

```bash
docker volume create clk-workspace

docker run --rm -it \
  -v clk-workspace:/app/workspace \
  clk "A local-first journaling app that summarizes my week"
```

**Host directory** — kickoffs written directly to a directory on your machine:

```bash
docker run --rm -it \
  -v /path/to/my/projects:/app/workspace \
  clk "A local-first journaling app that summarizes my week"
```

**Anonymous volume** — Docker allocates a temporary volume that is
automatically removed when the container exits (`--rm` handles cleanup):

```bash
docker run --rm -it \
  -v /app/workspace \
  clk "A local-first journaling app that summarizes my week"
```

**Ephemeral** — no explicit volume mount; Docker creates an anonymous volume
for `/app/workspace` (declared in the image) and removes it with `--rm`:

```bash
docker run --rm -it clk "A local-first journaling app that summarizes my week"
```

### Provider and authentication

Pass any `CLK_*` variable or API key with `-e`:

```bash
docker run --rm -it \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=claude \
  -e CLK_AUTH_MODE=apikey \
  -e ANTHROPIC_API_KEY=sk-ant-... \
  clk "A local-first journaling app that summarizes my week"
```

For the `pi` provider with an OpenRouter key:

```bash
docker run --rm -it \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=pi \
  -e CLK_PI_MODEL=openrouter/free \
  -e CLK_PI_KEY_TYPE=openrouter \
  -e CLK_PI_API_KEY=sk-or-... \
  clk "A local-first journaling app that summarizes my week"
```

For `ollama` or `openwebui` running on the host, use `host.docker.internal`
as the endpoint (macOS/Windows) or `--network host` (Linux):

```bash
docker run --rm -it \
  -v clk-workspace:/app/workspace \
  -e CLK_PROVIDER=ollama \
  -e CLK_OLLAMA_ENDPOINT=http://host.docker.internal:11434 \
  clk "My idea"
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
  clk "A local-first journaling app that summarizes my week"
```

### Run the REST API

To run the [REST API](#rest-api) server inside the container instead of the
TUI, override the entrypoint command:

```bash
docker run --rm -p 8001:8001 \
  -v clk-workspaces:/workspaces \
  clk python -m clk_harness.api
```

Mount `/workspaces` to persist workspace *directories* across container
restarts.

> **Note: workspace state is in-memory and is NOT recoverable after restart.**
> Even when the `/workspaces` volume is mounted, the in-memory registry of
> workspace IDs and task history is lost every time the container restarts.
> The files inside `/workspaces` survive on disk, but you must create new
> workspace registrations via `POST /api/workspaces` after each restart —
> previous workspace IDs and task IDs will not be recognised by the new
> container instance.

Override the workspace root with `CLK_WORKSPACES_DIR`.

## Telegram Bot

Two-way chat control for CLK. The bot lets you kick off runs, watch live
status updates, tail the activity log, and cancel tasks from anywhere
Telegram works — no SSH, no port forwarding, no public URL. It connects
via long polling, so it works behind NAT (your home network, a Pi behind
a router, a Docker container).

### How it works

`clk-telegram-bot` is a separate process that:

1. Long-polls Telegram's servers for messages from allowlisted users.
2. Translates commands into calls against the local CLK REST API
   (`clk-api`, default `http://127.0.0.1:8001`).
3. Tails `.clk/logs/activity.jsonl` and pushes interesting events (agent
   dispatches, action applied, iteration outcomes, errors) to subscribed
   chats in real time.

Access is gated by a numeric-user-ID allowlist. Unknown users get a single
canned reply that prints their own user ID (so the operator can add them)
and are otherwise ignored.

### One-time setup (any platform)

Three steps. The wizard automates the last two:

1. **Create the bot** with [@BotFather](https://t.me/BotFather):
   - Open Telegram, message `@BotFather`.
   - Send `/newbot`. Pick a display name and a unique username that ends
     in `bot` (e.g. `my_clk_bot`).
   - BotFather replies with an HTTP API token like
     `123456789:AAH...xyz`. Copy it.
2. **Run the wizard**:
   ```bash
   ./scripts/telegram_setup_wizard.sh
   ```
   The wizard:
   - Validates the token by calling `getMe` against Telegram.
   - Prints "Send any message to your new bot, then press Enter".
   - Reads `getUpdates` to capture your numeric user ID automatically
     (you can also enter one manually).
   - Writes `CLK_TELEGRAM_BOT_TOKEN`, `CLK_TELEGRAM_ALLOWED_USERS`, and
     `CLK_TELEGRAM_ENABLED=true` to `.env` (preserving other keys).
3. **Start the bot**:
   ```bash
   # Make sure the REST API is running first (so the bot has something to drive):
   clk-api &
   # Then start the bot:
   clk-telegram-bot
   ```

The wizard is idempotent: re-run any time to rotate the token, add more
allowed users, or re-discover your ID after switching accounts.

**You should now see:** in your Telegram chat with the new bot, sending
`/start` replies with your user ID and the help text. Sending `/status`
lists workspaces.

### Setup inside Docker

`kickoff.sh` offers Telegram setup automatically the first time it runs
without a token configured. The image already includes
`python-telegram-bot`, the wizard script, and the `clk-telegram-bot`
entry point.

```bash
# 1. Create an empty config file on the host (once).
touch ~/clk.env

# 2. Run kickoff with --setup; answer "y" at the Telegram prompt.
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  -v clk-workspace:/app/workspace \
  clk --setup
```

To run only the Telegram wizard (no kickoff prompts):

```bash
docker run --rm -it \
  -v ~/clk.env:/app/.env \
  --entrypoint scripts/telegram_setup_wizard.sh \
  clk
```

Once `~/clk.env` has the Telegram keys, run the bot in its own container
alongside `clk-api`:

```bash
# REST API server (port 8001 published so the bot container can reach it)
docker run -d --name clk-api \
  -v ~/clk.env:/app/.env \
  -v clk-workspaces:/workspaces \
  -p 127.0.0.1:8001:8001 \
  --entrypoint python clk -m clk_harness.api

# Telegram bot — talks to clk-api via Docker's bridge network
docker run -d --name clk-telegram-bot \
  --link clk-api \
  -v ~/clk.env:/app/.env \
  -v clk-workspaces:/workspaces \
  -e CLK_API_HOST=clk-api \
  -e CLK_API_PORT=8001 \
  --entrypoint clk-telegram-bot clk
```

The bot makes **outbound** HTTPS calls to `api.telegram.org`, so no
inbound port forwarding is needed. The default Docker bridge network is
enough.

### Setup on Raspberry Pi (systemd)

Install CLK via the [Pi extension](#pi-extension) or `pip install
'clk-harness[api,telegram]'`, then drop two systemd units:

```ini
# /etc/systemd/system/clk-api.service
[Unit]
Description=CLK REST API
After=network-online.target

[Service]
User=pi
WorkingDirectory=/home/pi/clk
EnvironmentFile=/home/pi/clk/.env
ExecStart=/usr/local/bin/clk-api
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

```ini
# /etc/systemd/system/clk-telegram-bot.service
[Unit]
Description=CLK Telegram bot
After=clk-api.service
Requires=clk-api.service

[Service]
User=pi
WorkingDirectory=/home/pi/clk
EnvironmentFile=/home/pi/clk/.env
ExecStart=/usr/local/bin/clk-telegram-bot
Restart=on-failure

[Install]
WantedBy=multi-user.target
```

Enable both: `sudo systemctl enable --now clk-api clk-telegram-bot`.

**You should now see:** from your phone, `/status` returns the current
workspace list. Sending `/run improve the README` kicks off a CLK run and
the bot replies with a task ID.

### Commands

| Command | Effect |
|---------|--------|
| `/start` | Greet, show your user ID, indicate whether allowlisted |
| `/help` | Show this command list |
| `/status` | List workspaces and last task ID |
| `/run <objective>` | Start a single CLK run with the given objective |
| `/loop [args]` | Start the Ralph / autoresearch loop |
| `/plan <topic>` | Run the planning workflow |
| `/idea <text>` | Capture an idea |
| `/cancel [task_id]` | Cancel a running task (latest if omitted) |
| `/tail [N]` | Print the last N lines of `activity.jsonl` (default 20) |
| `/subscribe` | Receive live event pushes in this chat |
| `/unsubscribe` | Stop receiving live event pushes |
| `/workspace <id>` | Set the default workspace for this chat |

Any plain text (no slash) from an allowlisted user is treated as
`/run <text>` — so you can just describe what you want.

### Adding more allowed users

Either re-run `scripts/telegram_setup_wizard.sh` (it appends new IDs to
the existing list) or edit `CLK_TELEGRAM_ALLOWED_USERS` in `.env`
directly:

```bash
# .env
CLK_TELEGRAM_ALLOWED_USERS=123456789,987654321,555666777
```

Restart `clk-telegram-bot` to pick up the change.

### Troubleshooting

- **Bot doesn't reply.** Send `/start` and check the reply for your user
  ID. If you get the "Not allowlisted" message, add the ID to
  `CLK_TELEGRAM_ALLOWED_USERS` and restart the bot.
- **`token rejected by Telegram`** (during the wizard). The token is
  wrong or was revoked. Get a fresh one from BotFather with `/token`.
- **No live updates** even after `/subscribe`. Confirm that the bot can
  read the activity log: `CLK_TELEGRAM_ACTIVITY_LOG` overrides the
  default path, or the bot auto-detects
  `$CLK_WORKSPACES_DIR/<workspace>/.clk/logs/activity.jsonl`.
- **`clk-telegram-bot --check-config` exits non-zero.** It prints which
  variable is missing (`2` = token, `3` = empty allowlist).
- **Kickoff prompts every run.** Set `CLK_TELEGRAM_SKIP=true` in `.env`
  to permanently suppress the "Set up Telegram bot now?" prompt.

## Recoverability

CLK tries hard to never leave you with a broken setup or a stuck
session. The safety nets:

| Safety net | When it kicks in | How to use it |
|---|---|---|
| `.env.bak` rotation | Every wizard run rotates the old `.env` to `.env.bak` before writing. | `./kickoff.sh --restore` swaps it back. |
| Atomic `.env` writes | Wizards write to `.env.tmp` and rename — Ctrl-C mid-write leaves either the old or the new file intact, never half. | Automatic; no user action. |
| Atomic JSON config writes | Same pattern for `.clk/config/*.json` and any agent-written JSON, with `.bak` rotation. | Implemented in `clk_harness.config.save_json`. |
| Per-step wizard resume | Wizard tracks last completed step in `.clk/.setup-progress`. If you Ctrl-C, the next run offers to resume. | `./kickoff.sh --setup` prompts "Resume from after step X? [Y/n]". |
| Crashed-session detection | The TUI writes its PID to `.clk/state/.tui-active`. If a previous TUI exited uncleanly, the next launch surfaces "recovered from a crashed session" and points to the preserved `.clk/state/conversation.md`. | Automatic. |
| `/undo` | After every agent commit, `/undo` lets you preview and revert the last commit. Two-step (preview first, then `/undo confirm`) so it's never accidental. | Type `/undo` in the TUI. |
| `/abort` | When an agent subprocess is stuck, SIGTERM it without killing the TUI. The provider returns a timeout error, the cycle reports the failure cleanly. | Type `/abort` in the TUI. |
| `/install` / `/configure` | Recover from "CLI not found" / "auth failed" without leaving the dashboard. | `/install [provider]` then `/configure [provider]`. |
| Pre-push secret scanner | Installed in the kickoff dir's `.git/hooks/pre-push`. Greps for `ANTHROPIC_API_KEY=`, `OPENAI_API_KEY=`, `sk-…`, private-key headers. Bypass with `git push --no-verify` when sure. | Automatic in every kickoff dir. |

**Confirmation policy.** Every install, push, undo, cost-cap
crossing, ollama pull, and destructive `--clean` action asks `[y/N]`
every single time. There is no "remember my answer" shortcut — by
design.

## GitHub integration

`kickoff.sh --setup` offers to wire each kickoff workspace up to a
GitHub remote so every CLK commit is checkpointed off your machine.

**Three modes:**

- `skip` — no GitHub, local commits only (default).
- `existing` — paste a `https://github.com/OWNER/REPO` or
  `git@github.com:OWNER/REPO.git` URL; the wizard validates it via
  `gh repo view` (or `git ls-remote` if `gh` isn't on PATH).
- `create` — provide `owner/repo` (default
  `$USER/$CLK_PROJECT_NAME-kickoff`), the wizard runs
  `gh repo create … --private` from inside the kickoff dir. Default
  visibility is private — making it public requires an explicit
  choice.

**Auth.** Prefer the `gh` CLI if it's on PATH and authenticated. If
not, the wizard offers to install `gh` and drops you into a shell
for `gh auth login` (same pattern as `pi login`). PATs are stashed
in `~/.config/clk/github-token` (chmod 600), never `.env`.

**Hardened `.gitignore`.** Written before the first push so secrets
can't leak. Blocks `.env`, `.env.bak`, `.env.local`, `*.pem`,
`*.key`, `*_id_rsa*`, `/secrets/`, plus editor / OS junk.

**Pre-push hook.** `.git/hooks/pre-push` greps the about-to-push
objects for obvious key patterns (Anthropic / OpenAI / OpenRouter /
Gemini / Google keys, generic `sk-…` strings, Slack `xoxb-` tokens,
private key headers). On a hit the push aborts with the offending
lines and the bypass instructions. Bypass once with `git push
--no-verify`.

**`CLK_GITHUB_PUSH_ON_COMMIT=true`** makes the harness follow every
auto-commit with a `git push origin HEAD`. Failures are non-fatal —
the commit stays local until the network or remote is back. The TUI
title bar shows `↑N` for the count of unpushed commits.

**Re-link from the TUI.** Type `/github` to see current remotes and
re-link instructions.

## Diagnostics & Doctor

Two new commands help when something feels off.

### `/doctor` (or `clk doctor`)

Health-check every provider, validate `.env` against known-bad
combos, and check git/GitHub state.

- Reports each finding as `ok | warn | fail`.
- Exits non-zero on any `fail` so it slots into CI.
- `/doctor --fix` prompts before each automated remedy (running
  `/install`, re-running `configure_tool`, writing a missing key).

Common findings:

| Finding | Meaning | Fix |
|---|---|---|
| `claude: unavailable` | `claude` CLI not on PATH or API key missing | `/install claude` then `/configure claude` |
| `anthropic_key: fail` | `CLK_AUTH_MODE=apikey` but `ANTHROPIC_API_KEY` is empty | `/configure claude` to set it |
| `git: warn` | no git repo at project root; auto-commit disabled | `git init` |
| `ollama: unavailable` | endpoint not reachable | `/install ollama`, then `ollama serve &` |

### `/diag` (or `clk diag`)

Bundles the current state into a `clk-diag-<ts>.tar.gz` for sharing
in bug reports. Contents:

- `.clk/logs/*` (recent only — capped so the bundle stays small)
- `.clk/runs/<last-3>/`
- `.clk/state/*.{md,json}`
- `clk doctor` output
- `pyproject.toml` version, `python --version`, `git --version`,
  `uname -a`
- A redacted copy of `.env` — every value under a key containing
  `KEY`, `TOKEN`, `SECRET`, or `PASS` is replaced with
  `<redacted: N chars>` so the recipient can confirm you *had* a
  key without seeing it.

Always confirms before writing the tarball.

## Tutorial mode

First-time users can type `/tutorial` in the TUI to run a
30-second sample idea — `"Add a hello() function to greeter.py"` —
against the `shell` provider. Costs nothing, takes no API keys,
demonstrates the cast → engineer → qa → commit loop end-to-end so
the user knows what a "real" run will look like.

The tutorial backs up your active provider, runs one engineering
cycle in `.clk/state/.tutorial/`, then restores. A marker at
`.clk/state/.seen-tutorial` suppresses the "type /tutorial" hint
in the welcome banner on subsequent runs.

## Workspace management

Each `kickoff.sh` creates `workspace/kickoff-<timestamp>/`. To keep
the directory navigable:

```bash
./kickoff.sh --list                # show every kickoff with its idea
./kickoff.sh --clean 7d            # delete kickoff dirs older than 7 days (after y/N)
./kickoff.sh --clean 30m           # same, in minutes
./kickoff.sh --restore             # roll .env back to .env.bak (undo last wizard run)
```

From inside the TUI:

```text
/workspaces list                   # numbered list, * marks the current one
/workspaces rename old-name new    # rename a kickoff dir
/workspaces switch <name>          # prints instructions (/quit, then cd)
/workspaces clean                  # points at ./kickoff.sh --clean
```

The kickoff manifest at `KICKOFF.md` (written by `kickoff.sh` into
each new workspace) records timestamp, source dir, project name,
provider, max iterations, install flag, and idea.

## Cost guardrails

Title-bar dollar cost is computed from the per-provider table in
`clk_harness/pricing.py`:

| Provider | Default $/1k in | Default $/1k out |
|---|---|---|
| claude (sonnet-4-5)  | $0.003   | $0.015  |
| claude (haiku-latest)| $0.0008  | $0.004  |
| claude (opus-latest) | $0.015   | $0.075  |
| codex (gpt-4o)       | $0.0025  | $0.010  |
| codex (gpt-4o-mini)  | $0.00015 | $0.0006 |
| codex (o1)           | $0.015   | $0.060  |
| gemini (1.5-pro)     | $0.00125 | $0.005  |
| gemini (1.5-flash)   | $0.000075| $0.0003 |
| pi                   | $0.003   | $0.015  (blended default; override per route) |
| ollama / shell       | $0.00    | $0.00   |

**Override per project** by adding to `.clk/config/providers.json`:

```jsonc
"providers": {
  "pi": {
    "type": "pi",
    "pricing": { "input_per_1k": 0.002, "output_per_1k": 0.008 }
  }
}
```

Or per model:

```jsonc
"pricing_by_model": { "openrouter/free": { "input_per_1k": 0.0, "output_per_1k": 0.0 } }
```

`/status` prints the per-provider breakdown so you can see which
provider is eating the budget. Updated lazily from the same numbers
the title bar shows.

### Robustness-loop multipliers

The robustness loops (see **Robustness loops**) trade tokens for
quality. Use this table to pick a regime:

| Knob                                   | Worst-case multiplier per affected dispatch                              | Recommended starting point |
|----------------------------------------|--------------------------------------------------------------------------|----------------------------|
| `robustness.auto_consensus`            | `off` → ×1; `on_careful` → ×(N+1) on careful stages only; `always` → ×(N+1) on every dispatch (where N = `consensus.max_samples`, default 6) | `on_careful` (default)     |
| `robustness.auto_refine`               | `off` → ×1; `careful_only` → ×(1 + 1 worker revision + 1 critic) on careful stages; `all` → that on every producing stage | `all` (default — every producing stage refines) |
| `robustness.max_quality_retries`       | At most this many extra dispatches when a response fails the quality check; 0 disables | 4 (default)                |
| `robustness.refine_max_rounds`         | Cap on critic↔worker round-trips inside a refine loop                    | 10 (default)               |
| `robustness.debate`                    | `off` → ×1; `careful_only` → an adversarial panel (one critic per lens) on careful stages; `all` → on every producing stage | `careful_only` (default)   |
| `robustness.debate_lenses`             | Adversarial lenses (one parallel critic each) — more lenses = more critic dispatches per round | `[correctness, security, simplicity]` |
| `robustness.debate_max_rounds`         | Cap on debate rounds (panel critique + worker revision) per stage        | 2 (default)                |
| `robustness.max_qa_depth`              | Cap on inter-agent Q&A chain depth (each peer answer can ask one peer)   | 3 (default)                |
| `robustness.plateau_window`            | How many no-improvement Ralph/autoresearch iterations before escalation  | 3 (default)                |
| `robustness.plateau_action`            | `off` disables adaptive loop termination entirely                        | `escalate_then_reframe`    |

Cost-minimal regime (closest to legacy CLK behavior, no extra tokens):

```jsonc
"robustness": {
  "auto_consensus": "off",
  "auto_refine": "off",
  "max_quality_retries": 0,
  "plateau_action": "off"
}
```

Cost-maximal "lean into the loop" regime (every dispatch fans out,
critic gates every careful stage, plateau detection on, Q&A protocol
fully open):

```jsonc
"robustness": {
  "auto_consensus": "always",
  "auto_refine": "all",
  "max_quality_retries": 3,
  "refine_max_rounds": 4,
  "plateau_action": "escalate_then_reframe"
}
```

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

## Layout

The package itself:

```
clk_harness/
  api.py                 # FastAPI REST API server
  _api_launcher.py       # background daemon thread launcher (auto-start on CLI)
  _api_shim.py           # console-script shim for clk-api (guards ImportError)
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
  run_all_tests.sh       # orchestrator: build + test in ephemeral Docker
tests/                   # pytest regression suite (CI-gated)
user_tests/              # pytest end-to-end suite (drives CLI + REST API)
pi-extension/            # standalone Pi extension (TypeScript)
  src/
    index.ts             # /clk + /clk-resume + /clk-help + /clk-doctor + /clk-undo,
                         #   session lifecycle + watchdog wiring
    prompts.ts           # the chief's operator's manual
    tools.ts             # clk_cast / clk_progress / clk_checkpoint / clk_branch /
                         #   clk_merge / clk_revert / clk_consensus / clk_subagent_quality /
                         #   clk_autoresearch / clk_ralph / clk_done
    watchdog.ts          # supervise loop: continue → stall rescue → stop ladder
    validate.ts          # shell validation gate for clk_merge / clk_done
    subagent.ts          # raw clk_subagent — spawnSubagent() exposed for consensus
    consensus.ts         # dispatchWithQuality + runConsensus (port of agent.py)
    quality.ts           # scoreResponse + repairHint + progressSignal
                         #   (port of response_quality.py)
    git.ts               # checkpoint, branch, merge, revert + hasRemote / commitsAhead /
                         #   pushBestEffort (port of git_ops.py auto-push helpers)
    state.ts / abort.ts / errors.ts / types.ts
  tests/                 # node --test suites covering every file in src/
docs/
  REST_API.md            # full REST API reference
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

## Layout

The kickoff dir lays the agents' work out as a normal project tree
with all harness machinery folded under `.clk/`:

```
workspace/kickoff-<ts>/
  src/, tests/, README.md ...   # the project the agents are building
                                # (agents write directly to project root)
  scripts/clk                   # convenience launcher shim
  KICKOFF.md                    # provenance manifest
  .gitignore                    # hardened — blocks .env, .env.bak, *.pem, …
  .git/hooks/pre-push           # secret scanner; aborts on key patterns
  .clk/                         # ALL harness state — sandboxed off
    .setup-progress             # per-step resume marker for the wizard
    harness/clk_harness/        # harness sources copied from parent
    harness/scripts/            # original launcher / installer
    harness/pyproject.toml      # package metadata for pip install -e
    config/                     # clk.config.json, providers.json, agents.json
                                # each written atomically with a .bak rotation
    state/                      # idea.json, prd.json, decisions.md ...
                                # plus:
                                #   .seen-welcome         first-run banner marker
                                #   .seen-tutorial        /tutorial done marker
                                #   .tui-active           PID lock (crashed-session detection)
                                #   configured-tools.json which tools have had configure_tool run
                                #   session-cost.json     persisted USD totals
    prompts/                    # per-agent system prompts
    blackboard/                 # cross-agent shared scratchpad (POST blocks land here)
    runs/                       # per-dispatch prompt + response logs
    backups/                    # pre-write copies of mutated files
    cache/, logs/, venv/        # local-only artifacts
```

The repo root also adds:

- `scripts/lib_env.sh` — shared atomic-write helpers (`env_set`,
  `env_get`, `env_atomic_write`, `env_restore`) sourced by both
  wizards.
- `scripts/install_tool.sh` — install + check + configure registry
  for every supported tool. Used by `kickoff.sh --setup` and by the
  TUI's `/install` / `/configure` commands.
- `clk_harness/pricing.py` — per-provider USD pricing table backing
  the title-bar cost estimate.
- `~/.config/clk/github-token` — when present (chmod 600), used in
  place of the `gh` CLI for GitHub operations.

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

### Putting it together

A typical "careful" engineering stage now runs:

1. Stage dispatched with `careful: true`.
2. `auto_consensus=on_careful` → N samples fan out in parallel.
3. Chief coalesces the samples.
4. `auto_refine=all` (default) → critic scores the coalesced output;
   the worker is revised until critic accepts or `max_rounds`.
5. Stage validation runs.
6. Checkpoint (if enabled) — chief CONTINUE / REDIRECT / ABORT
   verdict.
7. Outputs contract check; warn if any declared key was not posted.

Tracing this in `.clk/logs/`:

```
grep -E '^(consensus_|refine_|workflow_checkpoint|agent_quality_)' \
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

## Testing

CLK ships three test suites and a one-command orchestrator that runs them
all in an ephemeral Docker container.

| Suite                  | What it covers                                          | Runner |
|------------------------|---------------------------------------------------------|--------|
| `tests/`               | Unit + integration regression tests (CI-gated)          | pytest |
| `user_tests/`          | End-to-end CLI / REST API / `kickoff.sh` user tests     | pytest |
| `pi-extension/tests/`  | TypeScript Node tests for the Pi extension              | npm    |

### One-command run

```bash
# Interactive: prompts for LLM provider, API key, base URL, model.
# Builds an ephemeral Docker image, runs every suite inside, then tears
# the container down (success or failure).
./scripts/run_all_tests.sh

# CI / scripted use — skip the prompts and use the shell provider:
./scripts/run_all_tests.sh --non-interactive

# Single suite (no Docker, runs directly on the host):
./scripts/run_all_tests.sh --local --suite=user
./scripts/run_all_tests.sh --local --suite=ci
./scripts/run_all_tests.sh --local --suite=pi
```

The interactive menu asks four questions:

1. **LLM provider** (shell / claude / codex / gemini / pi / ollama / openwebui)
2. **Auth mode** (cli vs apikey) for the CLI-driven providers
3. **API key**, base URL, model name — only for the chosen provider
4. **Confirm + go**

All deterministic tests (CLI plumbing, REST API contract, etc.) run
against the `shell` provider regardless — they need no credentials and
always succeed.  The opt-in *real-provider smoke* test
(`test_kickoff_with_user_selected_provider` in `user_tests/`) runs
kickoff.sh end-to-end with whatever provider you selected, and the
`pi-extension` runtime smoke verifies the `pi` CLI is reachable when you
chose `pi` and gave it a model + key.

### What runs inside the Docker container

`run_all_tests.sh` (Docker mode):

1. Builds `clk:tests-<pid>` from the project `Dockerfile`.
2. Mounts the repo read-only at `/repo`, copies it into a writable
   `/work` inside the container.
3. Runs `pytest tests/` then `pytest user_tests/` then
   `npm test` inside `pi-extension/`.
4. **Always tears down** the container on exit (success, failure, or
   ^C) and removes the ephemeral image, unless `--keep` is passed.

Useful flags:

| Flag                | Effect |
|---------------------|--------|
| `--local`           | Run on the host directly; no Docker daemon required. |
| `--non-interactive` | Skip all prompts; force `CLK_PROVIDER=shell`. |
| `--suite=all`       | Default — run all three test directories. |
| `--suite=ci`        | Only `tests/` (regression). |
| `--suite=user`      | Only `user_tests/`. |
| `--suite=pi`        | Only `pi-extension/tests/`. |
| `--keep`            | Don't remove the container or image on exit. |
| `--no-build`        | Reuse a pre-built `clk:tests-latest` image. |
| `-k <expr>`         | Forward a `-k` filter to pytest. |
| `-- <args>`         | Pass remaining args verbatim to pytest. |

### Running suites manually

Each suite is just pytest / npm and can be invoked on its own:

```bash
# Regression suite (existing CI tests)
pip install -e ".[api,dev]" pytest pytest-asyncio httpx
pytest tests/ -v

# User-perspective end-to-end suite (CLI subprocess + live REST API +
# real kickoff.sh runs). Uses the shell provider — no API keys needed.
pytest user_tests/ -v

# Pi extension TypeScript suite
cd pi-extension
npm install
npm test                # unit + integration tests (96 tests, ~2s)
npm run test:strict     # also runs `tsc --noEmit`
```

The `user_tests/` suite verifies, from a real user's vantage point:

- Every `clk` sub-command (`init`, `idea`, `cast`, `roles`,
  `plan`, `run`, `loop`, `status`, `providers`, `configure`) exits
  cleanly and writes the documented `.clk/` artefacts.
- All seven shipped providers register and the `shell` provider is
  always available.
- The REST API serves health, capabilities, workflows, workspace CRUD,
  research task creation, SSE streaming, artifact listing, path
  traversal blocking, and cancellation.
- `kickoff.sh` produces a self-contained workspace dir with its own git
  repo, and respects `--provider` / `CLK_PROVIDER` overrides.
- Filesystem invariants (commit history, `.clk/runs/shell-stubs/`,
  per-command `.clk/logs/<cmd>-<ts>.log`, etc.).

The `pi-extension/tests/` suite verifies:

- `classifyError`, `withRetry`, `looksRedacted`, `isMaxTurnsResult`,
  and all `recoveryHint` branches.
- `clkChiefPrimer` renders the captured idea + every CLK tool name
  (`clk_cast`, `clk_subagent`, `clk_subagent_quality`, `clk_consensus`,
  `clk_autoresearch`, `clk_ralph`, `clk_checkpoint`, `clk_done`).
- `scoreResponse` flags every documented failure mode (empty / refusal /
  malformed ACTION / malformed POST / missing outputs / low confidence /
  needs-review / missing-confidence) and `repairHint` quotes each reason
  to the worker.
- `runConsensus` fans out N samples, scores them, picks the winner, caps
  to `maxParallel`, and captures spawn errors without throwing.
  `dispatchWithQuality` retries with a repair preamble on recoverable
  failures and stops on refusal or `maxRetries`.
- `setIdea`, `setRoster`, `appendProgress`, `markDone`, `isDone`
  round-trip state through `.clk/state/*.json` and `progress.md`.
- The `git` wrapper does init, checkpoint, branch, merge, revert,
  `hasRemote`, `commitsAhead`, and `pushBestEffort` correctly against a
  real `git` binary (including the bare-upstream sync, the unreachable-
  remote failure path, and the no-remote no-op).
- The extension's `default` export registers every documented tool
  (`clk_cast`, `clk_progress`, `clk_checkpoint`, `clk_revert`,
  `clk_branch`, `clk_merge`, `clk_done`, `clk_consensus`,
  `clk_subagent_quality`, `clk_autoresearch`, `clk_ralph`,
  `clk_subagent`) and the `/clk` slash command, and handles an
  empty-idea invocation cleanly.
- `firstLineShort` returns single-line, capped output so a multi-line
  idea never bleeds line 2 into the Pi status bar.

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
