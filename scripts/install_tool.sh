#!/usr/bin/env bash
# scripts/install_tool.sh — single source of truth for "install or
# configure provider CLI X."
#
# Used by:
#   * kickoff.sh --setup (after the user picks a provider)
#   * the TUI's /install and /configure commands
#
# Public API (when sourced):
#   check_tool NAME              # 0 if present/usable, 1 if missing
#   install_tool NAME [--prompt|--auto|--print-only]
#                                # 0 installed (or already present),
#                                # 1 user declined, 2 install failed
#   configure_tool NAME          # runs the first-use config flow:
#                                # auth -> upstream route -> model -> verify
#   mark_tool_configured NAME PROVIDER MODEL
#   tool_configured NAME         # 0 if already in configured-tools.json
#
# When invoked directly:
#   scripts/install_tool.sh check NAME
#   scripts/install_tool.sh install NAME [--auto|--prompt|--print-only]
#   scripts/install_tool.sh configure NAME

set -euo pipefail

_IT_SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
_IT_PROJECT_DIR="${CLK_PROJECT_ROOT:-$(cd -- "$_IT_SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)}"
_IT_ENV_FILE="${CLK_ENV_FILE:-$_IT_PROJECT_DIR/.env}"
_IT_STATE_DIR="$_IT_PROJECT_DIR/.clk/state"
_IT_CONFIGURED_FILE="$_IT_STATE_DIR/configured-tools.json"

# Source the shared env helpers.
# shellcheck source=scripts/lib_env.sh
. "$_IT_SCRIPT_DIR/lib_env.sh"

# ---------------------------------------------------------------------------
# I/O — same /dev/tty pattern as kickoff's wizard so we work inside
# `docker run -it` even when stdin is piped.
# ---------------------------------------------------------------------------
_it_open_tty() {
  if [ -n "${_IT_TTY_READY:-}" ]; then return 0; fi
  { exec 3</dev/tty; } 2>/dev/null || exec 3<&0
  { exec 4>/dev/tty; } 2>/dev/null || exec 4>&2
  _IT_TTY_READY=1
}

_it_say() { _it_open_tty; printf '%s\n' "$*" >&4; }
_it_warn() { _it_open_tty; printf '[install_tool] %s\n' "$*" >&4; }

_it_read() {
  local prompt="$1" default="${2:-}" v
  _it_open_tty
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" >&4
  else
    printf '%s: ' "$prompt" >&4
  fi
  IFS= read -r v <&3 || v=""
  printf '%s' "${v:-$default}"
}

_it_secret() {
  local prompt="$1" v
  _it_open_tty
  printf '%s: ' "$prompt" >&4
  stty -echo </dev/tty 2>/dev/null || true
  IFS= read -r v <&3 || v=""
  stty echo  </dev/tty 2>/dev/null || true
  printf '\n' >&4
  printf '%s' "$v"
}

_it_confirm() {
  local prompt="$1" default="${2:-N}" v
  v="$(_it_read "$prompt [$default]" "$default")"
  case "${v,,}" in
    y|yes) return 0 ;;
    *) return 1 ;;
  esac
}

# ---------------------------------------------------------------------------
# Platform detection
# ---------------------------------------------------------------------------
_it_platform() {
  case "$(uname -s)" in
    Darwin*) echo "macos" ;;
    Linux*)  echo "linux" ;;
    MINGW*|MSYS*|CYGWIN*) echo "windows" ;;
    *)       echo "unknown" ;;
  esac
}

_it_has() { command -v "$1" >/dev/null 2>&1; }

# ---------------------------------------------------------------------------
# Installer registry — one function per supported tool. Each one echoes
# the canonical install command (so the caller can print it before
# running), and the matching `_install_run_<tool>` actually executes it.
# Keeping print and run separate means we can always show the command
# first and ask before we run anything.
# ---------------------------------------------------------------------------

