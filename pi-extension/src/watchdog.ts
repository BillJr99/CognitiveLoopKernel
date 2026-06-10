/**
 * Run watchdog — the supervise loop, ported from the Python harness's
 * WorkflowRunner.run().
 *
 * In mainline CLK a code loop owns the run and dispatches the LLM; in
 * this extension the chief LLM *is* the loop, so a chief that ends its
 * turn without calling clk_done would leave the run sitting idle until
 * the user typed something. The watchdog closes that gap: on every
 * agent_end while a run is active and done.md is absent, it measures
 * material progress (HEAD moved or progress entries appended), then
 * either re-prompts the chief to continue, escalates to a rescue
 * prompt after consecutive no-progress turns, or stops the run when
 * the rescue also failed — the same continue → rescue → give-up ladder
 * as supervise.max_consecutive_no_progress + stall_rescue in the
 * Python harness.
 */

import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { head as gitHead } from "./git.js";
import { isActive, endRun } from "./abort.js";
import {
  getState,
  getSupervise,
  setSupervise,
  isDone,
} from "./state.js";
import type { ClkState, SuperviseState } from "./types.js";

/** Consecutive no-progress turns before the rescue prompt fires. */
export const DEFAULT_STALL_CAP = 3;
/** Hard cap on auto-continuations per run — mirrors supervise.max_cycles. */
export const DEFAULT_MAX_CONTINUES = 100;

export interface WatchdogLimits {
  stallCap: number;
  maxContinues: number;
}

export function limitsFromEnv(env: NodeJS.ProcessEnv = process.env): WatchdogLimits {
  const stall = Number.parseInt(env.CLK_STALL_CAP ?? "", 10);
  const max = Number.parseInt(env.CLK_MAX_AUTO_CONTINUES ?? "", 10);
  return {
    stallCap: Number.isFinite(stall) && stall > 0 ? stall : DEFAULT_STALL_CAP,
    maxContinues: Number.isFinite(max) && max > 0 ? max : DEFAULT_MAX_CONTINUES,
  };
}

export interface TurnSnapshot {
  /** Current git HEAD, or null when unavailable (not a repo yet). */
  head: string | null;
  /** Number of progress entries recorded so far. */
  progressCount: number;
}

export type WatchdogDecision =
  | { action: "continue"; reason: string }
  | { action: "rescue"; reason: string }
  | { action: "stop"; reason: string };

/**
 * Pure supervise-step: fold one finished chief turn into the counters
 * and decide what happens next. No I/O — fully unit-testable.
 */
export function evaluateTurn(
  prev: SuperviseState,
  snap: TurnSnapshot,
  limits: WatchdogLimits = limitsFromEnv(),
): { next: SuperviseState; decision: WatchdogDecision } {
  const next: SuperviseState = {
    ...prev,
    continuations: prev.continuations + 1,
    lastHead: snap.head,
    lastProgressCount: snap.progressCount,
  };

  if (next.continuations > limits.maxContinues) {
    return {
      next,
      decision: {
        action: "stop",
        reason:
          `auto-continue cap reached (${limits.maxContinues}); ` +
          "set CLK_MAX_AUTO_CONTINUES to raise it",
      },
    };
  }

  // First observed turn has no baseline — treat it as progress so a
  // fresh run always gets at least one continuation before stall logic
  // can engage.
  const baseline = prev.lastHead !== undefined || prev.lastProgressCount !== undefined;
  const material =
    !baseline ||
    snap.head !== prev.lastHead ||
    snap.progressCount > (prev.lastProgressCount ?? 0);

  if (material) {
    next.noProgress = 0;
    return {
      next,
      decision: { action: "continue", reason: "material progress observed" },
    };
  }

  next.noProgress = prev.noProgress + 1;
  if (next.noProgress >= limits.stallCap) {
    if (!prev.rescueAttempted) {
      next.rescueAttempted = true;
      next.noProgress = 0;
      return {
        next,
        decision: {
          action: "rescue",
          reason: `${limits.stallCap} consecutive turns without commits or progress entries`,
        },
      };
    }
    return {
      next,
      decision: {
        action: "stop",
        reason: "still stalled after the rescue prompt — ending the run",
      },
    };
  }
  return {
    next,
    decision: {
      action: "continue",
      reason: `no progress this turn (${next.noProgress}/${limits.stallCap})`,
    },
  };
}

