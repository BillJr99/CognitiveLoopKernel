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
  CLK_PROVIDER          shell | claude | codex | gemini | pi | ollama | openwebui
                                                               (default: shell)
  CLK_MAX_ITERATIONS    integer                                (default: 10)
  CLK_LOOP_MODE         ralph | autoresearch                   (default: ralph)
  CLK_PROJECT_NAME      project name shown in commits          (default: clk-app)
  CLK_RUN_INSTALL       true | false - run scripts/install_local.sh first (default: false)
  CLK_NO_TUI            true | false - skip the TUI and run the legacy
                        init/idea/plan/run/loop pipeline (default: false)
  CLK_AUTH_MODE         cli | apikey                            (default: cli)
                        cli    = trust the provider CLI's own auth (e.g. you
                                 already ran 'claude login') and don't prompt
                                 for an API key
                        apikey = prompt for / require the API key env var
  ANTHROPIC_API_KEY     used if CLK_PROVIDER=claude  AND CLK_AUTH_MODE=apikey
  OPENAI_API_KEY        used if CLK_PROVIDER=codex   AND CLK_AUTH_MODE=apikey
  GEMINI_API_KEY        used if CLK_PROVIDER=gemini  AND CLK_AUTH_MODE=apikey
                        (GOOGLE_API_KEY is also accepted for gemini)
  CLK_OLLAMA_ENDPOINT   used if CLK_PROVIDER=ollama            (default: http://localhost:11434)
  CLK_OLLAMA_MODEL      used if CLK_PROVIDER=ollama            (default: llama3.1)
  CLK_OPENWEBUI_ENDPOINT  used if CLK_PROVIDER=openwebui        (no default; required)
  CLK_OPENWEBUI_API_KEY   used if CLK_PROVIDER=openwebui        (bearer token)
  CLK_OPENWEBUI_MODEL     used if CLK_PROVIDER=openwebui        (prompted if unset)
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
prompt_default CLK_PROVIDER       "Provider (shell|claude|codex|gemini|pi|ollama|openwebui)" "shell"
prompt_default CLK_MAX_ITERATIONS "Max loop iterations"                          "10"
prompt_default CLK_LOOP_MODE      "Loop mode (ralph|autoresearch)"               "ralph"
prompt_default CLK_PROJECT_NAME   "Project name"                                 "clk-app"
prompt_default CLK_RUN_INSTALL    "Run scripts/install_local.sh? (true|false)"   "false"

# How CLI-based providers authenticate. Default 'cli' = trust whatever the
# tool already has (e.g. 'claude login'). 'apikey' = pass an API key via
# the standard env var. We only ask when the chosen provider is one of
# the CLI-driven ones.
case "$CLK_PROVIDER" in
  claude|codex|gemini)
    prompt_default CLK_AUTH_MODE "Auth mode (cli=use the CLI's existing auth, apikey=set an API key)" "cli"
    case "$CLK_AUTH_MODE" in
      cli|apikey) ;;
      *) echo "[kickoff] invalid CLK_AUTH_MODE='$CLK_AUTH_MODE' (use cli or apikey)" >&2; exit 2 ;;
    esac
    ;;
esac

case "$CLK_PROVIDER" in
  shell)   ;;
  claude)
    if [ "${CLK_AUTH_MODE:-cli}" = "apikey" ]; then
      prompt_secret ANTHROPIC_API_KEY "ANTHROPIC_API_KEY"
    else
      echo "[kickoff] claude: using CLI auth (run 'claude login' if you haven't)"
    fi
    ;;
  codex)
    if [ "${CLK_AUTH_MODE:-cli}" = "apikey" ]; then
      prompt_secret OPENAI_API_KEY "OPENAI_API_KEY"
    else
      echo "[kickoff] codex: using CLI auth (run 'codex login' if you haven't)"
    fi
    ;;
  gemini)
    if [ "${CLK_AUTH_MODE:-cli}" = "apikey" ]; then
      if [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
        prompt_secret GEMINI_API_KEY "GEMINI_API_KEY (or set GOOGLE_API_KEY)"
      fi
    else
      echo "[kickoff] gemini: using CLI auth (run 'gemini auth' or equivalent if needed)"
    fi
    ;;
  pi)      ;;
  ollama)
    prompt_default CLK_OLLAMA_ENDPOINT "Ollama endpoint" "http://localhost:11434"
    prompt_default CLK_OLLAMA_MODEL    "Ollama model"    "llama3.1"
    ;;
  openwebui)
    prompt_default CLK_OPENWEBUI_ENDPOINT "OpenWebUI endpoint (e.g. https://chat.example.com)" "http://localhost:8080"
    prompt_secret  CLK_OPENWEBUI_API_KEY  "OpenWebUI API key (Bearer token)"
    if [ -z "${CLK_OPENWEBUI_MODEL:-}" ]; then
      # Try fetching the model list; let the user pick by index. Fall
      # back to free-form entry if the host is unreachable / unauth'd.
      MODELS_TEXT="$(CLK_OPENWEBUI_ENDPOINT="$CLK_OPENWEBUI_ENDPOINT" \
                     CLK_OPENWEBUI_API_KEY="$CLK_OPENWEBUI_API_KEY" \
                     PYTHONPATH="$SCRIPT_DIR" python3 - <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH",""))
