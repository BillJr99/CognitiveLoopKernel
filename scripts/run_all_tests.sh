#!/usr/bin/env bash
#
# run_all_tests.sh — Run every CLK test suite (regression + user tests) in
# an ephemeral Docker container that this script creates and tears down.
#
# Usage:
#   ./scripts/run_all_tests.sh                # Interactive: prompts for
#                                             # provider/key/url/model
#   ./scripts/run_all_tests.sh --non-interactive
#                                             # Skip prompts, use 'shell'
#   ./scripts/run_all_tests.sh --local        # Run on host without Docker
#   ./scripts/run_all_tests.sh --keep         # Don't remove image/container
#   ./scripts/run_all_tests.sh --no-build     # Reuse the existing test image
#   ./scripts/run_all_tests.sh --suite=user   # Only run user_tests/
#   ./scripts/run_all_tests.sh --suite=ci     # Only run tests/
#   ./scripts/run_all_tests.sh --suite=pi     # Only run pi-extension/tests/
#   ./scripts/run_all_tests.sh -k <expr>      # Forward pytest -k filter
#   ./scripts/run_all_tests.sh -- <args>      # Pass extra args to pytest
#
# Exit codes:
#   0  all tests passed
#   1  one or more test suites failed
#   2  invalid usage / environment issue (no Docker, etc.)
#
# Notes:
#   * The interactive menu lets you pick which LLM the kickoff-style smoke
#     test will use.  Deterministic CLI and API tests always use the
#     'shell' provider regardless (no API keys, always available).
#   * Docker mode builds an ephemeral image tagged 'clk:tests-<pid>',
#     copies the repo into /work inside the container, runs pytest, and
#     **always tears down the container on exit** (success or failure)
#     unless --keep is passed.

set -euo pipefail

# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------
LOCAL=false
KEEP=false
NO_BUILD=false
INTERACTIVE=true
SUITE="all"   # all | ci | user
PYTEST_EXTRA=()

show_help() {
  sed -n '2,/^$/p' "$0" | sed 's/^# *//;s/^#$//'
  exit 0
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    -h|--help) show_help ;;
    --local)            LOCAL=true; shift ;;
    --keep)             KEEP=true; shift ;;
    --no-build)         NO_BUILD=true; shift ;;
    --non-interactive)  INTERACTIVE=false; shift ;;
    --interactive)      INTERACTIVE=true; shift ;;
    --suite=*)          SUITE="${1#*=}"; shift ;;
    --suite)
      [[ $# -lt 2 ]] && { echo "--suite requires a value" >&2; exit 2; }
      SUITE="$2"; shift 2 ;;
    -k|--keyword)
      [[ $# -lt 2 ]] && { echo "$1 requires a value" >&2; exit 2; }
      PYTEST_EXTRA+=("-k" "$2"); shift 2 ;;
    --) shift; PYTEST_EXTRA+=("$@"); break ;;
    *)
      PYTEST_EXTRA+=("$1"); shift ;;
  esac
done

case "$SUITE" in
  all|ci|user|pi) ;;
  *) echo "[run_all_tests] unknown suite: $SUITE (use all|ci|user|pi)" >&2; exit 2 ;;
esac

REPO_ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." >/dev/null 2>&1 && pwd)"

CI_PATH="tests/"
USER_PATH="user_tests/"
PI_EXT_PATH="pi-extension"   # npm-driven; runs alongside the pytest suites

# Pytest suites only; pi-extension is run separately (npm) inside the runner.
case "$SUITE" in
  all)  TEST_PATHS=("$CI_PATH" "$USER_PATH"); RUN_PI_EXT=true ;;
  ci)   TEST_PATHS=("$CI_PATH");              RUN_PI_EXT=false ;;
  user) TEST_PATHS=("$USER_PATH");            RUN_PI_EXT=false ;;
  pi)   TEST_PATHS=();                        RUN_PI_EXT=true ;;
esac

# ---------------------------------------------------------------------------
# Friendly interactive menu — collects values like LLM provider, API key,
# base URL, and model.  All values are exported as CLK_* env vars so they
# flow through to the test container.  Skip with --non-interactive.
# ---------------------------------------------------------------------------