_install_cmd_claude()  { echo "npm install -g @anthropic-ai/claude-code"; }
_install_cmd_codex()   { echo "npm install -g @openai/codex"; }
_install_cmd_gemini()  { echo "npm install -g @google/gemini-cli"; }
_install_cmd_pi()      { echo "npm install -g @pi-dev/cli"; }
_install_cmd_ollama() {
  case "$(_it_platform)" in
    macos)   _it_has brew && echo "brew install ollama" || echo "curl -fsSL https://ollama.ai/install.sh | sh" ;;
    linux)   echo "curl -fsSL https://ollama.ai/install.sh | sh" ;;
    *)       echo "(see https://ollama.com/download)" ;;
  esac
}
_install_cmd_openwebui() {
  echo "(install via Docker — see https://docs.openwebui.com)"
}
_install_cmd_tmux() {
  case "$(_it_platform)" in
    macos) _it_has brew && echo "brew install tmux" || echo "(see https://github.com/tmux/tmux)" ;;
    linux) _it_has apt && echo "sudo apt install -y tmux" || (_it_has dnf && echo "sudo dnf install -y tmux") || echo "(see https://github.com/tmux/tmux)" ;;
    *)     echo "(see https://github.com/tmux/tmux)" ;;
  esac
}
_install_cmd_curl() {
  case "$(_it_platform)" in
    macos) echo "(curl ships with macOS — already installed)" ;;
    linux) _it_has apt && echo "sudo apt install -y curl" || (_it_has dnf && echo "sudo dnf install -y curl") || echo "(install curl via your package manager)" ;;
    *)     echo "(install curl via your package manager)" ;;
  esac
}
_install_cmd_gh() {
  case "$(_it_platform)" in
    macos) _it_has brew && echo "brew install gh" || echo "(see https://cli.github.com)" ;;
    linux) _it_has apt && echo "sudo apt install -y gh" || (_it_has dnf && echo "sudo dnf install -y gh") || echo "(see https://cli.github.com)" ;;
    *)     echo "(see https://cli.github.com)" ;;
  esac
}

_install_run_default() {
  local cmd="$1"
  case "$cmd" in
    "("*) return 2 ;;  # placeholder text, can't run automatically
  esac
  # We deliberately use bash -c so pipelines (curl|sh) work as written.
  bash -c "$cmd"
}

# ---------------------------------------------------------------------------
# check_tool NAME — return 0 if usable, 1 if missing.
#
# For CLI tools this is just `command -v`; for HTTP services (ollama,
# openwebui) we probe the endpoint via the existing Python provider so
# the answer matches what the harness sees.
# ---------------------------------------------------------------------------
check_tool() {
  local name="$1"
  case "$name" in
    shell) return 0 ;;
    claude|codex|gemini|pi|tmux|curl|gh|npm)
      _it_has "$name" ;;
    ollama)
      # ollama is two things: a CLI and an HTTP server. We need at least
      # one of them reachable for the provider to work.
      if _it_has ollama; then return 0; fi
      local endpoint="${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}"
      local host port
      host="$(echo "$endpoint" | sed -E 's|^https?://||; s|/.*||; s|:.*||')"
      port="$(echo "$endpoint" | sed -nE 's|^https?://[^:/]+:([0-9]+).*|\1|p')"
      port="${port:-11434}"
      (echo > "/dev/tcp/$host/$port") 2>/dev/null
      ;;
    openwebui)
      local endpoint="${CLK_OPENWEBUI_ENDPOINT:-http://localhost:8080}"
      local host port
      host="$(echo "$endpoint" | sed -E 's|^https?://||; s|/.*||; s|:.*||')"
      port="$(echo "$endpoint" | sed -nE 's|^https?://[^:/]+:([0-9]+).*|\1|p')"
      port="${port:-8080}"
      (echo > "/dev/tcp/$host/$port") 2>/dev/null
      ;;
    *)
      _it_has "$name" ;;
  esac
}

# ---------------------------------------------------------------------------
# install_tool NAME [--prompt|--auto|--print-only]
#
# Returns: 0 installed (or already present), 1 user declined, 2 failed.
# ---------------------------------------------------------------------------
install_tool() {
  local name="$1"; shift || true
  local mode="--prompt"
  [ $# -gt 0 ] && mode="$1"

  if check_tool "$name"; then
    _it_say "[install_tool] $name already available — skipping install."
    return 0
  fi

  # Resolve the install command for this tool.
  local fn="_install_cmd_$name"
  if ! declare -F "$fn" >/dev/null; then
    _it_warn "no install recipe for '$name'."
    return 2
  fi
  local cmd
  cmd="$($fn)"

  _it_say ""
  _it_say "[install_tool] $name is not installed yet."
  _it_say "[install_tool]   suggested command: $cmd"

  case "$mode" in
    --print-only)
      _it_say "[install_tool]   (run that yourself, then re-run this wizard.)"
      return 1
      ;;
    --auto) ;;
    --prompt|*)
      if ! _it_confirm "Run this install command now?" "Y"; then
        _it_say "[install_tool] skipped — install $name manually when ready."
        return 1
      fi
      ;;
  esac

  _it_say "[install_tool] running: $cmd"
  if _install_run_default "$cmd"; then
    if check_tool "$name"; then
      _it_say "[install_tool] $name installed."
      return 0
    fi
    _it_warn "$name install command returned 0 but the tool still isn't on PATH."
    _it_warn "you may need a new shell, or to add npm's global bin to PATH."
    return 2
  fi
  _it_warn "$name install failed; see the output above."
  return 2
}

