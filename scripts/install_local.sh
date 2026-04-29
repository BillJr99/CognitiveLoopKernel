#!/usr/bin/env bash
# Local-only install for CLK.
#
# Creates .clk/venv (if not present), installs PyYAML inside it, and never
# touches the global Python environment. PyYAML is required for YAML
# workflow parsing; if pip is unavailable, the harness still runs but
# YAML workflows are disabled.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
VENV="$PROJECT_ROOT/.clk/venv"

cd "$PROJECT_ROOT"

if [ ! -d "$VENV" ]; then
  echo "[install_local] creating venv at $VENV"
  python3 -m venv "$VENV" || python3 -m venv --without-pip "$VENV"
fi

if [ -x "$VENV/bin/pip" ]; then
  echo "[install_local] installing PyYAML into local venv"
  "$VENV/bin/pip" install --quiet --upgrade pip || true
  "$VENV/bin/pip" install --quiet "PyYAML>=6.0"
else
  echo "[install_local] pip not available in venv; YAML workflows will be disabled."
  echo "[install_local] (the dummy provider and CLI still work without YAML)"
fi

echo "[install_local] done. Use ./scripts/clk to run the harness."
