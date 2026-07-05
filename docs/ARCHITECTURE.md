# Architecture

Part of the [CLK documentation](../README.md). Why CLK exists and how the package is laid out.

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