try:
    from clk_harness.providers.openwebui import list_models
    models = list_models(os.environ["CLK_OPENWEBUI_ENDPOINT"], os.environ.get("CLK_OPENWEBUI_API_KEY",""))
except Exception:
    models = []
print("\n".join(models))
PY
)"
      if [ -n "$MODELS_TEXT" ]; then
        echo "[kickoff] available models on $CLK_OPENWEBUI_ENDPOINT:"
        n=0
        while IFS= read -r m; do
          n=$((n+1))
          printf "  %2d) %s\n" "$n" "$m"
        done <<< "$MODELS_TEXT"
        if [ -t 0 ]; then
          read -r -p "Pick a number, or type a model name: " pick
          if [[ "$pick" =~ ^[0-9]+$ ]]; then
            CLK_OPENWEBUI_MODEL="$(echo "$MODELS_TEXT" | sed -n "${pick}p")"
          else
            CLK_OPENWEBUI_MODEL="$pick"
          fi
        else
          CLK_OPENWEBUI_MODEL="$(echo "$MODELS_TEXT" | head -n1)"
        fi
      else
        echo "[kickoff] could not fetch model list from $CLK_OPENWEBUI_ENDPOINT (offline/unauth?)"
        prompt_default CLK_OPENWEBUI_MODEL "OpenWebUI model name (type it)" "llama3.1"
      fi
      export CLK_OPENWEBUI_MODEL
    fi
    if [ -z "${CLK_OPENWEBUI_MODEL:-}" ]; then
      echo "[kickoff] CLK_OPENWEBUI_MODEL is required for openwebui" >&2
      exit 2
    fi
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

# Harness sources, scripts, and packaging metadata all live under
# .clk/harness/ so the project root looks like a normal codebase from
# the agents' point of view. The launcher (.clk/scripts/clk) and the
# installer know to look here.
mkdir -p "$KICKOFF_DIR/.clk/harness"
copy_if_present "$SCRIPT_DIR/clk_harness"    "$KICKOFF_DIR/.clk/harness/clk_harness"
copy_if_present "$SCRIPT_DIR/scripts"        "$KICKOFF_DIR/.clk/harness/scripts"
copy_if_present "$SCRIPT_DIR/pyproject.toml" "$KICKOFF_DIR/.clk/harness/pyproject.toml"
copy_if_present "$SCRIPT_DIR/README.md"      "$KICKOFF_DIR/.clk/harness/README.md"

# Launcher shim lives under .clk/scripts/ so the project root stays clean.
# The shim exports CLK_PROJECT_ROOT so the harness launcher resolves harness
# state in the right place even though the script is inside .clk/.
mkdir -p "$KICKOFF_DIR/.clk/scripts"
cat > "$KICKOFF_DIR/.clk/scripts/clk" <<'SHIM'
#!/usr/bin/env bash
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
export CLK_PROJECT_ROOT="$PROJECT_ROOT"
exec "$PROJECT_ROOT/.clk/harness/scripts/clk" "$@"
SHIM
chmod +x "$KICKOFF_DIR/.clk/scripts/clk"

