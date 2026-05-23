#!/usr/bin/env bash
# CLK kickoff — driven entirely by .env and optional --arg overrides.
#
# Usage:
#   ./kickoff.sh [OPTIONS] ["idea or problem statement"]
#
# Options:
#   --setup                  Interactive wizard to write or update .env
#   --provider <p>           Override CLK_PROVIDER
#   --max-iterations <n>     Override CLK_MAX_ITERATIONS
#   --project-name <name>    Override CLK_PROJECT_NAME
#   --no-tui                 Set CLK_NO_TUI=true (non-interactive pipeline)
#   --tui                    Set CLK_NO_TUI=false (TUI dashboard, the default)
#   --run-install            Set CLK_RUN_INSTALL=true
#   -h, --help               Show this help
#
# Configuration (highest-to-lowest precedence):
#   --arg overrides  →  shell environment vars  →  .env file  →  built-in defaults
#
# Normal runs ask no questions. If required configuration is missing, kickoff
# prints exactly what is needed and offers to launch --setup.  Run --setup at
# any time to create or update .env; existing values become the default answer
# to every question so you can press Enter to keep them.

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"

# ===========================================================================
# Setup wizard — invoked by --setup or offered when config is incomplete.
# Reads from /dev/tty so it works inside `docker run -it` even when stdin
# is not a terminal.
# ===========================================================================
_clk_setup() {
  { exec 3</dev/tty; } 2>/dev/null || exec 3<&0
  { exec 4>/dev/tty; } 2>/dev/null || exec 4>&2

  _sv_read() {
    local prompt="$1" default="$2" v
    printf '%s [%s]: ' "$prompt" "$default" >&4
    IFS= read -r v <&3
    printf '%s' "${v:-$default}"
  }

  _sv_secret() {
    local prompt="$1" v
    printf '%s (leave blank to keep): ' "$prompt" >&4
    stty -echo </dev/tty 2>/dev/null || true
    IFS= read -r v <&3
    stty echo  </dev/tty 2>/dev/null || true
    printf '\n' >&4
    printf '%s' "$v"
  }

  local env_file="$SCRIPT_DIR/.env"

  # Seed defaults from .env.example first, then let an existing .env override.
  # The wizard always rewrites .env at the end; press Enter to keep a value.
  if [ -f "$SCRIPT_DIR/.env.example" ]; then
    set -a; . "$SCRIPT_DIR/.env.example"; set +a
  fi
  if [ -s "$env_file" ]; then
    set -a; . "$env_file"; set +a
    printf '[setup] loaded existing values from %s\n' "$env_file" >&4
  else
    printf '[setup] %s is empty or missing — using .env.example defaults\n' "$env_file" >&4
  fi

  printf '\n=== CLK Setup Wizard ===\nPress Enter to keep the value shown in [brackets].\n\n' >&4

  local provider max_iter proj_name run_install no_tui auth_mode
  local anthropic_key openai_key gemini_key google_key
  local ollama_ep ollama_model owui_ep owui_key owui_model
  local pi_model pi_key pi_key_type
  local git_name git_email new

  provider="$(_sv_read    "Provider (shell|claude|codex|gemini|pi|ollama|openwebui)" "${CLK_PROVIDER:-shell}")"
  max_iter="$(_sv_read    "Max loop iterations"                                       "${CLK_MAX_ITERATIONS:-10}")"
  proj_name="$(_sv_read   "Project name"                                              "${CLK_PROJECT_NAME:-clk-app}")"
  run_install="$(_sv_read "Run install_local.sh (true|false)"                        "${CLK_RUN_INSTALL:-false}")"
  no_tui="$(_sv_read      "Skip TUI / non-interactive (true|false)"                  "${CLK_NO_TUI:-false}")"

  auth_mode="${CLK_AUTH_MODE:-cli}"
  case "$provider" in
    claude|codex|gemini)
      auth_mode="$(_sv_read "Auth mode (cli=use existing login, apikey=use API key)" "$auth_mode")"
      ;;
  esac

  anthropic_key="${ANTHROPIC_API_KEY:-}"
  openai_key="${OPENAI_API_KEY:-}"
  gemini_key="${GEMINI_API_KEY:-}"
  google_key="${GOOGLE_API_KEY:-}"

  if [ "$auth_mode" = "apikey" ]; then
    case "$provider" in
      claude)  new="$(_sv_secret "ANTHROPIC_API_KEY")"; [ -n "$new" ] && anthropic_key="$new" ;;
      codex)   new="$(_sv_secret "OPENAI_API_KEY")";    [ -n "$new" ] && openai_key="$new" ;;
      gemini)  new="$(_sv_secret "GEMINI_API_KEY")";    [ -n "$new" ] && gemini_key="$new" ;;
    esac
  fi

  ollama_ep="${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}"
  ollama_model="${CLK_OLLAMA_MODEL:-llama3.1}"
  owui_ep="${CLK_OPENWEBUI_ENDPOINT:-http://localhost:8080}"
  owui_key="${CLK_OPENWEBUI_API_KEY:-}"
  owui_model="${CLK_OPENWEBUI_MODEL:-}"
  pi_model="${CLK_PI_MODEL:-}"
  pi_key="${CLK_PI_API_KEY:-}"
  # No default for key type so the user can leave it blank to skip API key auth.
  pi_key_type="${CLK_PI_KEY_TYPE:-}"

  case "$provider" in
    ollama)
      ollama_ep="$(_sv_read    "Ollama endpoint" "$ollama_ep")"
      ollama_model="$(_sv_read "Ollama model"    "$ollama_model")"
      ;;
    openwebui)
      owui_ep="$(_sv_read "OpenWebUI endpoint" "$owui_ep")"
      printf '  (API key is only needed for authenticated OpenWebUI instances.)\n' >&4
      new="$(_sv_secret   "OpenWebUI API key")"; [ -n "$new" ] && owui_key="$new"
      # Try to fetch the live model list so the user can pick by number.
      local models_text=""
      if [ -n "$owui_ep" ]; then
        models_text="$(CLK_OPENWEBUI_ENDPOINT="$owui_ep" \
                       CLK_OPENWEBUI_API_KEY="$owui_key" \
                       PYTHONPATH="$SCRIPT_DIR" \
                       python3 - 2>/dev/null <<'PY'
