# Quick start

Part of the [CLK documentation](../README.md). Getting CLK running locally, and the lower-level CLI.

## Pick your path

Skim this matrix to jump straight to the right tutorial. Every path
goes through the same `kickoff.sh --setup` wizard at some point, so once
you've configured CLK in one place you can mix and match the rest.

| Platform / mode                      | Tutorial                                                  |
|--------------------------------------|-----------------------------------------------------------|
| Local Linux / macOS / WSL (Python)   | [Quick start](#quick-start) → [Lower-level CLI](#lower-level-cli) |
| Browser dashboard (point & click)    | [Web dashboard](WEBUI.md#web-dashboard)                          |
| Docker container (build locally)     | [Docker](KICKOFF.md#docker) → [First-run setup](KICKOFF.md#first-run-setup)   |
| Pre-built image from GHCR            | [Docker → Pull from GHCR](KICKOFF.md#pull-from-ghcr)                |
| Raspberry Pi (`pi` runtime)          | [Pi extension](PROVIDERS.md#pi-extension)                             |
| REST API (drive CLK from code)       | [REST API](REST_API.md#rest-api)                                     |
| Chat-control from your phone         | [Telegram Bot](TELEGRAM.md#telegram-bot)                             |

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
> [Telegram Bot](TELEGRAM.md#telegram-bot) section to drive CLK from your phone with
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
| `/gauntlet on\|off\|quick\|standard\|rigorous` | toggle the gauntlet loop or set its intensity, live (no argument prints the current state) |
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