# ---------------------------------------------------------------------------
# tool_configured NAME — 0 if already in configured-tools.json
# mark_tool_configured NAME PROVIDER MODEL — record a successful config
# ---------------------------------------------------------------------------
tool_configured() {
  local name="$1"
  [ -f "$_IT_CONFIGURED_FILE" ] || return 1
  python3 - "$_IT_CONFIGURED_FILE" "$name" <<'PY' 2>/dev/null
import json, sys
path, name = sys.argv[1], sys.argv[2]
try:
    data = json.load(open(path))
except Exception:
    sys.exit(1)
sys.exit(0 if name in (data.get("tools") or {}) else 1)
PY
}

mark_tool_configured() {
  local name="$1" provider="${2:-}" model="${3:-}"
  mkdir -p "$_IT_STATE_DIR"
  python3 - "$_IT_CONFIGURED_FILE" "$name" "$provider" "$model" <<'PY'
import json, os, sys, time
path, name, prov, model = sys.argv[1:5]
try:
    data = json.load(open(path))
except Exception:
    data = {}
tools = data.setdefault("tools", {})
tools[name] = {
    "provider": prov,
    "model": model,
    "configured_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
}
tmp = path + ".tmp"
with open(tmp, "w") as fh:
    json.dump(data, fh, indent=2, sort_keys=True)
    fh.write("\n")
os.replace(tmp, path)
PY
}

# ---------------------------------------------------------------------------
# configure_tool NAME — first-use config flow.
#
# Each tool has the same four-step shape:
#   (a) auth         — login / API key
#   (b) upstream     — for routers like pi, which LLM provider to use
#   (c) model        — pick (or pull) the model
#   (d) verify       — tiny ping to confirm it works
# ---------------------------------------------------------------------------

# Shared: helper to drop into an interactive shell for `<tool> login`.
_it_login_shell() {
  local tool="$1"
  _it_say ""
  _it_say "[configure $tool] Opening a sub-shell. Run \`$tool login\`, then type 'exit'."
  PS1="[$tool-setup]\$ " "${SHELL:-bash}" -i </dev/tty >&4 2>&4 || true
  _it_say "[configure $tool] returned from sub-shell."
}

_configure_claude() {
  local auth_mode="${CLK_AUTH_MODE:-cli}"
  if [ "$auth_mode" = "apikey" ]; then
    local key="${ANTHROPIC_API_KEY:-}"
    if [ -z "$key" ]; then
      key="$(_it_secret "Paste your ANTHROPIC_API_KEY (input hidden)")"
      [ -n "$key" ] && env_set "$_IT_ENV_FILE" ANTHROPIC_API_KEY "$key"
    fi
  else
    if _it_confirm "Run \`claude login\` now? (opens a sub-shell)" "Y"; then
      _it_login_shell claude
    fi
  fi
  local model
  model="$(_it_read "Claude model" "${CLK_CLAUDE_MODEL:-claude-sonnet-4-5}")"
  env_set "$_IT_ENV_FILE" CLK_CLAUDE_MODEL "$model"
  mark_tool_configured claude claude "$model"
  _it_say "[configure claude] saved model=$model"
}

_configure_codex() {
  local auth_mode="${CLK_AUTH_MODE:-cli}"
  if [ "$auth_mode" = "apikey" ]; then
    local key="${OPENAI_API_KEY:-}"
    if [ -z "$key" ]; then
      key="$(_it_secret "Paste your OPENAI_API_KEY (input hidden)")"
      [ -n "$key" ] && env_set "$_IT_ENV_FILE" OPENAI_API_KEY "$key"
    fi
  else
    if _it_confirm "Run \`codex login\` now? (opens a sub-shell)" "Y"; then
      _it_login_shell codex
    fi
  fi
  local model
  model="$(_it_read "Codex model" "${CLK_CODEX_MODEL:-gpt-4o-mini}")"
  env_set "$_IT_ENV_FILE" CLK_CODEX_MODEL "$model"
  mark_tool_configured codex codex "$model"
  _it_say "[configure codex] saved model=$model"
}