import os, sys
sys.path.insert(0, os.environ.get("PYTHONPATH",""))
try:
    from clk_harness.providers.openwebui import list_models
    models = list_models(os.environ["CLK_OPENWEBUI_ENDPOINT"],
                         os.environ.get("CLK_OPENWEBUI_API_KEY",""))
except Exception:
    models = []
print("\n".join(models))
PY
)" || true
      fi
      if [ -n "$models_text" ]; then
        printf '[setup] available models on %s:\n' "$owui_ep" >&4
        local n=0 m
        while IFS= read -r m; do
          n=$((n+1))
          printf '  %2d) %s\n' "$n" "$m" >&4
        done <<< "$models_text"
        printf 'Pick a number, or type a model name [%s]: ' "$owui_model" >&4
        local pick=""
        IFS= read -r pick <&3
        if [[ "${pick:-}" =~ ^[0-9]+$ ]]; then
          owui_model="$(echo "$models_text" | sed -n "${pick}p")"
        elif [ -n "${pick:-}" ]; then
          owui_model="$pick"
        fi
        # empty pick → keep current owui_model
      else
        [ -n "$owui_ep" ] && printf '[setup] could not fetch model list (offline/unauth?)\n' >&4
        owui_model="$(_sv_read "OpenWebUI model name" "${owui_model:-llama3.1}")"
      fi
      ;;
    pi)
      printf '\n  Examples: openrouter/free  openrouter/auto  anthropic/claude-3-5-sonnet\n' >&4
      pi_model="$(_sv_read "pi model (leave blank for pi default)" "$pi_model")"
      if command -v pi >/dev/null 2>&1; then
        local open_pi
        open_pi="$(_sv_read "Open a shell to run pi commands (e.g. pi login)? (y/N)" "N")"
        if [ "${open_pi,,}" = "y" ]; then
          printf '[setup] Dropping into a shell — run pi commands, then type exit to return.\n' >&4
          PS1='[pi-setup]$ ' "${SHELL:-bash}" -i </dev/tty >&4 2>&4 || true
          printf '[setup] Returned from pi setup shell.\n' >&4
        fi
      fi
      printf '  Key type maps which env var receives your API key:\n' >&4
      printf '    openrouter → OPENROUTER_API_KEY   openai → OPENAI_API_KEY\n' >&4
      printf '  Leave blank if pi login above already handled auth.\n' >&4
      pi_key_type="$(_sv_read "Key type (openrouter|openai|anthropic|<provider>, blank to skip)" "$pi_key_type")"
      if [ -n "$pi_key_type" ]; then
        new="$(_sv_secret "API key for $pi_key_type")"; [ -n "$new" ] && pi_key="$new"
      fi
      ;;
  esac

  printf '\n--- Telegram bot (two-way chat control) ---\n' >&4
  local tg_setup tg_skip
  local default_tg="N"
  [ "${CLK_TELEGRAM_ENABLED:-false}" = "true" ] && default_tg="y"
  tg_setup="$(_sv_read "Set up Telegram bot now? (y/N)" "$default_tg")"
  if [ "${tg_setup,,}" = "y" ]; then
    tg_skip="false"
  else
    tg_skip="true"
    printf '[setup] Skipping Telegram. CLK_TELEGRAM_SKIP=true will be written to .env.\n' >&4
  fi

  printf '\n--- Git identity (used in kickoff commits) ---\n' >&4
  local cur_name cur_email
  cur_name="$(git config --global user.name  2>/dev/null || true)"
  cur_email="$(git config --global user.email 2>/dev/null || true)"
  printf '  Current global git name:  %s\n' "${cur_name:-<not set>}"  >&4
  printf '  Current global git email: %s\n' "${cur_email:-<not set>}" >&4

  git_name="$(_sv_read  "Git user.name  (blank = keep current)" "${CLK_GIT_NAME:-}")"
  git_email="$(_sv_read "Git user.email (blank = keep current)" "${CLK_GIT_EMAIL:-}")"

  exec 3<&- 2>/dev/null || true

  cat > "$env_file" <<ENV