# Resolved selections (consumed below).
TEST_PROVIDER="shell"
TEST_ANTHROPIC_API_KEY=""
TEST_OPENAI_API_KEY=""
TEST_GEMINI_API_KEY=""
TEST_OLLAMA_ENDPOINT=""
TEST_OLLAMA_MODEL=""
TEST_OPENWEBUI_ENDPOINT=""
TEST_OPENWEBUI_API_KEY=""
TEST_OPENWEBUI_MODEL=""
TEST_PI_MODEL=""
TEST_PI_KEY_TYPE=""
TEST_PI_API_KEY=""
TEST_AUTH_MODE="cli"

_prompt() {
  # Usage: _prompt VAR "Friendly question" [default]
  local __var="$1" __msg="$2" __default="${3:-}" __reply=""
  if [ -n "$__default" ]; then
    printf '  %s [%s]: ' "$__msg" "$__default" >&2
  else
    printf '  %s: ' "$__msg" >&2
  fi
  IFS= read -r __reply || true
  printf -v "$__var" '%s' "${__reply:-$__default}"
}

_prompt_secret() {
  # Hide echo while typing secrets.
  local __var="$1" __msg="$2" __reply=""
  printf '  %s (leave blank to skip): ' "$__msg" >&2
  stty -echo 2>/dev/null || true
  IFS= read -r __reply || true
  stty echo 2>/dev/null || true
  printf '\n' >&2
  printf -v "$__var" '%s' "$__reply"
}

_choose_provider() {
  cat >&2 <<MENU

  Select an LLM backend for tests that exercise live model calls.
  (Unit, CLI, and REST API tests always use the 'shell' provider — no
  API keys are needed for them.)

    1) shell      — Dummy provider, no network, always available (default)
    2) claude     — Anthropic Claude (via CLI or API key)
    3) codex      — OpenAI Codex (via CLI or API key)
    4) gemini     — Google Gemini (via CLI or API key)
    5) pi         — pi.dev terminal agent (OpenRouter-style keys)
    6) ollama     — Local Ollama server (HTTP)
    7) openwebui  — Any OpenAI-compatible HTTP endpoint (e.g. OpenWebUI)

MENU
  local choice=""
  _prompt choice "Choose [1-7]" "1"
  case "$choice" in
    1|shell)     TEST_PROVIDER="shell" ;;
    2|claude)    TEST_PROVIDER="claude" ;;
    3|codex)     TEST_PROVIDER="codex" ;;
    4|gemini)    TEST_PROVIDER="gemini" ;;
    5|pi)        TEST_PROVIDER="pi" ;;
    6|ollama)    TEST_PROVIDER="ollama" ;;
    7|openwebui) TEST_PROVIDER="openwebui" ;;
    *)
      echo "  (Unrecognised choice $choice — defaulting to shell)" >&2
      TEST_PROVIDER="shell" ;;
  esac
}

_choose_auth_mode() {
  cat >&2 <<MENU

  How should '$TEST_PROVIDER' authenticate?
    1) cli     — Trust the provider's locally-installed CLI (default)
                 e.g. you have already run 'claude login'
    2) apikey  — Pass an API key directly to the upstream HTTP API
MENU
  local choice=""
  _prompt choice "Choose [1-2]" "1"
  case "$choice" in
    2|apikey|api) TEST_AUTH_MODE="apikey" ;;
    *)            TEST_AUTH_MODE="cli" ;;
  esac
}