function runRecap(state: ClkState, snap: TurnSnapshot): string {
  const lines: string[] = [];
  if (state.idea) lines.push(`Idea: ${state.idea.split("\n")[0]}`);
  if (state.roster) {
    lines.push(`Roster: ${state.roster.agents.map((a) => a.name).join(", ")}`);
  }
  if (snap.head) lines.push(`HEAD: ${snap.head.slice(0, 8)}`);
  const recent = state.progress.slice(-3);
  if (recent.length > 0) {
    lines.push("Recent progress:");
    for (const p of recent) lines.push(`  [${p.kind}] ${p.message.slice(0, 120)}`);
  }
  return lines.join("\n");
}

/**
 * Continuation prompt — sent after a turn that made progress (or is
 * only mildly stalled). Mirrors the supervise-mode "low bar to
 * continue" framing from the chief's primer.
 */
export function continuationMessage(state: ClkState, snap: TurnSnapshot): string {
  return [
    "[CLK watchdog] The run is still active and clk_done has not been called.",
    "",
    runRecap(state, snap),
    "",
    "Continue autonomously now — do not wait for the user:",
    "- Scan for any low-bar trigger (missing tests, no Ralph pass on the",
    "  latest output, TODOs, stale docs, any nameable improvement) and run",
    "  the next iteration (clk_ralph / clk_consensus / clk_autoresearch).",
    "- Record the transition with clk_progress and checkpoint your work.",
    "- Call clk_done ONLY if every completion criterion in your primer's",
    "  rule 11 is satisfied and no low-bar trigger applies.",
  ].join("\n");
}

/**
 * Rescue prompt — fired once per run after the stall cap. Same intent
 * as the Python harness's stall-rescue chief dispatch: restructure,
 * split, or justify done — but no more marginal tweaks.
 */
export function rescueMessage(state: ClkState, snap: TurnSnapshot, reason: string): string {
  return [
    `[CLK watchdog — STALL RESCUE] ${reason}.`,
    "",
    runRecap(state, snap),
    "",
    "The current approach is not producing commits. Do NOT repeat the",
    "last action. Choose exactly one:",
    "1. RESTRUCTURE — break the current objective into smaller concrete",
    "   tasks and dispatch the first one now (clk_ralph or clk_consensus).",
    "2. UNBLOCK — name the specific blocker, then run clk_autoresearch on",
    "   it and act on the finding.",
    "3. DONE — if and only if every rule-11 completion criterion is met,",
    "   call clk_done with the evidence in the reason.",
    "Whatever you choose, your next turn MUST produce a commit or an",
    "explicit clk_done — otherwise the watchdog ends the run as stalled.",
  ].join("\n");
}

export interface WatchdogDeps {
  /** Injectable for tests; defaults to git.head(). */
  getHead?: (cwd: string) => Promise<string | null>;
}

/**
 * agent_end hook body. Returns the decision taken (or null when the
 * watchdog did not engage) so the wiring is observable in tests.
 */
export async function onAgentEnd(
  pi: ExtensionAPI,
  ctx: {
    cwd: string;
    ui: {
      setStatus(key: string, value: string): void;
      notify(msg: string, level?: "info" | "warning" | "error"): void;
    };
  },
  deps: WatchdogDeps = {},
): Promise<WatchdogDecision | null> {
  if (!isActive()) return null;
  if (await isDone(ctx.cwd)) return null;

  const state = getState();
  if (!state.idea) return null; // no captured run to supervise

  const headFn = deps.getHead ?? ((cwd: string) => gitHead(cwd));
  let headSha: string | null = null;
  try {
    headSha = await headFn(ctx.cwd);
  } catch {
    headSha = null;
  }
  const snap: TurnSnapshot = { head: headSha, progressCount: state.progress.length };

  const { next, decision } = evaluateTurn(getSupervise(), snap);
  await setSupervise(ctx.cwd, next, pi);

  if (decision.action === "stop") {
    endRun(`watchdog: ${decision.reason}`);
    ctx.ui.setStatus("clk-run", "stalled");
    ctx.ui.notify(
      `CLK watchdog stopped the run: ${decision.reason}. ` +
        "State is preserved — /clk-resume continues it.",
      "warning",
    );
    return decision;
  }

  const message =
    decision.action === "rescue"
      ? rescueMessage(state, snap, decision.reason)
      : continuationMessage(state, snap);
  ctx.ui.setStatus(
    "clk-run",
    decision.action === "rescue" ? "active (rescue)" : `active (turn ${next.continuations})`,
  );
  pi.sendUserMessage(message);
  return decision;
}
