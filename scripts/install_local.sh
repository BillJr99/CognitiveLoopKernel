#!/usr/bin/env bash
# Local-only install for CLK.
#
# Strategy (in order):
#   1. Create `.clk/venv` with pip and `pip install -e .` into it. This is the
#      preferred path - it picks up every dep declared in pyproject.toml plus
#      any extras and exposes the `clk` console script inside the venv.
#   2. If the venv has no pip (ensurepip missing), install just the runtime
#      dependencies parsed from pyproject.toml into `.clk/site-packages` via
#      system pip's `--target`. The launcher (`scripts/clk`) adds that dir to
#      PYTHONPATH; the package itself runs from the source tree.
#   3. If neither works, print the apt one-liner and exit cleanly. The
#      harness still runs via its mini-YAML fallback.
#
# Optional first arg picks an extras group from pyproject.toml:
#   ./scripts/install_local.sh dev   # also installs pytest

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
VENV="$PROJECT_ROOT/.clk/venv"
TARGET_DIR="$PROJECT_ROOT/.clk/site-packages"
EXTRAS="${1:-}"

cd "$PROJECT_ROOT"

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
