#!/usr/bin/env bash
# CLK kickoff.
#
# Usage:
#   ./kickoff.sh "your idea or problem statement"
#
# Behavior:
#   1. Loads `.env` from the script's directory (if present). Vars already
#      in your shell environment win over .env, and .env wins over prompts.
#   2. Prompts for anything still missing: provider, max iterations, project
#      name, loop mode, and any provider-specific API keys / endpoints.
#   3. Creates `kickoff-YYYYMMDD-HHMMSS/` in the *current working directory*,
#      copies the harness sources into it, gives it its own git repo, and
#      runs init -> idea -> plan -> run -> loop entirely inside that dir.
#   4. The source directory is never modified. Deleting the kickoff directory
#      returns the project to its pre-kickoff state and you can rerun freely.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# ---------------------------------------------------------------------------
# 1. Parse args
# ---------------------------------------------------------------------------
# The idea/prompt argument is optional. When omitted, the TUI opens with an
# empty input field and the user types their idea directly into the
# dashboard. When provided, the TUI displays it and dispatches the
# engineering workflow before handing control to the user for follow-ups.
if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
  cat <<USAGE
usage: $(basename "$0") ["<idea or problem statement>"]

If no argument is given, the TUI opens with an empty input field.

Optional environment overrides (also accepted via .env in the script directory):
  CLK_PROVIDER          shell | claude | codex | pi | ollama   (default: shell)
  CLK_MAX_ITERATIONS    integer                                (default: 10)
  CLK_LOOP_MODE         ralph | autoresearch                   (default: ralph)
  CLK_PROJECT_NAME      project name shown in commits          (default: clk-app)
  CLK_RUN_INSTALL       true | false - run scripts/install_local.sh first (default: false)
  CLK_NO_TUI            true | false - skip the TUI and run the legacy
                        init/idea/plan/run/loop pipeline (default: false)
  ANTHROPIC_API_KEY     required if CLK_PROVIDER=claude
  OPENAI_API_KEY        required if CLK_PROVIDER=codex
  CLK_OLLAMA_ENDPOINT   used if CLK_PROVIDER=ollama            (default: http://localhost:11434)
  CLK_OLLAMA_MODEL      used if CLK_PROVIDER=ollama            (default: llama3.1)
USAGE
  exit 0
fi
IDEA="${1:-}"

# ---------------------------------------------------------------------------
# 2. Load .env (export every assigned var so subprocesses inherit it)
# ---------------------------------------------------------------------------
if [ -f "$SCRIPT_DIR/.env" ]; then
  echo "[kickoff] loading $SCRIPT_DIR/.env"
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/.env"
  set +a
fi

# ---------------------------------------------------------------------------
# 3. Prompt helpers (ask only if unset/empty AND we have a TTY)
# ---------------------------------------------------------------------------
prompt_default() {
  local name="$1" label="$2" default="$3"
  local current="${!name:-}"
  if [ -n "$current" ]; then return; fi
  if [ -t 0 ]; then
    local v
    read -r -p "$label [$default]: " v
    printf -v "$name" '%s' "${v:-$default}"
  else
    printf -v "$name" '%s' "$default"
  fi
  export "$name"
}

prompt_secret() {
  local name="$1" label="$2"
  local current="${!name:-}"
  if [ -n "$current" ]; then return; fi
  if [ -t 0 ]; then
    local v
    read -r -s -p "$label: " v
    echo
    printf -v "$name" '%s' "$v"
  else
    printf -v "$name" '%s' ""
  fi
  export "$name"
}

# ---------------------------------------------------------------------------
# 4. Resolve settings
# ---------------------------------------------------------------------------
prompt_default CLK_PROVIDER       "Provider (shell|claude|codex|pi|ollama)"     "shell"
prompt_default CLK_MAX_ITERATIONS "Max loop iterations"                          "10"
prompt_default CLK_LOOP_MODE      "Loop mode (ralph|autoresearch)"               "ralph"
prompt_default CLK_PROJECT_NAME   "Project name"                                 "clk-app"
prompt_default CLK_RUN_INSTALL    "Run scripts/install_local.sh? (true|false)"   "false"

case "$CLK_PROVIDER" in
  shell)   ;;
  claude)  prompt_secret ANTHROPIC_API_KEY "ANTHROPIC_API_KEY" ;;
  codex)   prompt_secret OPENAI_API_KEY    "OPENAI_API_KEY"    ;;
  pi)      ;;
  ollama)
    prompt_default CLK_OLLAMA_ENDPOINT "Ollama endpoint" "http://localhost:11434"
    prompt_default CLK_OLLAMA_MODEL    "Ollama model"    "llama3.1"
    ;;
  *)
    echo "[kickoff] unknown provider: $CLK_PROVIDER" >&2
    exit 2
    ;;
esac

case "$CLK_LOOP_MODE" in
  ralph|autoresearch) ;;
  *) echo "[kickoff] invalid CLK_LOOP_MODE='$CLK_LOOP_MODE' (use ralph or autoresearch)" >&2; exit 2 ;;
esac