_collect_provider_settings() {
  echo >&2
  echo "  Configuring provider: $TEST_PROVIDER" >&2
  case "$TEST_PROVIDER" in
    shell)
      echo "  (shell provider needs no further configuration)" >&2
      ;;
    claude)
      _choose_auth_mode
      [ "$TEST_AUTH_MODE" = "apikey" ] && _prompt_secret TEST_ANTHROPIC_API_KEY "ANTHROPIC_API_KEY"
      ;;
    codex)
      _choose_auth_mode
      [ "$TEST_AUTH_MODE" = "apikey" ] && _prompt_secret TEST_OPENAI_API_KEY "OPENAI_API_KEY"
      ;;
    gemini)
      _choose_auth_mode
      [ "$TEST_AUTH_MODE" = "apikey" ] && _prompt_secret TEST_GEMINI_API_KEY "GEMINI_API_KEY"
      ;;
    ollama)
      _prompt TEST_OLLAMA_ENDPOINT "Ollama base URL" "http://localhost:11434"
      _prompt TEST_OLLAMA_MODEL    "Ollama model name" "llama3.1"
      ;;
    openwebui)
      _prompt TEST_OPENWEBUI_ENDPOINT "OpenWebUI base URL" "http://localhost:8080"
      _prompt_secret TEST_OPENWEBUI_API_KEY "OpenWebUI API key (optional)"
      _prompt TEST_OPENWEBUI_MODEL    "OpenWebUI model name" "llama3.1"
      ;;
    pi)
      cat >&2 <<HINT
  Examples for pi model:
    openrouter/free               — free OpenRouter routing
    openrouter/auto               — let OpenRouter pick a free model
    anthropic/claude-3-5-sonnet   — specific OpenRouter model
HINT
      _prompt TEST_PI_MODEL    "pi model (blank = pi default)" ""
      _prompt TEST_PI_KEY_TYPE "API key provider (openrouter|openai|anthropic|...)" "openrouter"
      _prompt_secret TEST_PI_API_KEY "API key for $TEST_PI_KEY_TYPE"
      ;;
  esac
}

if $INTERACTIVE && [ -t 0 ]; then
  cat >&2 <<HEADER

==============================================================================
  CLK Test Suite — interactive configuration
==============================================================================
HEADER
  _choose_provider
  _collect_provider_settings
  echo >&2
  echo "  Summary:" >&2
  echo "    provider:  $TEST_PROVIDER" >&2
  echo "    auth mode: $TEST_AUTH_MODE" >&2
  echo "    suite:     $SUITE" >&2
  echo "    mode:      $($LOCAL && echo 'local (host)' || echo 'docker (ephemeral)')" >&2
  echo >&2
  _confirm=""
  _prompt _confirm "Proceed? [Y/n]" "Y"
  case "${_confirm,,}" in
    n|no) echo "[run_all_tests] aborted by user" >&2; exit 0 ;;
  esac
elif ! $INTERACTIVE; then
  echo "[run_all_tests] --non-interactive: using provider=shell" >&2
fi

# Export selections so the test environment / container can see them.
export CLK_PROVIDER="$TEST_PROVIDER"
export CLK_AUTH_MODE="$TEST_AUTH_MODE"
export ANTHROPIC_API_KEY="${TEST_ANTHROPIC_API_KEY:-${ANTHROPIC_API_KEY:-}}"
export OPENAI_API_KEY="${TEST_OPENAI_API_KEY:-${OPENAI_API_KEY:-}}"
export GEMINI_API_KEY="${TEST_GEMINI_API_KEY:-${GEMINI_API_KEY:-}}"
export CLK_OLLAMA_ENDPOINT="${TEST_OLLAMA_ENDPOINT:-${CLK_OLLAMA_ENDPOINT:-}}"
export CLK_OLLAMA_MODEL="${TEST_OLLAMA_MODEL:-${CLK_OLLAMA_MODEL:-}}"
export CLK_OPENWEBUI_ENDPOINT="${TEST_OPENWEBUI_ENDPOINT:-${CLK_OPENWEBUI_ENDPOINT:-}}"
export CLK_OPENWEBUI_API_KEY="${TEST_OPENWEBUI_API_KEY:-${CLK_OPENWEBUI_API_KEY:-}}"
export CLK_OPENWEBUI_MODEL="${TEST_OPENWEBUI_MODEL:-${CLK_OPENWEBUI_MODEL:-}}"
export CLK_PI_MODEL="${TEST_PI_MODEL:-${CLK_PI_MODEL:-}}"
export CLK_PI_KEY_TYPE="${TEST_PI_KEY_TYPE:-${CLK_PI_KEY_TYPE:-}}"
export CLK_PI_API_KEY="${TEST_PI_API_KEY:-${CLK_PI_API_KEY:-}}"

