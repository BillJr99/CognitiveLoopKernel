#!/usr/bin/env bash
# CLK Telegram bot setup wizard.
#
# Walks the operator through:
#   1. Creating a bot with @BotFather and validating the token.
#   2. Discovering their numeric Telegram user ID by sending a message
#      to the new bot (we poll getUpdates and extract message.from.id).
#   3. Persisting CLK_TELEGRAM_BOT_TOKEN, CLK_TELEGRAM_ALLOWED_USERS, and
#      CLK_TELEGRAM_ENABLED into the project's .env file (idempotent).
#
# Usage:
#   scripts/telegram_setup_wizard.sh                # interactive
#   CLK_TELEGRAM_BOT_TOKEN=... \
#     CLK_TELEGRAM_ALLOWED_USERS=123 \
#     CLK_TELEGRAM_SETUP_NONINTERACTIVE=1 \
#     scripts/telegram_setup_wizard.sh              # CI / scripted
#
# Works inside `docker run -it` (reads/writes /dev/tty when available).

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_DIR="$(cd -- "$SCRIPT_DIR/.." >/dev/null 2>&1 && pwd)"
ENV_FILE="${CLK_ENV_FILE:-$PROJECT_DIR/.env}"

# Open /dev/tty for prompts when stdin is piped (docker run -it). Tests
# can force stdin via CLK_TELEGRAM_NO_TTY=1.
if [ "${CLK_TELEGRAM_NO_TTY:-0}" = "1" ]; then
  exec 3<&0
  exec 4>&2
else
  { exec 3</dev/tty; } 2>/dev/null || exec 3<&0
  { exec 4>/dev/tty; } 2>/dev/null || exec 4>&2
fi

_say() { printf '%s\n' "$*" >&4; }

_read() {
  local prompt="$1" default="${2:-}" v
  if [ -n "$default" ]; then
    printf '%s [%s]: ' "$prompt" "$default" >&4
  else
    printf '%s: ' "$prompt" >&4
  fi
  IFS= read -r v <&3 || v=""
  printf '%s' "${v:-$default}"
}

_secret() {
  local prompt="$1" v
  printf '%s: ' "$prompt" >&4
  stty -echo </dev/tty 2>/dev/null || true
  IFS= read -r v <&3 || v=""
  stty echo  </dev/tty 2>/dev/null || true
  printf '\n' >&4
  printf '%s' "$v"
}

# Load existing .env values so we can show them as defaults.
if [ -f "$ENV_FILE" ]; then
  set -a; . "$ENV_FILE"; set +a
fi

# ----- env-file mutation -------------------------------------------------

_env_set() {
  local key="$1" value="$2"
  local tmp
  mkdir -p "$(dirname "$ENV_FILE")"
  touch "$ENV_FILE"
  tmp="$(mktemp "${ENV_FILE}.XXXXXX")"
  awk -v k="$key" -v v="$value" '
    BEGIN { set=0 }
    /^[[:space:]]*#/ { print; next }
    /^[[:space:]]*$/ { print; next }
    {
      if (index($0, k"=") == 1) { print k"="v; set=1; next }
      print
    }
    END { if (!set) print k"="v }
  ' "$ENV_FILE" > "$tmp"
  mv "$tmp" "$ENV_FILE"
}

# ----- HTTP helper -------------------------------------------------------

_http_get() {
  local url="$1"
  if command -v curl >/dev/null 2>&1; then
    curl -fsS --max-time 30 "$url" || return $?
  else
    python3 - "$url" <<'PY'
import sys, urllib.request, urllib.error
url = sys.argv[1]
try:
    with urllib.request.urlopen(url, timeout=30) as r:
        sys.stdout.write(r.read().decode("utf-8"))
except urllib.error.HTTPError as e:
    sys.exit(e.code)
except Exception as e:
    sys.stderr.write(str(e) + "\n")
    sys.exit(1)
PY
  fi
}

_extract_json_field() {
  # Read JSON from stdin, print first occurrence of FIELD. We pass the
  # program via -c (not stdin) so the piped JSON reaches sys.stdin
  # cleanly.
  python3 -c '
import json, sys
field = sys.argv[1]
data = json.load(sys.stdin)
def walk(obj):
    if isinstance(obj, dict):
        if field in obj:
            print(obj[field]); return True
        for v in obj.values():
            if walk(v): return True
    elif isinstance(obj, list):
        for v in obj:
            if walk(v): return True
    return False
walk(data)
' "$1"
}