# Generated by kickoff.sh --setup on $(date)
CLK_PROVIDER=$provider
CLK_MAX_ITERATIONS=$max_iter
CLK_PROJECT_NAME=$proj_name
CLK_RUN_INSTALL=$run_install
CLK_NO_TUI=$no_tui

# Auth mode for CLI-driven providers (claude, codex, gemini)
CLK_AUTH_MODE=$auth_mode

# API keys (only used when CLK_AUTH_MODE=apikey)
ANTHROPIC_API_KEY=$anthropic_key
OPENAI_API_KEY=$openai_key
GEMINI_API_KEY=$gemini_key
GOOGLE_API_KEY=$google_key

# HTTP-based providers
CLK_OLLAMA_ENDPOINT=$ollama_ep
CLK_OLLAMA_MODEL=$ollama_model
CLK_OPENWEBUI_ENDPOINT=$owui_ep
CLK_OPENWEBUI_API_KEY=$owui_key
CLK_OPENWEBUI_MODEL=$owui_model

# Pi provider
CLK_PI_MODEL=$pi_model
CLK_PI_API_KEY=$pi_key
CLK_PI_KEY_TYPE=$pi_key_type

# Git identity for kickoff commits (overrides global git config inside containers)
CLK_GIT_NAME=$git_name
CLK_GIT_EMAIL=$git_email

# Telegram bot (populated by scripts/telegram_setup_wizard.sh when enabled)
CLK_TELEGRAM_BOT_TOKEN=${CLK_TELEGRAM_BOT_TOKEN:-}
CLK_TELEGRAM_ALLOWED_USERS=${CLK_TELEGRAM_ALLOWED_USERS:-}
CLK_TELEGRAM_ENABLED=${CLK_TELEGRAM_ENABLED:-false}
CLK_TELEGRAM_WORKSPACE=${CLK_TELEGRAM_WORKSPACE:-}
CLK_TELEGRAM_SKIP=$tg_skip
ENV

  printf '\n[setup] saved %s\n' "$env_file" >&4

  if [ "${tg_setup,,}" = "y" ]; then
    printf '\n[setup] launching Telegram wizard...\n' >&4
    CLK_ENV_FILE="$env_file" "$SCRIPT_DIR/scripts/telegram_setup_wizard.sh" >&4 2>&4 || \
      printf '[setup] telegram wizard exited non-zero; continuing\n' >&4
  fi

  exec 4>&- 2>/dev/null || true
}

