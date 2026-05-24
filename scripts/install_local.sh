#!/usr/bin/env bash
# Local-only install for CLK.
#
# WHAT THIS SCRIPT DOES
#   Installs the Python harness into a project-local virtual environment
#   so the `clk` CLI and the in-process FastAPI / TUI work without
#   polluting the user's system Python. After it finishes, the
#   project's `.clk/venv/bin/clk` is the canonical entry point (and
#   `scripts/clk` is a shim that finds it).
#
#   The script is idempotent — re-running it upgrades pip and
#   reinstalls the package in editable mode. It does NOT delete the
#   existing venv so cached deps persist between runs.
#
# DIRECTORY LAYOUT (in a kickoff'd project)
#   <project>/                          ← your code + the harness state
#       .clk/                           ← all harness state (recoverable)
#           harness/                    ← copy of the CLK source tree
#               scripts/install_local.sh ← THIS SCRIPT
#               pyproject.toml          ← deps declared here
#           venv/                       ← preferred install path (1)
#               bin/clk                 ← the console script callers use
#               bin/python              ← matched against pyproject.toml deps
#           site-packages/              ← fallback when venv has no pip (2)
#           config/                     ← clk.config.json, providers.json, …
#           state/                      ← agent memory, casting log, …
#           logs/, runs/, backups/, blackboard/
#
#   `CLK_PROJECT_ROOT` (set by the kickoff shim) lets this script find
#   the project root from `.clk/harness/`. When unset, this script
#   assumes it's running from a plain checkout and uses its own parent.
#
# INSTALL STRATEGY (tried in order)
#   1. Create `.clk/venv` with pip and `pip install -e .` into it. This
#      is the preferred path — it picks up every dep declared in
#      pyproject.toml plus any extras and exposes the `clk` console
#      script inside the venv. The launcher `scripts/clk` picks up
#      `.clk/venv/bin/clk` automatically.
#   2. If the venv has no pip (ensurepip missing — common on
#      stripped-down distros), install just the runtime dependencies
#      parsed from pyproject.toml into `.clk/site-packages` via
#      system pip's `--target`. The launcher then adds that directory
#      to PYTHONPATH; the package itself runs from the source tree.
#   3. If neither works, print the apt one-liner and exit cleanly.
#      The harness still runs via its mini-YAML fallback (no PyYAML)
#      but lacks the optional FastAPI / Telegram extras.
#
# OPTIONAL EXTRAS
#   The first positional argument picks an extras group from
#   pyproject.toml:
#     ./scripts/install_local.sh           # runtime deps only
#     ./scripts/install_local.sh dev       # adds pytest, pytest-asyncio
#     ./scripts/install_local.sh api       # adds FastAPI + uvicorn for REST API
#     ./scripts/install_local.sh "api,dev" # both
#
# WHAT THIS SCRIPT DOES *NOT* INSTALL
#   * Provider CLIs (claude, codex, gemini, pi) — those are installed by
#     `kickoff.sh --setup` and by `/install` from inside the TUI; see
#     the README "Provider and authentication" section.
#   * Telegram-bot dependencies — handled by
#     scripts/telegram_setup_wizard.sh; see README "Telegram Bot".
#   * Docker — the test orchestrator at scripts/run_all_tests.sh uses
#     Docker if available but falls back to --local mode; see README
#     "Testing" for the breakdown.
#   * GitHub integration — handled by kickoff.sh's GitHub block; see
#     README "GitHub integration".
#
# RELATED ENTRY POINTS
#   * scripts/clk                — launcher shim that resolves `.clk/venv`
#                                  or `.clk/site-packages` automatically
#   * scripts/install_tool.sh    — installs a provider CLI on demand
#                                  (claude, codex, gemini, ollama, pi)
#   * scripts/run_loop.sh        — convenience wrapper around `clk loop`
#   * scripts/run_all_tests.sh   — full test orchestrator (Docker or local)
#   * kickoff.sh                 — top-level project bootstrap; calls this
#                                  script when CLK_RUN_INSTALL=true
#
# See the README "Robustness loops" and "Cost guardrails" sections for
# the runtime config knobs (CLK_ROBUSTNESS_*, provider retry, etc.)
# this script writes — they're tuned via `.env` / kickoff.sh, not here.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
HARNESS_HOME="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
# In the kickoff layout the harness lives under <project>/.clk/harness/,
# but the venv and site-packages should live at <project>/.clk/. Honor
# CLK_PROJECT_ROOT (set by the kickoff shim) when present.
PROJECT_ROOT="${CLK_PROJECT_ROOT:-$HARNESS_HOME}"
VENV="$PROJECT_ROOT/.clk/venv"
TARGET_DIR="$PROJECT_ROOT/.clk/site-packages"
EXTRAS="${1:-}"