# ----- step 1: token -----------------------------------------------------

_say ""
_say "=== CLK Telegram Bot Setup ==="
_say ""
_say "1. Open Telegram and start a chat with @BotFather."
_say "2. Send /newbot, pick a display name, then a unique username ending in 'bot'."
_say "3. BotFather will reply with an HTTP API token like 123456:ABC-DEF..."
_say ""

token="${CLK_TELEGRAM_BOT_TOKEN:-}"
while :; do
  if [ -n "$token" ]; then
    keep="$(_read "Use existing token (masked)? [Y/n]" "Y")"
    case "${keep,,}" in
      y|yes|"") : ;;
      *) token="" ;;
    esac
  fi
  if [ -z "$token" ]; then
    token="$(_secret "Paste the bot token")"
  fi
  if [ -z "$token" ]; then
    _say "[wizard] token cannot be empty"
    continue
  fi
  _say "[wizard] verifying token with api.telegram.org..."
  if resp="$(_http_get "https://api.telegram.org/bot${token}/getMe" 2>/dev/null)"; then
    bot_username="$(printf '%s' "$resp" | _extract_json_field username || true)"
    if [ -n "$bot_username" ]; then
      _say "[wizard] token OK: bot is @${bot_username}"
      break
    fi
  fi
  _say "[wizard] token rejected by Telegram. Re-enter."
  token=""
done

# ----- step 2: discover user IDs -----------------------------------------

declare -a captured_ids=()

# Parse existing allowlist (comma-separated).
existing_allow="${CLK_TELEGRAM_ALLOWED_USERS:-}"
if [ -n "$existing_allow" ]; then
  _say ""
  _say "[wizard] existing allowlist: $existing_allow"
fi

_say ""
_say "Now we'll discover your numeric Telegram user ID."
_say "Open Telegram, find @${bot_username:-your_new_bot}, and send it ANY message."
_say "Then press Enter here to continue."

if [ "${CLK_TELEGRAM_SETUP_NONINTERACTIVE:-0}" != "1" ]; then
  IFS= read -r _ <&3 || true
fi

if updates="$(_http_get "https://api.telegram.org/bot${token}/getUpdates" 2>/dev/null)"; then
  while IFS= read -r uid; do
    [ -z "$uid" ] && continue
    captured_ids+=("$uid")
  done < <(printf '%s' "$updates" | python3 -c '
import json, sys
data = json.load(sys.stdin)
seen = set()
for upd in data.get("result", []):
    msg = upd.get("message") or upd.get("edited_message") or {}
    frm = msg.get("from") or {}
    uid = frm.get("id")
    if uid is not None and uid not in seen:
        seen.add(uid)
        print(uid)
')
fi

if [ "${#captured_ids[@]}" -eq 0 ]; then
  _say "[wizard] no messages found in getUpdates."
  manual="$(_read "Enter your numeric Telegram user ID manually (or blank to skip)" "")"
  if [ -n "$manual" ]; then
    captured_ids+=("$manual")
  fi
else
  for uid in "${captured_ids[@]}"; do
    _say "[wizard] detected user ID: $uid"
  done
fi

# Merge with existing allowlist.
merged="$existing_allow"
for uid in "${captured_ids[@]}"; do
  case ",$merged," in
    *",$uid,"*) ;;
    *) [ -n "$merged" ] && merged="$merged,$uid" || merged="$uid" ;;
  esac
done

if [ -z "$merged" ]; then
  _say "[wizard] WARNING: allowlist is empty -- the bot will refuse everyone."
fi

# ----- step 3: persist ---------------------------------------------------

_env_set CLK_TELEGRAM_BOT_TOKEN "$token"
_env_set CLK_TELEGRAM_ALLOWED_USERS "$merged"
_env_set CLK_TELEGRAM_ENABLED "true"

_say ""
_say "[wizard] saved to $ENV_FILE:"
_say "  CLK_TELEGRAM_BOT_TOKEN=<hidden>"
_say "  CLK_TELEGRAM_ALLOWED_USERS=$merged"
_say "  CLK_TELEGRAM_ENABLED=true"
_say ""
_say "Next steps:"
_say "  - Start the bot:  clk-telegram-bot"
_say "  - Or as systemd:  see README -> Tutorial 3 (Raspberry Pi)"
_say ""