# ===========================================================================
# Apply built-in defaults for every var that has one.
# Call this after loading .env and applying --arg overrides.
# ===========================================================================
_apply_defaults() {
  CLK_PROVIDER="${CLK_PROVIDER:-shell}"
  CLK_MAX_ITERATIONS="${CLK_MAX_ITERATIONS:-10}"
  CLK_PROJECT_NAME="${CLK_PROJECT_NAME:-clk-app}"
  CLK_RUN_INSTALL="${CLK_RUN_INSTALL:-false}"
  CLK_NO_TUI="${CLK_NO_TUI:-false}"
  CLK_AUTH_MODE="${CLK_AUTH_MODE:-cli}"
  CLK_OLLAMA_ENDPOINT="${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}"
  CLK_OLLAMA_MODEL="${CLK_OLLAMA_MODEL:-llama3.1}"
}

# ===========================================================================
# Validate resolved config.  Prints one line per problem; silent when OK.
# ===========================================================================
_clk_missing() {
  case "$CLK_PROVIDER" in
    shell) ;;
    claude)
      if [ "${CLK_AUTH_MODE}" = "apikey" ] && [ -z "${ANTHROPIC_API_KEY:-}" ]; then
        echo "ANTHROPIC_API_KEY is unset — required when CLK_PROVIDER=claude and CLK_AUTH_MODE=apikey (or set CLK_AUTH_MODE=cli to use 'claude login')"
      fi ;;
    codex)
      if [ "${CLK_AUTH_MODE}" = "apikey" ] && [ -z "${OPENAI_API_KEY:-}" ]; then
        echo "OPENAI_API_KEY is unset — required when CLK_PROVIDER=codex and CLK_AUTH_MODE=apikey (or set CLK_AUTH_MODE=cli)"
      fi ;;
    gemini)
      if [ "${CLK_AUTH_MODE}" = "apikey" ] && [ -z "${GEMINI_API_KEY:-}" ] && [ -z "${GOOGLE_API_KEY:-}" ]; then
        echo "GEMINI_API_KEY (or GOOGLE_API_KEY) is unset — required when CLK_PROVIDER=gemini and CLK_AUTH_MODE=apikey"
      fi ;;
    pi) ;;   # Nothing strictly required; pi login handles auth
    ollama) ;; # Has built-in defaults
    openwebui)
      [ -z "${CLK_OPENWEBUI_ENDPOINT:-}" ] && \
        echo "CLK_OPENWEBUI_ENDPOINT is unset — required for CLK_PROVIDER=openwebui"
      [ -z "${CLK_OPENWEBUI_MODEL:-}" ] && \
        echo "CLK_OPENWEBUI_MODEL is unset — required for CLK_PROVIDER=openwebui (use --setup to pick from a live model list)"
      ;;
    *)
      echo "CLK_PROVIDER='$CLK_PROVIDER' is not recognised (valid: shell|claude|codex|gemini|pi|ollama|openwebui)"
      ;;
  esac

  if ! [[ "$CLK_MAX_ITERATIONS" =~ ^[0-9]+$ ]]; then
    echo "CLK_MAX_ITERATIONS must be a positive integer (got '$CLK_MAX_ITERATIONS')"
  fi

  if [ "${CLK_NO_TUI:-false}" = "true" ] && [ -z "${IDEA:-}" ]; then
    echo "An idea argument is required when CLK_NO_TUI=true — pass it as the first positional argument"
  fi
}

# ===========================================================================
# 1. Parse arguments
# ===========================================================================
SETUP_MODE=false
_OVR_PROVIDER=""
_OVR_MAX_ITER=""
_OVR_PROJ_NAME=""
_OVR_NO_TUI=""
_OVR_RUN_INSTALL=""
IDEA=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup) SETUP_MODE=true; shift ;;
    -h|--help)
      cat <<USAGE
usage: $(basename "$0") [OPTIONS] ["<idea or problem statement>"]

