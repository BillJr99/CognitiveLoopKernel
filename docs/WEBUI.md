# Web dashboard

Part of the [CLK documentation](../README.md). The browser dashboard: launch, tour, `.env` configuration, and UI development.

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
