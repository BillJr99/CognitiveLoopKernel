#!/usr/bin/env bash
# scripts/lib_env.sh — shared .env mutation helpers.
#
# Sourced by kickoff.sh and scripts/telegram_setup_wizard.sh so both
# wizards write .env exactly the same way: atomic, with a .bak rotation,
# never partial-on-failure.
#
# Public API:
#   env_set FILE KEY VALUE     # set KEY=VALUE in FILE, atomically
#   env_get FILE KEY [DEFAULT] # echo current value (default if unset)
#   env_atomic_write FILE      # write stdin to FILE atomically (rotate to .bak)
#   env_restore FILE           # mv FILE.bak -> FILE

# Guard against double-sourcing.
[ -n "${_CLK_LIB_ENV_LOADED:-}" ] && return 0
_CLK_LIB_ENV_LOADED=1

# ---------------------------------------------------------------------------
# env_atomic_write FILE
#
# Reads stdin, writes it to FILE atomically (via a temp file in the same
# directory, fsync, rename) and rotates the previous FILE to FILE.bak so
# the user can always recover from a botched edit.
# ---------------------------------------------------------------------------
env_atomic_write() {
  local file="$1" tmp
  [ -z "$file" ] && { echo "[lib_env] env_atomic_write: missing FILE" >&2; return 2; }
  mkdir -p "$(dirname "$file")" || return 2
  tmp="$(mktemp "${file}.XXXXXX")" || return 2
  cat > "$tmp" || { rm -f "$tmp"; return 2; }
  # Best-effort fsync so a crash between rename and writeback doesn't
  # leave us with a zero-length file.
  sync "$tmp" 2>/dev/null || true
  if [ -f "$file" ]; then
    # Rotate previous content to .bak. mv is atomic on the same filesystem.
    mv -f "$file" "${file}.bak" 2>/dev/null || true
  fi
  mv -f "$tmp" "$file"
}

# ---------------------------------------------------------------------------
# env_set FILE KEY VALUE
#
# Idempotent. If KEY=... exists in FILE, the value is replaced; otherwise
# the line is appended. Comments and blanks are preserved. Uses
# env_atomic_write internally so a Ctrl-C mid-edit can't corrupt the file.
# ---------------------------------------------------------------------------
env_set() {
  local file="$1" key="$2" value="$3"
  [ -z "$file" ] || [ -z "$key" ] && {
    echo "[lib_env] env_set: usage: env_set FILE KEY VALUE" >&2; return 2; }
  mkdir -p "$(dirname "$file")"
  [ -f "$file" ] || : > "$file"
  awk -v k="$key" -v v="$value" '
    BEGIN { set = 0 }
    /^[[:space:]]*#/ { print; next }
    /^[[:space:]]*$/ { print; next }
    {
      if (index($0, k "=") == 1) {
        print k "=" v
        set = 1
        next
      }
      print
    }
    END { if (!set) print k "=" v }
  ' "$file" | env_atomic_write "$file"
}

# ---------------------------------------------------------------------------
# env_get FILE KEY [DEFAULT]
#
# Echoes the current value of KEY in FILE, or DEFAULT if unset. The .env
# format is plain `KEY=VALUE` so this is a simple grep/cut.
# ---------------------------------------------------------------------------
env_get() {
  local file="$1" key="$2" default="${3:-}"
  if [ ! -f "$file" ]; then
    printf '%s' "$default"
    return 0
  fi
  local line
  line="$(grep -E "^${key}=" "$file" | tail -n1)"
  if [ -z "$line" ]; then
    printf '%s' "$default"
    return 0
  fi
  printf '%s' "${line#*=}"
}

# ---------------------------------------------------------------------------
# env_restore FILE
#
# Moves FILE.bak back to FILE. Returns 1 if no backup exists.
# ---------------------------------------------------------------------------
env_restore() {
  local file="$1"
  [ -z "$file" ] && { echo "[lib_env] env_restore: missing FILE" >&2; return 2; }
  if [ ! -f "${file}.bak" ]; then
    echo "[lib_env] no backup at ${file}.bak" >&2
    return 1
  fi
  mv -f "${file}.bak" "$file"
}