# pip needs to install from the harness sources (where pyproject.toml is)
# but venv / site-packages are anchored to PROJECT_ROOT.
cd "$HARNESS_HOME"

pkg_spec() {
  if [ -n "$EXTRAS" ]; then printf '%s' ".[$EXTRAS]"; else printf '.'; fi
}

# Extract `[project].dependencies` from pyproject.toml without requiring
# tomllib (3.11+) or tomli. The format is small and regular.
extract_deps() {
  python3 - "$PROJECT_ROOT/pyproject.toml" <<'PY'
import re, sys
text = open(sys.argv[1], encoding="utf-8").read()
m = re.search(r"^\s*dependencies\s*=\s*\[([^\]]*)\]", text, re.MULTILINE | re.DOTALL)
if not m:
    sys.exit(0)
for line in m.group(1).splitlines():
    line = line.strip().rstrip(",").strip()
    if not line or line.startswith("#"):
        continue
    # Strip surrounding quotes.
    if (line.startswith('"') and line.endswith('"')) or (line.startswith("'") and line.endswith("'")):
        line = line[1:-1]
    if line:
        print(line)
PY
}

# ---------------------------------------------------------------------------
# Path 1: full venv with pip
# ---------------------------------------------------------------------------
if [ ! -x "$VENV/bin/pip" ]; then
  echo "[install_local] attempting venv at $VENV"
  python3 -m venv "$VENV" 2>/dev/null || true
fi

if [ -x "$VENV/bin/pip" ]; then
  echo "[install_local] using venv pip"
  "$VENV/bin/pip" install --quiet --upgrade pip
  echo "[install_local] installing $(pkg_spec) (editable)"
  "$VENV/bin/pip" install --quiet -e "$(pkg_spec)"
  echo "[install_local] done."
  echo "[install_local]   python: $VENV/bin/python"
  echo "[install_local]   clk:    $VENV/bin/clk  (or use ./scripts/clk)"
  exit 0
fi

# ---------------------------------------------------------------------------
# Path 2: pip --target site-packages directory (no venv-pip available)
# ---------------------------------------------------------------------------
if command -v pip3 >/dev/null 2>&1 || command -v pip >/dev/null 2>&1; then
  PIP="$(command -v pip3 || command -v pip)"
  echo "[install_local] no venv pip; falling back to $PIP --target $TARGET_DIR"
  mkdir -p "$TARGET_DIR"

  DEPS=$(extract_deps)
  if [ -n "$EXTRAS" ]; then
    echo "[install_local] note: --target mode does not resolve extras automatically;"
    echo "[install_local]       only [project].dependencies will be installed."
  fi

  if [ -z "$DEPS" ]; then
    echo "[install_local] no runtime dependencies found in pyproject.toml; nothing to install."
  else
    echo "[install_local] installing dependencies into $TARGET_DIR:"
    while IFS= read -r dep; do
      [ -z "$dep" ] && continue
      echo "[install_local]   $dep"
      "$PIP" install --quiet --target "$TARGET_DIR" --upgrade "$dep"
    done <<< "$DEPS"
  fi

  echo "[install_local] done."
  echo "[install_local]   site-packages: $TARGET_DIR"
  echo "[install_local]   ./scripts/clk auto-adds this to PYTHONPATH."
  exit 0
fi

# ---------------------------------------------------------------------------
# Path 3: nothing available - degrade gracefully
# ---------------------------------------------------------------------------
cat <<MSG
[install_local] No usable pip found.
[install_local] On Debian/Ubuntu, one of these unblocks the local install:
[install_local]   sudo apt install python3-venv python3-pip   # for the venv path
[install_local] or just install pip:
[install_local]   sudo apt install python3-pip                # for the --target path
[install_local]
[install_local] CLK still runs without dependencies via its built-in mini-YAML
[install_local] fallback. Real providers (claude/codex/ollama) keep working too.
MSG
exit 0