Options:
  --setup                  Interactive wizard to write or update .env
  --provider <p>           Override CLK_PROVIDER
  --max-iterations <n>     Override CLK_MAX_ITERATIONS
  --project-name <name>    Override CLK_PROJECT_NAME
  --no-tui                 Set CLK_NO_TUI=true  (non-interactive pipeline)
  --tui                    Set CLK_NO_TUI=false (TUI dashboard, the default)
  --run-install            Set CLK_RUN_INSTALL=true
  -h, --help               Show this help

Configuration (highest-to-lowest precedence):
  --arg overrides  →  shell env vars  →  .env file  →  built-in defaults

Providers:
  shell      No AI — harness scaffolding only
  claude     Anthropic Claude CLI  (CLK_AUTH_MODE=cli, default) or API key
  codex      OpenAI Codex CLI      (CLK_AUTH_MODE=cli, default) or API key
  gemini     Google Gemini CLI     (CLK_AUTH_MODE=cli, default) or API key
  pi         Pi coding agent       (CLK_PI_MODEL, CLK_PI_KEY_TYPE, CLK_PI_API_KEY)
  ollama     Local Ollama          (CLK_OLLAMA_ENDPOINT, CLK_OLLAMA_MODEL)
  openwebui  OpenWebUI             (CLK_OPENWEBUI_ENDPOINT, CLK_OPENWEBUI_MODEL;
                                    CLK_OPENWEBUI_API_KEY is optional — only needed
                                    for authenticated instances)

Run --setup to configure interactively; values are saved to .env and used as
defaults in future runs.  If required config is missing on a normal run,
kickoff prints what is needed and offers to run --setup immediately.

Environment variables (accepted directly or via .env):
  CLK_PROVIDER, CLK_MAX_ITERATIONS, CLK_PROJECT_NAME, CLK_RUN_INSTALL,
  CLK_NO_TUI, CLK_AUTH_MODE, ANTHROPIC_API_KEY, OPENAI_API_KEY,
  GEMINI_API_KEY, GOOGLE_API_KEY, CLK_OLLAMA_ENDPOINT, CLK_OLLAMA_MODEL,
  CLK_OPENWEBUI_ENDPOINT, CLK_OPENWEBUI_MODEL,
  CLK_OPENWEBUI_API_KEY (optional — only for authenticated OpenWebUI instances),
  CLK_PI_MODEL, CLK_PI_KEY_TYPE, CLK_PI_API_KEY, CLK_GIT_NAME, CLK_GIT_EMAIL
USAGE
      exit 0
      ;;
    --provider=*)       _OVR_PROVIDER="${1#*=}";  shift ;;
    --provider)
      [[ $# -lt 2 || "$2" == -* ]] && { printf '[kickoff] --provider requires a value\n' >&2; exit 2; }
      _OVR_PROVIDER="$2"; shift 2 ;;
    --max-iterations=*) _OVR_MAX_ITER="${1#*=}";   shift ;;
    --max-iterations)
      [[ $# -lt 2 || "$2" == -* ]] && { printf '[kickoff] --max-iterations requires a value\n' >&2; exit 2; }
      _OVR_MAX_ITER="$2"; shift 2 ;;
    --project-name=*)   _OVR_PROJ_NAME="${1#*=}";  shift ;;
    --project-name)
      [[ $# -lt 2 || "$2" == -* ]] && { printf '[kickoff] --project-name requires a value\n' >&2; exit 2; }
      _OVR_PROJ_NAME="$2"; shift 2 ;;
    --no-tui)           _OVR_NO_TUI="true";         shift ;;
    --tui)              _OVR_NO_TUI="false";         shift ;;
    --run-install)      _OVR_RUN_INSTALL="true";    shift ;;
    --)                 shift; [ $# -gt 0 ] && { IDEA="$1"; shift; }; break ;;
    -*)
      printf '[kickoff] unknown option: %s\n' "$1" >&2
      printf 'Run  %s --help  for usage.\n' "'$(basename "$0")'" >&2
      exit 2 ;;
    *) IDEA="$1"; shift ;;
  esac
done

if $SETUP_MODE; then
  _clk_setup
  printf '[setup] run  %s  to start a new session\n' "'$(basename "$0")'" >/dev/tty
  exit 0
fi

