#!/usr/bin/env bash
# Convenience wrapper: run the Ralph loop with a sensible default budget.
#
# Usage:
#   scripts/run_loop.sh                    # 20 iterations, ralph mode
#   scripts/run_loop.sh --mode autoresearch
#   scripts/run_loop.sh --max-iterations 5
#   scripts/run_loop.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
exec "$SCRIPT_DIR/clk" loop "$@"
