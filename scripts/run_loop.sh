#!/usr/bin/env bash
# Convenience wrapper: run the Ralph loop with a sensible default budget.
#
# This is a thin shim around `clk loop`. The loop's behavior is
# documented in docs/MISSIONS.md sections:
#
#   * "Loops"             — the two Ralph modes (refinement,
#                           autoresearch) and how they pick what to do
#                           each iteration.
#   * "Robustness loops"  — what happens around each iteration:
#                           response-quality re-dispatch, auto-consensus
#                           fan-out, critic refinement, plateau detection
#                           with escalate-then-reframe.
#
# Both loops respect `clk.config.json::max_iterations` (overridable via
# `--max-iterations N`) and stop early when `.clk/state/done.md`
# exists. Plateau termination writes done.md automatically when
# `robustness.plateau_action` is enabled (default).
#
# Usage:
#   scripts/run_loop.sh                    # 20 iterations, ralph mode
#   scripts/run_loop.sh --mode autoresearch
#   scripts/run_loop.sh --max-iterations 5
#   scripts/run_loop.sh --dry-run

set -euo pipefail

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
exec "$SCRIPT_DIR/clk" loop "$@"
