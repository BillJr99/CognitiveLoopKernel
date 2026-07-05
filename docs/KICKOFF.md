# Docker & kickoff

Part of the [CLK documentation](../README.md). Running CLK from a container or a bare clone via `kickoff.sh`, and the workspace layout it creates.

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
   [GitHub integration](TUI.md#github-integration).
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

To run the [REST API](REST_API.md#rest-api) server inside the container instead of the
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