if ! [[ "$CLK_MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
  echo "[kickoff] CLK_MAX_ITERATIONS must be an integer (got '$CLK_MAX_ITERATIONS')" >&2
  exit 2
fi

# ---------------------------------------------------------------------------
# 5. Create kickoff directory in CWD; never touch the source tree
# ---------------------------------------------------------------------------
TS="$(date +%Y%m%d-%H%M%S)"
KICKOFF_DIR="$(pwd)/kickoff-$TS"
if [ -e "$KICKOFF_DIR" ]; then
  echo "[kickoff] $KICKOFF_DIR already exists; refusing to overwrite" >&2
  exit 1
fi
echo "[kickoff] creating $KICKOFF_DIR"
mkdir -p "$KICKOFF_DIR"

copy_if_present() {
  local src="$1" dst="$2"
  if [ -e "$src" ]; then cp -R "$src" "$dst"; fi
}
copy_if_present "$SCRIPT_DIR/clk_harness"    "$KICKOFF_DIR/clk_harness"
copy_if_present "$SCRIPT_DIR/scripts"        "$KICKOFF_DIR/scripts"
copy_if_present "$SCRIPT_DIR/pyproject.toml" "$KICKOFF_DIR/pyproject.toml"
copy_if_present "$SCRIPT_DIR/README.md"      "$KICKOFF_DIR/README.md"
copy_if_present "$SCRIPT_DIR/.gitignore"     "$KICKOFF_DIR/.gitignore"

# Strip __pycache__ that cp -R may have picked up.
find "$KICKOFF_DIR" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

chmod +x "$KICKOFF_DIR/scripts/clk" \
         "$KICKOFF_DIR/scripts/install_local.sh" \
         "$KICKOFF_DIR/scripts/run_loop.sh" 2>/dev/null || true

# Manifest so future-you knows how this dir was launched.
cat > "$KICKOFF_DIR/KICKOFF.md" <<MANIFEST
# CLK kickoff manifest

| Field            | Value |
|------------------|-------|
| Timestamp        | $TS |
| Source dir       | $SCRIPT_DIR |
| Project name     | $CLK_PROJECT_NAME |
| Provider         | $CLK_PROVIDER |
| Loop mode        | $CLK_LOOP_MODE |
| Max iterations   | $CLK_MAX_ITERATIONS |
| Ran installer    | $CLK_RUN_INSTALL |
| Idea             | $IDEA |

This directory is fully self-contained. Delete it to reset.
MANIFEST

# ---------------------------------------------------------------------------
# 6. Run the harness inside the kickoff directory
# ---------------------------------------------------------------------------
cd "$KICKOFF_DIR"

# Anchor the project root here so find_project_root() returns this dir even
# if a parent directory has its own .clk/.
mkdir -p .clk

# Give the kickoff dir its OWN git repo. Otherwise, when this directory is
# created inside an existing repo, clk init's `git commit` would land in the
# outer repo because git walks upward to find `.git/`.
if command -v git >/dev/null 2>&1; then
  if [ ! -d .git ]; then
    git init -q
    git config user.name  "CLK Kickoff"
    git config user.email "kickoff@local.invalid"
  fi
fi

CLK="./scripts/clk"

if [ "${CLK_RUN_INSTALL,,}" = "true" ]; then
  echo "[kickoff] running scripts/install_local.sh"
  ./scripts/install_local.sh || echo "[kickoff] install_local.sh reported a problem (continuing)"
fi

echo "[kickoff] clk init"
"$CLK" init --name "$CLK_PROJECT_NAME"

echo "[kickoff] activating provider: $CLK_PROVIDER"
CLK_PROVIDER="$CLK_PROVIDER" \
CLK_OLLAMA_ENDPOINT="${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}" \
CLK_OLLAMA_MODEL="${CLK_OLLAMA_MODEL:-llama3.1}" \
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(".clk/config/providers.json")
data = json.loads(p.read_text(encoding="utf-8"))
provider = os.environ["CLK_PROVIDER"]
data["active"] = provider
if provider == "ollama":
    data.setdefault("providers", {}).setdefault("ollama", {})
    data["providers"]["ollama"]["endpoint"] = os.environ["CLK_OLLAMA_ENDPOINT"]
    data["providers"]["ollama"]["model"]    = os.environ["CLK_OLLAMA_MODEL"]
p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
"$CLK" configure --set "default_provider=$CLK_PROVIDER" >/dev/null

if [ "${CLK_NO_TUI:-false}" = "true" ]; then
  # Legacy non-interactive pipeline. Useful for CI / smoke tests.
  if [ -z "$IDEA" ]; then
    echo "[kickoff] CLK_NO_TUI=true requires an idea argument" >&2
    exit 2
  fi
  echo "[kickoff] clk idea"
  "$CLK" idea "$IDEA" --title "$CLK_PROJECT_NAME"
  echo "[kickoff] clk plan"
  "$CLK" plan || echo "[kickoff] plan reported failures (continuing)"
  echo "[kickoff] clk run"
  "$CLK" run || echo "[kickoff] run reported failures (continuing)"
  echo "[kickoff] clk loop --mode $CLK_LOOP_MODE --max-iterations $CLK_MAX_ITERATIONS"
  "$CLK" loop --mode "$CLK_LOOP_MODE" --max-iterations "$CLK_MAX_ITERATIONS"
else
  # Default: hand control to the TUI dashboard. If $IDEA is set, it pre-seeds
  # the idea and starts an engineering cycle; otherwise the dashboard waits
  # for the user to type one into the input field.
  echo "[kickoff] launching TUI (use /quit to exit, /help-style commands listed inside)"
  if [ -n "$IDEA" ]; then
    "$CLK" tui "$IDEA"
  else
    "$CLK" tui
  fi
fi

echo
echo "[kickoff] complete"
echo "[kickoff] kickoff dir: $KICKOFF_DIR"
echo "[kickoff] inspect:     cd \"$KICKOFF_DIR\" && ./scripts/clk status"
echo "[kickoff] reset:       rm -rf \"$KICKOFF_DIR\""