# ===========================================================================
# 1b. First-run nudge: if .env is missing and we have a TTY, offer setup
# inline so the user doesn't have to know about --setup. Declining falls
# through to defaults. CI / non-interactive containers skip silently.
# ===========================================================================
if [ ! -s "$SCRIPT_DIR/.env" ]; then
  if { exec 6<>/dev/tty; } 2>/dev/null; then
    if [ -f "$SCRIPT_DIR/.env" ]; then
      printf '[kickoff] %s is empty (placeholder) — first run?\n' "$SCRIPT_DIR/.env" >&2
    else
      printf '[kickoff] No .env found at %s — first run?\n' "$SCRIPT_DIR/.env" >&2
    fi
    IFS= read -r -p "[kickoff] Run --setup now to configure? [Y/n]: " _firstrun_ans <&6
    exec 6>&-
    case "${_firstrun_ans,,}" in
      ""|y|yes)
        _clk_setup
        ;;
      *)
        printf '[kickoff] Skipping setup; continuing with defaults.\n' >&2
        ;;
    esac
  fi
fi

# ===========================================================================
# 2. Load .env (export every assigned var so subprocesses inherit it)
# ===========================================================================
if [ -f "$SCRIPT_DIR/.env" ]; then
  echo "[kickoff] loading $SCRIPT_DIR/.env"
  set -a
  # shellcheck disable=SC1091
  . "$SCRIPT_DIR/.env"
  set +a
fi

# Apply git identity overrides from .env (useful inside Docker containers).
if [ -n "${CLK_GIT_NAME:-}" ]; then
  git config --global user.name "$CLK_GIT_NAME" 2>/dev/null || true
fi
if [ -n "${CLK_GIT_EMAIL:-}" ]; then
  git config --global user.email "$CLK_GIT_EMAIL" 2>/dev/null || true
fi

# ===========================================================================
# 3. Apply --arg overrides, then fill in built-in defaults
# ===========================================================================
[ -n "$_OVR_PROVIDER" ]    && CLK_PROVIDER="$_OVR_PROVIDER"
[ -n "$_OVR_MAX_ITER" ]    && CLK_MAX_ITERATIONS="$_OVR_MAX_ITER"
[ -n "$_OVR_PROJ_NAME" ]   && CLK_PROJECT_NAME="$_OVR_PROJ_NAME"
[ -n "$_OVR_NO_TUI" ]      && CLK_NO_TUI="$_OVR_NO_TUI"
[ -n "$_OVR_RUN_INSTALL" ] && CLK_RUN_INSTALL="$_OVR_RUN_INSTALL"

_apply_defaults

# ===========================================================================
# 4. Validate; if anything is missing, offer --setup then retry or exit
# ===========================================================================
_MISSING="$(_clk_missing)"
if [ -n "$_MISSING" ]; then
  printf '[kickoff] Cannot start — missing or invalid configuration:\n\n' >&2
  while IFS= read -r _line; do
    printf '  • %s\n' "$_line" >&2
  done <<< "$_MISSING"
  printf '\n' >&2

  _do_setup=false
  # Test whether /dev/tty is actually openable (it exists but may not be
  # accessible in CI or non-interactive Docker containers).
  if { exec 3<>/dev/tty; } 2>/dev/null; then
    printf '[kickoff] Run  %s --setup  to configure, or answer below.\n' \
           "'$(basename "$0")'" >&2
    IFS= read -r -p "[kickoff] Run --setup now? [y/N]: " _ans <&3
    exec 3>&-
    [ "${_ans,,}" = "y" ] && _do_setup=true
  else
    printf '[kickoff] Re-run with  %s --setup  to configure interactively.\n' \
           "'$(basename "$0")'" >&2
  fi

  if $_do_setup; then
    _clk_setup
    # Reload .env and re-apply overrides + defaults.
    [ -f "$SCRIPT_DIR/.env" ] && { set -a; . "$SCRIPT_DIR/.env"; set +a; }
    [ -n "$_OVR_PROVIDER" ]    && CLK_PROVIDER="$_OVR_PROVIDER"
    [ -n "$_OVR_MAX_ITER" ]    && CLK_MAX_ITERATIONS="$_OVR_MAX_ITER"
    [ -n "$_OVR_PROJ_NAME" ]   && CLK_PROJECT_NAME="$_OVR_PROJ_NAME"
    [ -n "$_OVR_NO_TUI" ]      && CLK_NO_TUI="$_OVR_NO_TUI"
    [ -n "$_OVR_RUN_INSTALL" ] && CLK_RUN_INSTALL="$_OVR_RUN_INSTALL"
    _apply_defaults
    _MISSING="$(_clk_missing)"
    if [ -n "$_MISSING" ]; then
      printf '[kickoff] Still missing after setup — cannot continue:\n\n' >&2
      while IFS= read -r _line; do printf '  • %s\n' "$_line" >&2; done <<< "$_MISSING"
      exit 2
    fi
  else
    exit 2
  fi