# ---------------------------------------------------------------------------
# Local mode — run pytest directly on the host
# ---------------------------------------------------------------------------
if $LOCAL; then
  echo "[run_all_tests] mode=local  suite=$SUITE  provider=$CLK_PROVIDER"
  cd "$REPO_ROOT"

  # Best-effort install of dev deps without clobbering the user's env.
  python3 -m pip install --quiet --upgrade pip || true
  python3 -m pip install --quiet -e ".[api,dev]" || \
    python3 -m pip install --quiet PyYAML fastapi uvicorn pydantic pytest pytest-asyncio httpx

  fail=0
  for path in "${TEST_PATHS[@]}"; do
    if [ ! -e "$path" ]; then
      echo "[run_all_tests] skipping $path (not present)"
      continue
    fi
    echo
    echo "=== pytest $path ==="
    if ! python3 -m pytest "$path" -v "${PYTEST_EXTRA[@]}"; then
      fail=1
    fi
  done

  if $RUN_PI_EXT && [ -d "$PI_EXT_PATH" ] && command -v npm >/dev/null 2>&1; then
    echo
    echo "=== npm test (pi-extension) ==="
    (
      cd "$PI_EXT_PATH"
      # Install once if node_modules is absent; reuse it otherwise.
      [ -d node_modules ] || npm install --no-audit --no-fund --silent
      npm test
    ) || fail=1
  elif $RUN_PI_EXT; then
    echo "[run_all_tests] pi-extension skipped (missing dir or npm)"
  fi

  exit "$fail"
fi

# ---------------------------------------------------------------------------
# Docker mode — build an ephemeral image, run tests inside, tear down.
# ---------------------------------------------------------------------------
if ! command -v docker >/dev/null 2>&1; then
  echo "[run_all_tests] docker not found on PATH. Re-run with --local to test on the host." >&2
  exit 2
fi
if ! docker info >/dev/null 2>&1; then
  echo "[run_all_tests] cannot reach the docker daemon. Re-run with --local to test on the host." >&2
  exit 2
fi

IMAGE_TAG="clk:tests-$$"
CONTAINER_NAME="clk-tests-$$"

# Tear-down: always remove the container and (unless --no-build / --keep) the
# image too.  Trap fires on success, failure, ^C, and any explicit exit.
cleanup() {
  local rc=$?
  echo
  if $KEEP; then
    echo "[run_all_tests] --keep set; leaving image=$IMAGE_TAG container=$CONTAINER_NAME"
  else
    echo "[run_all_tests] tearing down Docker container $CONTAINER_NAME ..."
    docker rm -f "$CONTAINER_NAME" >/dev/null 2>&1 || true
    if ! $NO_BUILD; then
      echo "[run_all_tests] removing ephemeral image $IMAGE_TAG ..."
      docker rmi "$IMAGE_TAG" >/dev/null 2>&1 || true
    fi
    echo "[run_all_tests] teardown complete."
  fi
  exit "$rc"
}
trap cleanup EXIT INT TERM

# Build (or reuse) the image.
if $NO_BUILD; then
  IMAGE_TAG="clk:tests-latest"
  if ! docker image inspect "$IMAGE_TAG" >/dev/null 2>&1; then
    echo "[run_all_tests] --no-build set but $IMAGE_TAG does not exist" >&2
    exit 2
  fi
else
  echo "[run_all_tests] building $IMAGE_TAG from $REPO_ROOT/Dockerfile ..."
  docker build -q -t "$IMAGE_TAG" "$REPO_ROOT" >/dev/null
  echo "[run_all_tests] build OK: $IMAGE_TAG"
fi

# Compose the in-container test command.
TEST_ARGS_STR=""
for p in "${TEST_PATHS[@]}"; do TEST_ARGS_STR="$TEST_ARGS_STR $p"; done
PYTEST_EXTRA_STR=""
for a in "${PYTEST_EXTRA[@]:-}"; do
  PYTEST_EXTRA_STR="$PYTEST_EXTRA_STR $(printf '%q' "$a")"
