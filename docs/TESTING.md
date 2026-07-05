# Testing

Part of the [CLK documentation](../README.md). Test suites and how to run them.

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