_configure_gemini() {
  local auth_mode="${CLK_AUTH_MODE:-cli}"
  if [ "$auth_mode" = "apikey" ]; then
    local key="${GEMINI_API_KEY:-${GOOGLE_API_KEY:-}}"
    if [ -z "$key" ]; then
      key="$(_it_secret "Paste your GEMINI_API_KEY (input hidden)")"
      [ -n "$key" ] && env_set "$_IT_ENV_FILE" GEMINI_API_KEY "$key"
    fi
  else
    if _it_confirm "Run \`gemini auth login\` now? (opens a sub-shell)" "Y"; then
      _it_login_shell gemini
    fi
  fi
  local model
  model="$(_it_read "Gemini model" "${CLK_GEMINI_MODEL:-gemini-1.5-pro}")"
  env_set "$_IT_ENV_FILE" CLK_GEMINI_MODEL "$model"
  mark_tool_configured gemini gemini "$model"
  _it_say "[configure gemini] saved model=$model"
}

_configure_pi() {
  _it_say ""
  _it_say "[configure pi] Pi routes requests through an upstream LLM provider."
  _it_say "  openrouter   pay-as-you-go gateway to ~100 models"
  _it_say "  anthropic    direct Anthropic API"
  _it_say "  openai       direct OpenAI API"
  _it_say "  google       direct Gemini API"
  local route key_type_default suggestions
  key_type_default="${CLK_PI_KEY_TYPE:-openrouter}"
  route="$(_it_read "Upstream provider" "$key_type_default")"
  case "$route" in
    openrouter) suggestions="openrouter/auto, openrouter/free, anthropic/claude-3.5-sonnet" ;;
    anthropic)  suggestions="claude-sonnet-4-5, claude-3-5-sonnet-latest" ;;
    openai)     suggestions="gpt-4o, gpt-4o-mini, o1-mini" ;;
    google)     suggestions="gemini-1.5-pro, gemini-1.5-flash" ;;
    *)          suggestions="(provider-specific)" ;;
  esac
  _it_say "[configure pi] suggested models for $route: $suggestions"
  local model
  model="$(_it_read "Pi model (blank for pi's default)" "${CLK_PI_MODEL:-}")"

  # Login (skippable).
  if _it_has pi && _it_confirm "Open a shell for \`pi login\`?" "N"; then
    _it_login_shell pi
  fi

  # API key (skippable if pi login already handled auth).
  local key_var key
  case "$route" in
    openrouter) key_var="OPENROUTER_API_KEY" ;;
    anthropic)  key_var="ANTHROPIC_API_KEY" ;;
    openai)     key_var="OPENAI_API_KEY" ;;
    google)     key_var="GOOGLE_API_KEY" ;;
    *)          key_var="$(echo "${route^^}" | tr - _)_API_KEY" ;;
  esac
  if _it_confirm "Set $key_var now? (skip if pi login handled it)" "N"; then
    key="$(_it_secret "Paste $key_var (input hidden)")"
    [ -n "$key" ] && env_set "$_IT_ENV_FILE" "$key_var" "$key"
    env_set "$_IT_ENV_FILE" CLK_PI_API_KEY "$key"
  fi

  env_set "$_IT_ENV_FILE" CLK_PI_KEY_TYPE "$route"
  env_set "$_IT_ENV_FILE" CLK_PI_MODEL "$model"
  mark_tool_configured pi "$route" "$model"
  _it_say "[configure pi] saved route=$route model=${model:-<pi default>}"
}