done

read -r -d '' IN_CONTAINER <<'SCRIPT' || true
set -e
echo "[in-container] preparing /work"
mkdir -p /work
cp -a /repo/. /work/
cd /work

echo "[in-container] installing Python test dependencies"
pip install --quiet --upgrade pip
pip install --quiet -e ".[api,dev]" pytest pytest-asyncio httpx

# Disable the background API auto-launcher so test fixtures don't fight
# for ports 8001 vs the per-test free_port fixture.
export CLK_DISABLE_API=1
# Give the in-process REST API tests a private workspaces root so they
# don't need root to write to /workspaces.
export CLK_WORKSPACES_DIR=/tmp/clk-workspaces

echo "[in-container] CLK_PROVIDER=${CLK_PROVIDER:-shell} (test-selected backend)"

fail=0
PYTEST_TARGETS="__TEST_ARGS__"
if [ -n "$(printf '%s' "$PYTEST_TARGETS" | tr -d '[:space:]')" ]; then
  echo "[in-container] running pytest suites: $PYTEST_TARGETS"
  pytest -v $PYTEST_TARGETS __PYTEST_EXTRA__ || fail=$?
fi

if [ "__RUN_PI_EXT__" = "true" ] && [ -d pi-extension ]; then
  echo "[in-container] running pi-extension npm test (npm install + test)"
  if command -v npm >/dev/null 2>&1; then
    (
      cd pi-extension
      npm install --no-audit --no-fund --silent
      npm test
    ) || fail=$?
  else
    echo "[in-container] npm not available; skipping pi-extension"
  fi
fi

exit "$fail"
SCRIPT

IN_CONTAINER="${IN_CONTAINER//__TEST_ARGS__/$TEST_ARGS_STR}"
IN_CONTAINER="${IN_CONTAINER//__PYTEST_EXTRA__/$PYTEST_EXTRA_STR}"
IN_CONTAINER="${IN_CONTAINER//__RUN_PI_EXT__/$($RUN_PI_EXT && echo true || echo false)}"

# Build the -e env-var list to pass into docker run.
DOCKER_ENV_ARGS=(
  -e "CLK_PROVIDER=$CLK_PROVIDER"
  -e "CLK_AUTH_MODE=$CLK_AUTH_MODE"
  -e "ANTHROPIC_API_KEY=$ANTHROPIC_API_KEY"
  -e "OPENAI_API_KEY=$OPENAI_API_KEY"
  -e "GEMINI_API_KEY=$GEMINI_API_KEY"
  -e "CLK_OLLAMA_ENDPOINT=$CLK_OLLAMA_ENDPOINT"
  -e "CLK_OLLAMA_MODEL=$CLK_OLLAMA_MODEL"
  -e "CLK_OPENWEBUI_ENDPOINT=$CLK_OPENWEBUI_ENDPOINT"
  -e "CLK_OPENWEBUI_API_KEY=$CLK_OPENWEBUI_API_KEY"
  -e "CLK_OPENWEBUI_MODEL=$CLK_OPENWEBUI_MODEL"
  -e "CLK_PI_MODEL=$CLK_PI_MODEL"
  -e "CLK_PI_KEY_TYPE=$CLK_PI_KEY_TYPE"
  -e "CLK_PI_API_KEY=$CLK_PI_API_KEY"
)

echo "[run_all_tests] launching container $CONTAINER_NAME (suite=$SUITE)"
echo "[run_all_tests] (Press Ctrl-C to abort; teardown still runs.)"
set +e
docker run \
  --name "$CONTAINER_NAME" \
  --rm \
  -v "$REPO_ROOT:/repo:ro" \
  "${DOCKER_ENV_ARGS[@]}" \
  --entrypoint /bin/bash \
  "$IMAGE_TAG" \
  -c "$IN_CONTAINER"
status=$?
set -e

if [ "$status" -eq 0 ]; then
  echo "[run_all_tests] ALL TESTS PASSED (suite=$SUITE)"
else
  echo "[run_all_tests] TESTS FAILED (suite=$SUITE, exit=$status)" >&2
fi
exit "$status"
