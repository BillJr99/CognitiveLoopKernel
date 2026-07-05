# TUI guide

Part of the [CLK documentation](../README.md). Living with the terminal UI: recoverability, GitHub integration, diagnostics, the tutorial, and workspace management.

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
