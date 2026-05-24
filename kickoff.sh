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

# Shared helpers — atomic .env writes and tool install/configure registry.
# Sourced once so _clk_setup, _clk_setup_github, and the TUI all behave
# the same way.
# shellcheck source=scripts/lib_env.sh
. "$SCRIPT_DIR/scripts/lib_env.sh"
# shellcheck source=scripts/install_tool.sh
. "$SCRIPT_DIR/scripts/install_tool.sh"

# ===========================================================================
# Setup wizard — invoked by --setup or offered when config is incomplete.
# Reads from /dev/tty so it works inside `docker run -it` even when stdin
# is not a terminal.
#
# Design notes — see /root/.claude/plans/recommend-ways-to-make-imperative-wreath.md
#   * Explain-then-ask: every prompt is preceded by a short block telling
#     the user what the value does and what reasonable choices are.
#   * Atomic writes: each answer is persisted to .env immediately through
#     env_set, so a Ctrl-C mid-wizard does not leave the file half-written
#     and the next run can resume.
#   * Per-step resume: `.clk/.setup-progress` records the last completed
#     step name; the next run offers to skip ahead.
#   * Tool autopilot: after the provider is chosen, the wizard runs
#     `check_tool` and, on miss, asks before invoking `install_tool`.
#     Then `configure_tool` walks the user through auth, upstream route
#     (for pi), and model picking (including `ollama pull`).
#   * Always-confirm: every install/push/destructive action prompts y/N
#     every time. No "remember the answer" shortcut.
# ===========================================================================
_clk_setup() {
  { exec 3</dev/tty; } 2>/dev/null || exec 3<&0
  { exec 4>/dev/tty; } 2>/dev/null || exec 4>&2

  _sv_read() {
    local prompt="$1" default="$2" v
    if [ -n "$default" ]; then
      printf '%s [%s]: ' "$prompt" "$default" >&4
    else
      printf '%s: ' "$prompt" >&4
    fi
    IFS= read -r v <&3 || v=""
    printf '%s' "${v:-$default}"
  }

  _sv_secret() {
    local prompt="$1" v
    printf '%s (leave blank to keep): ' "$prompt" >&4
    stty -echo </dev/tty 2>/dev/null || true
    IFS= read -r v <&3 || v=""
    stty echo  </dev/tty 2>/dev/null || true
    printf '\n' >&4
    printf '%s' "$v"
  }

  _sv_explain() {
    # One blank line above, then the explanation, then a separator. The
    # tone mirrors install_local.sh: tell the user what you're about to
    # do and why before doing it.
    printf '\n%s\n' "$1" >&4
  }

  _sv_confirm() {
    local prompt="$1" default="${2:-N}" v hint
    case "${default^^}" in
      Y|YES) hint="Y/n" ;;
      *)     hint="y/N" ;;
    esac
    printf '%s [%s]: ' "$prompt" "$hint" >&4
    IFS= read -r v <&3 || v=""
    v="${v:-$default}"
    case "${v,,}" in y|yes) return 0 ;; *) return 1 ;; esac
  }

  local env_file="${CLK_ENV_FILE:-$SCRIPT_DIR/.env}"
  export CLK_ENV_FILE="$env_file"
  local progress_file="$SCRIPT_DIR/.clk/.setup-progress"
  mkdir -p "$(dirname "$progress_file")"

  # Seed defaults from .env.example first, then let an existing .env override.
  if [ -f "$SCRIPT_DIR/.env.example" ]; then
    set -a; . "$SCRIPT_DIR/.env.example"; set +a
  fi
  if [ -s "$env_file" ]; then
    set -a; . "$env_file"; set +a
    printf '[setup] loaded existing values from %s\n' "$env_file" >&4
  else
    printf '[setup] %s is empty or missing — using .env.example defaults\n' "$env_file" >&4
  fi

  # Per-step resume: each completed step writes its name here. On the
  # next run, we look at the last name and offer to skip ahead.
  local last_step=""
  if [ -s "$progress_file" ]; then
    last_step="$(tail -n1 "$progress_file" 2>/dev/null || true)"
  fi
  local skip_until=""
  if [ -n "$last_step" ]; then
    if _sv_confirm "[setup] Resume from after step '$last_step'?" "Y"; then
      skip_until="$last_step"
    else
      : > "$progress_file"
    fi
  fi

  printf '\n=== CLK Setup Wizard ===\n' >&4
  printf 'Press Enter to keep the value shown in [brackets].\n' >&4
  printf 'Every install, push, and destructive action is confirmed y/N first.\n' >&4

  # Mark a step as complete. Used both for the progress file and to
  # short-circuit when resuming.
  _mark_step() { printf '%s\n' "$1" >> "$progress_file"; }
  _should_run_step() {
    # Returns 0 (run) unless we're skipping past this step.
    [ -z "$skip_until" ] && return 0
    if [ "$1" = "$skip_until" ]; then
      # Stop skipping after this match — the next step runs normally.
      skip_until=""
      return 1
    fi
    return 1
  }

  local provider max_iter proj_name run_install no_tui auth_mode
  local git_name git_email new tg_setup tg_skip

  # --- provider --------------------------------------------------------
  if _should_run_step "provider"; then
    _sv_explain "=== Provider ===