_configure_ollama() {
  local endpoint
  endpoint="$(_it_read "Ollama endpoint" "${CLK_OLLAMA_ENDPOINT:-http://localhost:11434}")"
  env_set "$_IT_ENV_FILE" CLK_OLLAMA_ENDPOINT "$endpoint"

  # If the server isn't reachable, offer to start it.
  if ! check_tool ollama; then
    _it_warn "ollama endpoint $endpoint not reachable."
    if _it_has ollama && _it_confirm "Start \`ollama serve\` in the background?" "Y"; then
      ollama serve >/tmp/ollama-serve.log 2>&1 &
      sleep 2
    fi
  fi

  # List local models if we can.
  local listing=""
  if _it_has ollama; then
    listing="$(ollama list 2>/dev/null | awk 'NR>1 {print $1}' || true)"
  fi
  local choice model="${CLK_OLLAMA_MODEL:-llama3.1}"
  if [ -n "$listing" ]; then
    _it_say ""
    _it_say "[configure ollama] local models:"
    local n=0 m
    while IFS= read -r m; do
      [ -z "$m" ] && continue
      n=$((n+1))
      _it_say "  $n) $m"
    done <<< "$listing"
    _it_say "  $((n+1))) pull a new model by name"
    choice="$(_it_read "Pick a number (or type a model name)" "1")"
    if [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -le "$n" ]; then
      model="$(sed -n "${choice}p" <<< "$listing")"
    elif [[ "$choice" =~ ^[0-9]+$ ]] && [ "$choice" -eq "$((n+1))" ]; then
      model=""
    else
      model="$choice"
    fi
  fi

  if [ -z "$model" ]; then
    _it_say ""
    _it_say "[configure ollama] common models: llama3.1, llama3.2, qwen2.5-coder, gemma2, deepseek-r1"
    model="$(_it_read "Model name to pull" "llama3.1")"
    if _it_has ollama && _it_confirm "Run \`ollama pull $model\` now?" "Y"; then
      _it_say "[configure ollama] pulling $model — this can take a few minutes..."
      if ! ollama pull "$model"; then
        _it_warn "ollama pull failed; check the error above."
      fi
    fi
  fi

  env_set "$_IT_ENV_FILE" CLK_OLLAMA_MODEL "$model"
  mark_tool_configured ollama ollama "$model"
  _it_say "[configure ollama] saved endpoint=$endpoint model=$model"
}

_configure_openwebui() {
  local endpoint key model
  endpoint="$(_it_read "OpenWebUI endpoint" "${CLK_OPENWEBUI_ENDPOINT:-http://localhost:8080}")"
  env_set "$_IT_ENV_FILE" CLK_OPENWEBUI_ENDPOINT "$endpoint"
  if _it_confirm "Set OpenWebUI API key now? (only needed for authenticated instances)" "N"; then
    key="$(_it_secret "Paste OpenWebUI API key")"
    [ -n "$key" ] && env_set "$_IT_ENV_FILE" CLK_OPENWEBUI_API_KEY "$key"
  fi
  # Try the live model picker via the existing Python helper.
  local listing
  listing="$(CLK_OPENWEBUI_ENDPOINT="$endpoint" \
            CLK_OPENWEBUI_API_KEY="${key:-${CLK_OPENWEBUI_API_KEY:-}}" \
            PYTHONPATH="$_IT_PROJECT_DIR" \
            python3 -c "
from clk_harness.providers.openwebui import list_models
import os
print('\n'.join(list_models(os.environ['CLK_OPENWEBUI_ENDPOINT'],
                            os.environ.get('CLK_OPENWEBUI_API_KEY',''))))
" 2>/dev/null || true)"
  if [ -n "$listing" ]; then
    _it_say "[configure openwebui] available models on $endpoint:"
    local n=0 m
    while IFS= read -r m; do
      n=$((n+1))
      _it_say "  $n) $m"
    done <<< "$listing"
    local pick
    pick="$(_it_read "Pick a number or type a model name" "${CLK_OPENWEBUI_MODEL:-1}")"
    if [[ "$pick" =~ ^[0-9]+$ ]]; then
      model="$(sed -n "${pick}p" <<< "$listing")"
    else
      model="$pick"
    fi
  else
    model="$(_it_read "OpenWebUI model name" "${CLK_OPENWEBUI_MODEL:-llama3.1}")"
  fi
  env_set "$_IT_ENV_FILE" CLK_OPENWEBUI_MODEL "$model"
  mark_tool_configured openwebui openwebui "$model"
  _it_say "[configure openwebui] saved endpoint=$endpoint model=$model"
}

_configure_shell() {
  mark_tool_configured shell shell ""
  _it_say "[configure shell] nothing to configure — shell provider just echoes prompts."
}

configure_tool() {
  local name="$1"
  local fn="_configure_$name"
  if ! declare -F "$fn" >/dev/null; then
    _it_warn "no configure recipe for '$name'."
    return 2
  fi
  "$fn"
}

# ---------------------------------------------------------------------------
# Direct CLI dispatch (when this script is run, not sourced).
# ---------------------------------------------------------------------------
if [ "${BASH_SOURCE[0]}" = "${0}" ]; then
  cmd="${1:-}"; shift || true
  case "$cmd" in
    check)     check_tool "$@" ;;
    install)   install_tool "$@" ;;
    configure) configure_tool "$@" ;;
    *)
      cat >&2 <<USAGE
usage: scripts/install_tool.sh <check|install|configure> NAME [opts]

  check NAME            — 0 if NAME is installed/reachable, 1 otherwise.
  install NAME [mode]   — install NAME. mode: --prompt (default), --auto, --print-only.
  configure NAME        — first-use config flow (auth, route, model).
USAGE
      exit 2 ;;
  esac
fi
