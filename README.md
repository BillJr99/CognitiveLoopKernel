# Cognitive Loop Kernel (CLK)

Local-only multi-agent development harness. Drop `clk` into an empty
directory, capture an idea, and let a team of agents iterate the idea
into a working system through repeated agentic development cycles.

## Why CLK

- **Local-first.** Everything lives under `.clk/` in the project
  directory. No global installs, no `sudo`.
- **Provider-agnostic.** Works with Claude Code, OpenAI Codex, Pi, local
  Ollama, or a built-in dummy "shell" provider for testing.
- **Iterative by design.** Ships with Archon-style YAML workflows, a
  Ralph/gnhf-style improvement loop, and a Karpathy-style autoresearch
  loop.
- **Memory through git.** Every successful milestone is committed with a
  structured message so future agent runs can mine the log for context.

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

| TUI command            | Effect |
|------------------------|--------|
| free text              | first message becomes the idea, then runs `engineering`; later messages append to the conversation and re-run |
| `/idea <text>`         | replace the captured idea |
| `/run [workflow]`      | run a single workflow cycle (default `engineering`) |
| `/loop ralph 5`        | start a Ralph loop with 5 iterations |
| `/loop autoresearch 3` | start an autoresearch loop |
| `/stop`                | request the active loop to stop after the current iteration |
| `/provider <name>`     | switch the active provider (shell, claude, codex, pi, ollama) |
| `/status`              | log a status snapshot |
| `/quit`                | exit the TUI |

PgUp/PgDn scroll the log pane; Backspace edits the input; Enter sends.

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

## Layout

The package itself:

```
clk_harness/
  cli.py                 # argparse entrypoint
  config.py              # paths, default configs, JSON load/save
  git_ops.py             # init, commit, revert, status helpers
  providers/             # claude, codex, pi, ollama, shell adapters
  orchestration/         # agent runner, workflow runner, ralph + autoresearch loops
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
    clk.config.json      # project-wide config
    providers.json       # provider registry + active provider
    agents.json          # agent -> prompt + provider mapping
    workflows/*.yaml     # Archon-style workflows
  prompts/               # editable prompt templates (one per agent)
  state/
    idea.json            # captured idea
    system_brief.md      # initial brief
    prd.json             # product manager output
    progress.md          # human-readable timeline
    decisions.md         # decisions log
    experiments.jsonl    # per-iteration outcomes
    agent_memory.jsonl   # all agent invocations
    done.md              # written only when completion criteria met
  logs/                  # per-command log files
  runs/                  # per-invocation prompt + response capture
  tools/                 # locally-cloned external tools (e.g. pi)
  venv/                  # local python venv
  backups/               # safety copies of overwritten files
```

## Providers

| Provider  | Detection                                | Notes |
|-----------|------------------------------------------|-------|
| `shell`   | always available                         | dummy; echoes prompts and writes stub files. Use for tests, CI, dry runs. |
| `claude`  | `claude` on PATH                         | runs `claude --print` non-interactively. |
| `codex`   | `codex` on PATH                          | runs `codex exec`. |
| `pi`      | `pi` on PATH or `.clk/tools/pi/bin/pi`   | extensible terminal harness. |
| `ollama`  | TCP reachable at `endpoint`              | local-only LLM via HTTP. |

`./scripts/clk providers` prints availability as JSON. Customize per
provider in `.clk/config/providers.json`.

## Workflows

YAML workflows live in `.clk/config/workflows/`. The bundled set:

- `discovery.yaml` - validate problem, users, landscape.
- `product.yaml` - PRD + technical architecture.
- `engineering.yaml` - one full development cycle (default for `clk run`).
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

- **Ralph (`--mode ralph`, default).** Each iteration: Ralph picks one
  measurable improvement, the engineer implements it, QA validates, and
  the harness commits or reverts.
- **Autoresearch (`--mode autoresearch`).** Each iteration: survey
  state, pick the highest-value open question, design and run a small
  experiment, record the learning regardless of pass/fail.

Both loops respect `max_iterations` and stop early when
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