fi

# ===========================================================================
# 5. Create kickoff directory under workspace/; never touch the source tree
# ===========================================================================
TS="$(date +%Y%m%d-%H%M%S)"
WORKSPACE_DIR="$(pwd)/workspace"
KICKOFF_DIR="$WORKSPACE_DIR/kickoff-$TS"
mkdir -p "$WORKSPACE_DIR"
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
copy_if_present "$SCRIPT_DIR/clk_harness"       "$KICKOFF_DIR/.clk/harness/clk_harness"
copy_if_present "$SCRIPT_DIR/scripts"           "$KICKOFF_DIR/.clk/harness/scripts"
copy_if_present "$SCRIPT_DIR/pyproject.toml"    "$KICKOFF_DIR/.clk/harness/pyproject.toml"
copy_if_present "$SCRIPT_DIR/requirements.txt"  "$KICKOFF_DIR/.clk/harness/requirements.txt"
copy_if_present "$SCRIPT_DIR/README.md"         "$KICKOFF_DIR/.clk/harness/README.md"

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
| Max iterations   | $CLK_MAX_ITERATIONS |
| Ran installer    | $CLK_RUN_INSTALL |
| Idea             | $IDEA |

This directory is fully self-contained. Delete it to reset.
MANIFEST

# ===========================================================================
# 6. Run the harness inside the kickoff directory
# ===========================================================================
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
CLK_OPENWEBUI_ENDPOINT="${CLK_OPENWEBUI_ENDPOINT:-}" \
CLK_OPENWEBUI_API_KEY="${CLK_OPENWEBUI_API_KEY:-}" \
CLK_OPENWEBUI_MODEL="${CLK_OPENWEBUI_MODEL:-}" \
CLK_PI_MODEL="${CLK_PI_MODEL:-}" \
CLK_PI_API_KEY="${CLK_PI_API_KEY:-}" \
CLK_PI_KEY_TYPE="${CLK_PI_KEY_TYPE:-}" \
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
# subprocess — which is exactly what the user expects when they choose
# "apikey" auth: the API key alone, no local CLI dependency.
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
elif provider == "pi":
    provs.setdefault("pi", {"type": "pi", "command": "pi", "args": []})
    pi_model    = os.environ.get("CLK_PI_MODEL", "").strip()
    pi_key      = os.environ.get("CLK_PI_API_KEY", "").strip()
    pi_key_type = os.environ.get("CLK_PI_KEY_TYPE", "").strip().lower()
    if pi_model:
        provs["pi"]["model"] = pi_model
    if pi_key:
        provs["pi"]["api_key"] = pi_key
    if pi_key_type:
        provs["pi"]["key_type"] = pi_key_type
p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
PY
"$CLK" configure --set "default_provider=$CLK_PROVIDER" >/dev/null

if [ "${CLK_NO_TUI:-false}" = "true" ]; then
  # Non-interactive pipeline. Useful for CI / smoke tests and Docker without -it.
  echo "[kickoff] clk idea"
  "$CLK" idea "$IDEA" --title "$CLK_PROJECT_NAME"
  echo "[kickoff] clk plan"
  "$CLK" plan || echo "[kickoff] plan reported failures (continuing)"
  echo "[kickoff] clk run"
  "$CLK" run || echo "[kickoff] run reported failures (continuing)"
  echo "[kickoff] clk loop --max-iterations $CLK_MAX_ITERATIONS"
  "$CLK" loop --max-iterations "$CLK_MAX_ITERATIONS"
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
echo "[kickoff] workspace:   $WORKSPACE_DIR"
echo "[kickoff] reset:       rm -rf \"$KICKOFF_DIR\""
