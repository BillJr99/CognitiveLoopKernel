#!/usr/bin/env bash
# CLK kickoff — thin wrapper around the `clk kickoff` subcommand.
#
# Usage:
#   ./kickoff.sh [OPTIONS] ["idea or problem statement"]
#
# Options (run `./kickoff.sh --help` for the authoritative list):
#   --setup                  Interactive wizard to write or update .env
#   --restore                Restore .env from .env.bak (undo last --setup)
#   --list                   List past kickoff dirs under workspace/
#   --clean DURATION         Delete kickoff dirs older than DURATION (e.g. 7d, 30m)
#   --provider <p>           Override CLK_PROVIDER
#   --max-iterations <n>     Override CLK_MAX_ITERATIONS
#   --project-name <name>    Override CLK_PROJECT_NAME
#   --no-tui                 Set CLK_NO_TUI=true  (non-interactive pipeline)
#   --tui                    Set CLK_NO_TUI=false (TUI dashboard, the default)
#   --run-install            Set CLK_RUN_INSTALL=true
#   -h, --help               Show this help
#
# The kickoff flow itself — .env handling, the setup wizard, workspace
# scaffolding, CLK_* env-var overrides, provider activation, and the
# pipeline/TUI drivers — lives in clk_harness/kickoff.py.  This script
# only resolves a Python interpreter and execs:
#
#   python -m clk_harness.cli kickoff "$@"
#
# so the historical `./kickoff.sh` entry point keeps working from a bare
# `git clone` with nothing but bash + python3 installed.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# Interpreter resolution mirrors scripts/clk: prefer a project-local
# .clk/venv (populated by scripts/install_local.sh), then fall back to
# python3 / python on PATH.
PY=""
if [ -x "$SCRIPT_DIR/.clk/venv/bin/python" ]; then
  PY="$SCRIPT_DIR/.clk/venv/bin/python"
elif command -v python3 >/dev/null 2>&1; then
  PY="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
  PY="$(command -v python)"
else
  echo "kickoff: no python interpreter found on PATH" >&2
  exit 127
fi

# Make the adjacent clk_harness/ package importable from a bare checkout
# (no pip install needed); an installed package keeps working too.
export PYTHONPATH="${SCRIPT_DIR}${PYTHONPATH:+:${PYTHONPATH}}"

exec "$PY" -m clk_harness.cli kickoff "$@"