The provider is the AI that actually writes your code each cycle.

  shell      no AI — useful for smoke tests and the /tutorial walkthrough
  claude     Anthropic Claude Code CLI (best at writing code, supports tools)
  codex      OpenAI Codex CLI
  gemini     Google Gemini CLI
  pi         Pi terminal harness — routes through OpenRouter/Anthropic/OpenAI/Google
  ollama     local LLM via the Ollama daemon (no external API, free)
  openwebui  OpenWebUI server (self-hosted, OpenAI-compatible)"
    provider="$(_sv_read "Provider" "${CLK_PROVIDER:-shell}")"
    env_set "$env_file" CLK_PROVIDER "$provider"
    _mark_step provider
  else
    provider="${CLK_PROVIDER:-shell}"
  fi

  # --- max iterations + project name + flags ---------------------------
  if _should_run_step "loop_settings"; then
    _sv_explain "=== Loop settings ===
\`max iterations\` caps how many refinement cycles the Ralph and
autoresearch loops can run. \`project name\` becomes the title of the
captured idea and (optionally) the GitHub repo name. The \`run install\`
flag triggers .clk/harness/scripts/install_local.sh inside each kickoff
dir so providers like pi can find PyYAML and other deps. \`no TUI\`
switches to a non-interactive pipeline — handy for CI."
    max_iter="$(_sv_read    "Max loop iterations" "${CLK_MAX_ITERATIONS:-10}")"
    proj_name="$(_sv_read   "Project name"        "${CLK_PROJECT_NAME:-clk-app}")"
    run_install="$(_sv_read "Run install_local.sh in each kickoff (true|false)" "${CLK_RUN_INSTALL:-false}")"
    no_tui="$(_sv_read      "Skip TUI / non-interactive (true|false)" "${CLK_NO_TUI:-false}")"
    env_set "$env_file" CLK_MAX_ITERATIONS "$max_iter"
    env_set "$env_file" CLK_PROJECT_NAME   "$proj_name"
    env_set "$env_file" CLK_RUN_INSTALL    "$run_install"
    env_set "$env_file" CLK_NO_TUI         "$no_tui"
    _mark_step loop_settings
  else
    max_iter="${CLK_MAX_ITERATIONS:-10}"
    proj_name="${CLK_PROJECT_NAME:-clk-app}"
    run_install="${CLK_RUN_INSTALL:-false}"
    no_tui="${CLK_NO_TUI:-false}"
  fi

  # --- auth mode (CLI providers) ---------------------------------------
  auth_mode="${CLK_AUTH_MODE:-cli}"
  case "$provider" in
    claude|codex|gemini)
      if _should_run_step "auth_mode"; then
        _sv_explain "=== Auth mode ($provider) ===
'cli'    — use your existing local CLI login (run \`$provider login\` once;
           best when you already use $provider day-to-day).
'apikey' — call the provider's HTTP API directly using an API key.
           No CLI dependency, but you must paste a key below."
        auth_mode="$(_sv_read "Auth mode" "$auth_mode")"
        env_set "$env_file" CLK_AUTH_MODE "$auth_mode"
        _mark_step auth_mode
      fi
      ;;
  esac

  # --- install + configure the chosen tool -----------------------------
  if _should_run_step "tool_setup" && [ "$provider" != "shell" ]; then
    _sv_explain "=== Tool detection ($provider) ===
Checking whether \`$provider\` is installed and reachable. If it
isn't, the wizard will suggest an install command and ask before
running it. After the tool is available, we'll walk through
first-use config (auth -> route -> model -> verify)."
    if check_tool "$provider"; then
      printf '[setup] %s is available on this machine.\n' "$provider" >&4
    else
      install_tool "$provider" --prompt || printf '[setup] %s install was skipped or failed; continuing.\n' "$provider" >&4
    fi
    if check_tool "$provider"; then
      if tool_configured "$provider" && ! _sv_confirm "Re-run first-use config for $provider?" "N"; then
        printf '[setup] %s already configured (per .clk/state/configured-tools.json).\n' "$provider" >&4
      else
        configure_tool "$provider" || printf '[setup] %s configure step exited non-zero; continuing.\n' "$provider" >&4
      fi
    else
      printf '[setup] %s is still unavailable — provider calls will fail until you install it.\n' "$provider" >&4
    fi
    _mark_step tool_setup
    # Reload .env so values just written by configure_tool become visible
    # to the rest of the wizard.
    [ -s "$env_file" ] && { set -a; . "$env_file"; set +a; }
  elif _should_run_step "tool_setup"; then
    _mark_step tool_setup
  fi

  # --- docker host fallback for local LLM endpoints --------------------
  # Catches the common Docker-in-container case where CLK_OLLAMA_ENDPOINT
  # or CLK_OPENWEBUI_ENDPOINT default to http://localhost:... but the
  # actual server is on the host. We probe both the configured URL and
  # the host.docker.internal equivalent; if only the latter answers,
  # we offer to rewrite .env — even if the user picked a different
  # provider as active (the TUI's health check surfaces all of them).
  if _should_run_step "docker_host_fallback"; then
    _sv_explain "=== Local LLM endpoint check ===
If you have ollama or OpenWebUI running on the host but CLK is in a
container, 'localhost' won't reach them. We'll probe each configured
endpoint and, when only host.docker.internal works, offer to switch."
    _it_offer_docker_host_fallback "Ollama" CLK_OLLAMA_ENDPOINT \
      "${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}" || true
    _it_offer_docker_host_fallback "OpenWebUI" CLK_OPENWEBUI_ENDPOINT \
      "${CLK_OPENWEBUI_ENDPOINT:-http://localhost:8080}" || true
    _mark_step docker_host_fallback
    [ -s "$env_file" ] && { set -a; . "$env_file"; set +a; }
  fi

  # --- telegram --------------------------------------------------------
  if _should_run_step "telegram"; then
    _sv_explain "=== Telegram bot (optional) ===
If enabled, you can drive CLK from your phone: send the bot an idea,
get progress updates back, /stop or /abort remotely. The dedicated
wizard at scripts/telegram_setup_wizard.sh walks through BotFather
token creation and discovers your numeric user ID so we can allowlist
only you."
    local default_tg="N"
    [ "${CLK_TELEGRAM_ENABLED:-false}" = "true" ] && default_tg="y"
    tg_setup="$(_sv_read "Set up Telegram bot now? (y/N)" "$default_tg")"
    if [ "${tg_setup,,}" = "y" ]; then
      tg_skip="false"
    else
      tg_skip="true"
      printf '[setup] Skipping Telegram. CLK_TELEGRAM_SKIP=true will be written to .env.\n' >&4
    fi
    env_set "$env_file" CLK_TELEGRAM_SKIP "$tg_skip"
    _mark_step telegram
  fi

  # --- GitHub ----------------------------------------------------------
  if _should_run_step "github"; then
    _clk_setup_github "$env_file" "$proj_name"
    _mark_step github
  fi

  # --- git identity ----------------------------------------------------
  if _should_run_step "git_identity"; then
    _sv_explain "=== Git identity (used in kickoff commits) ===
Each kickoff workspace is its own git repo and CLK auto-commits after
every successful agent run. The author/committer comes from your
global git config unless you set CLK_GIT_NAME / CLK_GIT_EMAIL here
(useful inside containers where the global config doesn't propagate)."
    local cur_name cur_email
    cur_name="$(git config --global user.name  2>/dev/null || true)"
    cur_email="$(git config --global user.email 2>/dev/null || true)"
    printf '  Current global git name:  %s\n' "${cur_name:-<not set>}"  >&4
    printf '  Current global git email: %s\n' "${cur_email:-<not set>}" >&4
    git_name="$(_sv_read  "Git user.name  (blank = keep current)" "${CLK_GIT_NAME:-}")"
    git_email="$(_sv_read "Git user.email (blank = keep current)" "${CLK_GIT_EMAIL:-}")"
    env_set "$env_file" CLK_GIT_NAME  "$git_name"
    env_set "$env_file" CLK_GIT_EMAIL "$git_email"
    _mark_step git_identity
  fi

  exec 3<&- 2>/dev/null || true

  printf '\n[setup] saved %s\n' "$env_file" >&4
  printf '[setup] previous values are in %s.bak\n' "$env_file" >&4

  if [ "${tg_setup:-N}" = "y" ] || [ "${tg_setup:-N}" = "Y" ]; then
    printf '\n[setup] launching Telegram wizard...\n' >&4
    CLK_ENV_FILE="$env_file" "$SCRIPT_DIR/scripts/telegram_setup_wizard.sh" >&4 2>&4 || \
      printf '[setup] telegram wizard exited non-zero; continuing\n' >&4
  fi

  # Wizard finished cleanly — clear progress so a future --setup starts
  # at the top instead of asking to resume.
  rm -f "$progress_file"

  exec 4>&- 2>/dev/null || true
}

# ===========================================================================
# GitHub connection block — invoked by _clk_setup. Adds a `origin` remote
# to the local kickoff repo, hardens the .gitignore so secrets can't be
# pushed, and installs a pre-push hook that greps for obvious key
# patterns. The caller is responsible for git_init; we only configure
# the remote. The kickoff dir already gets its own `git init` later in
# this script, so the wizard records the choice in .env and the run-time
# kickoff sequence applies it.
# ===========================================================================
_clk_setup_github() {
  local env_file="$1" proj_name="$2"
  _sv_explain "=== GitHub (optional) ===
Each kickoff workspace is already a local git repo. You can optionally
connect it to a GitHub remote so:
  - every agent commit is checkpointed off your machine
  - you (or another machine) can resume the work later by cloning
  - friends/teammates can review the run

  skip       no GitHub — local commits only (default)
  existing   connect to a repo you already own (paste URL)
  create     create a brand new private repo under your account

The wizard will write a hardened .gitignore (blocking .env, .env.bak,
SSH keys, etc.) and install a pre-push hook that aborts when an
obvious API key pattern appears in the diff."

  local choice
  choice="$(_sv_read "Connect to GitHub?" "${CLK_GITHUB_MODE:-skip}")"
  case "$choice" in
    skip|"")
      env_set "$env_file" CLK_GITHUB_MODE skip
      env_set "$env_file" CLK_GITHUB_REMOTE ""
      env_set "$env_file" CLK_GITHUB_PUSH_ON_COMMIT "false"
      printf '[setup] GitHub disabled.\n' >&4
      return 0
      ;;
    existing)
      local url
      url="$(_sv_read "Existing repo (https://github.com/OWNER/REPO or git@github.com:OWNER/REPO.git)" "${CLK_GITHUB_REMOTE:-}")"
      if [ -z "$url" ]; then
        printf '[setup] no URL provided; skipping GitHub.\n' >&4
        env_set "$env_file" CLK_GITHUB_MODE skip
        return 0
      fi
      env_set "$env_file" CLK_GITHUB_MODE existing
      env_set "$env_file" CLK_GITHUB_REMOTE "$url"
      ;;
    create)
      if ! _it_has gh; then
        printf '[setup] gh CLI is required to create a repo from here.\n' >&4
        if ! install_tool gh --prompt; then
          printf '[setup] gh unavailable; cannot create. Falling back to "existing" — paste a URL.\n' >&4
          local url
          url="$(_sv_read "Existing repo URL" "")"
          if [ -n "$url" ]; then
            env_set "$env_file" CLK_GITHUB_MODE existing
            env_set "$env_file" CLK_GITHUB_REMOTE "$url"
          else
            env_set "$env_file" CLK_GITHUB_MODE skip
          fi
          return 0
        fi
      fi
      if ! gh auth status >/dev/null 2>&1; then
        printf '[setup] gh is installed but not authenticated.\n' >&4
        if _sv_confirm "Run \`gh auth login\` now?" "Y"; then
          _it_login_shell gh
        fi
      fi
      local owner_repo default_or
      default_or="$(gh api user --jq .login 2>/dev/null || echo "$USER")"
      owner_repo="$(_sv_read "owner/repo to create" "${default_or}/${proj_name}-kickoff")"
      env_set "$env_file" CLK_GITHUB_MODE create
      env_set "$env_file" CLK_GITHUB_REMOTE "$owner_repo"
      printf '[setup] GitHub repo "%s" will be created (private) on the first kickoff push.\n' "$owner_repo" >&4
      ;;
    *)
      printf '[setup] unknown GitHub choice "%s"; skipping.\n' "$choice" >&4
      env_set "$env_file" CLK_GITHUB_MODE skip
      return 0
      ;;
  esac

  if _sv_confirm "Auto-push to GitHub after every CLK commit?" "Y"; then
    env_set "$env_file" CLK_GITHUB_PUSH_ON_COMMIT "true"
  else
    env_set "$env_file" CLK_GITHUB_PUSH_ON_COMMIT "false"
  fi
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

RESTORE_MODE=false
LIST_MODE=false
CLEAN_OLDER_THAN=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --setup) SETUP_MODE=true; shift ;;
    --restore) RESTORE_MODE=true; shift ;;
    --list) LIST_MODE=true; shift ;;
    --clean=*) CLEAN_OLDER_THAN="${1#*=}"; shift ;;
    --clean)
      [[ $# -lt 2 || "$2" == -* ]] && { printf '[kickoff] --clean requires a value like 7d\n' >&2; exit 2; }
      CLEAN_OLDER_THAN="$2"; shift 2 ;;
    -h|--help)
      cat <<USAGE
usage: $(basename "$0") [OPTIONS] ["<idea or problem statement>"]

Options:
  --setup                  Interactive wizard to write or update .env
  --restore                Restore .env from .env.bak (undo last --setup)
  --list                   List past kickoff dirs under workspace/
  --clean DURATION         Delete kickoff dirs older than DURATION (e.g. 7d, 30d)
                           Always asks y/N before deleting. --clean alone shows --dry-run.
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

if $RESTORE_MODE; then
  env_restore "$SCRIPT_DIR/.env" && \
    printf '[kickoff] .env restored from .env.bak\n' || \
    { printf '[kickoff] no .env.bak to restore\n' >&2; exit 1; }
  exit 0
fi

if $LIST_MODE; then
  ws_dir="$(pwd)/workspace"
  if [ ! -d "$ws_dir" ]; then
    printf '[kickoff] no workspace/ dir yet — nothing to list.\n'
    exit 0
  fi
  printf '%-32s %-19s %s\n' "kickoff dir" "last activity" "idea"
  for d in "$ws_dir"/kickoff-*; do
    [ -d "$d" ] || continue
    last="$(stat -c "%y" "$d" 2>/dev/null | cut -d. -f1)"
    idea=""
    if [ -f "$d/.clk/state/idea.json" ]; then
      idea="$(python3 -c "import json,sys;print((json.load(open(sys.argv[1])).get('title') or '')[:60])" "$d/.clk/state/idea.json" 2>/dev/null || true)"
    fi
    printf '%-32s %-19s %s\n' "$(basename "$d")" "${last:-?}" "$idea"
  done
  exit 0
fi

if [ -n "$CLEAN_OLDER_THAN" ]; then
  ws_dir="$(pwd)/workspace"
  if [ ! -d "$ws_dir" ]; then
    printf '[kickoff] no workspace/ dir; nothing to clean.\n'
    exit 0
  fi
  # Convert e.g. 7d -> 7 (days), 30m -> 30 (minutes); -mtime expects days,
  # -mmin expects minutes. Anything else aborts.
  unit="${CLEAN_OLDER_THAN: -1}"
  qty="${CLEAN_OLDER_THAN%?}"
  if ! [[ "$qty" =~ ^[0-9]+$ ]]; then
    printf '[kickoff] --clean expects something like 7d or 30m (got %s)\n' "$CLEAN_OLDER_THAN" >&2
    exit 2
  fi
  case "$unit" in
    d) find_flag=(-mtime "+$qty") ;;
    m) find_flag=(-mmin  "+$qty") ;;
    *) printf '[kickoff] --clean unit must be d (days) or m (minutes)\n' >&2; exit 2 ;;
  esac
  mapfile -t targets < <(find "$ws_dir" -mindepth 1 -maxdepth 1 -type d -name "kickoff-*" "${find_flag[@]}" 2>/dev/null)
  if [ "${#targets[@]}" -eq 0 ]; then
    printf '[kickoff] no kickoff dirs older than %s.\n' "$CLEAN_OLDER_THAN"
    exit 0
  fi
  printf '[kickoff] would remove %d kickoff dirs older than %s:\n' "${#targets[@]}" "$CLEAN_OLDER_THAN"
  for t in "${targets[@]}"; do printf '  - %s\n' "$t"; done
  if { exec 7<>/dev/tty; } 2>/dev/null; then
    IFS= read -r -p "Delete these? [y/N]: " _ans <&7
    exec 7>&-
    if [ "${_ans,,}" = "y" ] || [ "${_ans,,}" = "yes" ]; then
      for t in "${targets[@]}"; do rm -rf -- "$t"; printf '[kickoff] removed %s\n' "$t"; done
    else
      printf '[kickoff] nothing deleted.\n'
    fi
  else
    printf '[kickoff] non-interactive; refusing to delete without confirmation.\n' >&2
    printf '[kickoff] re-run from a terminal to confirm.\n' >&2
    exit 2
  fi
  exit 0
fi

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
# Secrets-by-pattern are blocked too so a pushed remote can't leak them
# even if an agent accidentally writes one to a file.
cat > .gitignore <<'GITIGNORE'
# All harness state lives under .clk/ — ignore it entirely.
.clk/
# Secrets
/.env
/.env.example
/.env.bak
/.env.partial
.env.local
*.pem
*.key
*_id_rsa*
/secrets/
/.secrets/
# Editor / OS junk
__pycache__/
*.pyc
.DS_Store
.idea/
.vscode/
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

  # Install a pre-push hook that aborts on obvious secret patterns so a
  # leaked .env or key file can't reach GitHub. Pure bash so it ships
  # with the kickoff bundle. The user can bypass with `git push --no-verify`
  # when they know better.
  mkdir -p .git/hooks
  cat > .git/hooks/pre-push <<'HOOK'
#!/usr/bin/env bash
# CLK pre-push secret scan. Bypass with `git push --no-verify` when sure.
set -eo pipefail
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
  range="$local_sha"
  if [ "$remote_sha" != "0000000000000000000000000000000000000000" ]; then
    range="$remote_sha..$local_sha"
  fi
  hits=$(git log -p "$range" 2>/dev/null | grep -E \
    -e 'ANTHROPIC_API_KEY=[A-Za-z0-9_\-]+' \
    -e 'OPENAI_API_KEY=[A-Za-z0-9_\-]+' \
    -e 'OPENROUTER_API_KEY=[A-Za-z0-9_\-]+' \
    -e 'GEMINI_API_KEY=[A-Za-z0-9_\-]+' \
    -e 'GOOGLE_API_KEY=[A-Za-z0-9_\-]+' \
    -e 'sk-[A-Za-z0-9]{20,}' \
    -e 'xoxb-[A-Za-z0-9-]{20,}' \
    -e 'BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY' \
    || true)
  if [ -n "$hits" ]; then
    echo "[pre-push] aborting — possible secret(s) in $range:" >&2
    echo "$hits" | head -n 5 >&2
    echo "" >&2
    echo "To override: git push --no-verify  (only when you're sure)." >&2
    exit 1
  fi
done
HOOK
  chmod +x .git/hooks/pre-push

  # Connect a GitHub remote if the wizard recorded one.
  if [ -n "${CLK_GITHUB_REMOTE:-}" ] && [ "${CLK_GITHUB_MODE:-skip}" != "skip" ]; then
    case "$CLK_GITHUB_MODE" in
      existing)
        if ! git remote get-url origin >/dev/null 2>&1; then
          echo "[kickoff] linking existing GitHub remote: $CLK_GITHUB_REMOTE"
          git remote add origin "$CLK_GITHUB_REMOTE"
        fi
        ;;
      create)
        if command -v gh >/dev/null 2>&1 && gh auth status >/dev/null 2>&1; then
          if ! git remote get-url origin >/dev/null 2>&1; then
            echo "[kickoff] creating GitHub repo: $CLK_GITHUB_REMOTE (private)"
            gh repo create "$CLK_GITHUB_REMOTE" --private --source=. --remote=origin \
              || echo "[kickoff] gh repo create failed (continuing without remote)"
          fi
        else
          echo "[kickoff] CLK_GITHUB_MODE=create but gh CLI is not authenticated; skipping remote."
        fi
        ;;
    esac
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