# Strip __pycache__ that cp -R may have picked up.
find "$KICKOFF_DIR/.clk/harness" -type d -name "__pycache__" -prune -exec rm -rf {} + 2>/dev/null || true

chmod +x "$KICKOFF_DIR/.clk/harness/scripts/clk" \
         "$KICKOFF_DIR/.clk/harness/scripts/install_local.sh" \
         "$KICKOFF_DIR/.clk/harness/scripts/run_loop.sh" 2>/dev/null || true

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

# Write a kickoff-specific .gitignore. Agents operate at the project root
# directly; the harness lives entirely under .clk/ and is ignored wholesale.
cat > .gitignore <<'GITIGNORE'
# All harness state lives under .clk/ — ignore it entirely.
.clk/
/.env
/.env.example
__pycache__/
*.pyc
GITIGNORE

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

CLK="./.clk/scripts/clk"

if [ "${CLK_RUN_INSTALL,,}" = "true" ]; then
  echo "[kickoff] running .clk/harness/scripts/install_local.sh"
  ./.clk/harness/scripts/install_local.sh || echo "[kickoff] install_local.sh reported a problem (continuing)"
fi

echo "[kickoff] clk init"
"$CLK" init --name "$CLK_PROJECT_NAME"

echo "[kickoff] activating provider: $CLK_PROVIDER"
CLK_PROVIDER="$CLK_PROVIDER" \
CLK_OLLAMA_ENDPOINT="${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}" \
CLK_OLLAMA_MODEL="${CLK_OLLAMA_MODEL:-llama3.1}" \
CLK_OPENWEBUI_ENDPOINT="${CLK_OPENWEBUI_ENDPOINT:-http://localhost:8080}" \
CLK_OPENWEBUI_API_KEY="${CLK_OPENWEBUI_API_KEY:-}" \
CLK_OPENWEBUI_MODEL="${CLK_OPENWEBUI_MODEL:-}" \
CLK_AUTH_MODE="${CLK_AUTH_MODE:-cli}" \
python3 - <<'PY'
import json, os
from pathlib import Path
p = Path(".clk/config/providers.json")
data = json.loads(p.read_text(encoding="utf-8"))
provider = os.environ["CLK_PROVIDER"]
data["active"] = provider
provs = data.setdefault("providers", {})
auth_mode = os.environ.get("CLK_AUTH_MODE", "cli")
# For CLI-driven providers, mode=cli (default) spawns the CLI subprocess.
# mode=api makes the provider call the upstream HTTP API directly with no
# subprocess at all - which is exactly what the user expects when they
# choose "apikey" auth: the API key alone, no local CLI dependency.
for cli_provider in ("claude", "codex", "gemini"):
    provs.setdefault(cli_provider, {"type": cli_provider})
    provs[cli_provider]["mode"] = "api" if auth_mode == "apikey" else "cli"
if provider == "claude" and auth_mode == "apikey":
    provs["claude"]["api_key"] = os.environ.get("ANTHROPIC_API_KEY", "")
if provider == "codex" and auth_mode == "apikey":
    provs["codex"]["api_key"] = os.environ.get("OPENAI_API_KEY", "")
if provider == "gemini" and auth_mode == "apikey":
    provs["gemini"]["api_key"] = (
        os.environ.get("GEMINI_API_KEY", "")
        or os.environ.get("GOOGLE_API_KEY", "")
    )
if provider == "ollama":
    provs.setdefault("ollama", {})
    provs["ollama"]["endpoint"] = os.environ["CLK_OLLAMA_ENDPOINT"]
    provs["ollama"]["model"]    = os.environ["CLK_OLLAMA_MODEL"]
elif provider == "openwebui":
    provs.setdefault("openwebui", {"type": "openwebui"})
    provs["openwebui"]["type"]     = "openwebui"
    provs["openwebui"]["endpoint"] = os.environ["CLK_OPENWEBUI_ENDPOINT"]
    provs["openwebui"]["api_key"]  = os.environ["CLK_OPENWEBUI_API_KEY"]
    provs["openwebui"]["model"]    = os.environ["CLK_OPENWEBUI_MODEL"]
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
echo "[kickoff] inspect:     cd \"$KICKOFF_DIR\" && ./.clk/scripts/clk status"
echo "[kickoff] reset:       rm -rf \"$KICKOFF_DIR\""
