import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

/**
 * The active CLK orchestration run, if any. We track this at module scope so
 * tools and event handlers can co-operate without threading context everywhere.
 *
 * The controller serves three purposes:
 *   1. Refuse concurrent /clk runs in the same session.
 *   2. Provide a cancellation signal to long-running work in our own tools
 *      (git ops, fetches), so /clk-abort or shutdown propagates.
 *   3. Let tools detect "are we mid-run?" without inspecting state files.
 */
let active: AbortController | null = null;

export function startRun(): AbortController {
  if (active && !active.signal.aborted) {
    throw new Error(
      "A /clk run is already active in this session. Run /clk-abort first if you want to start over.",
    );
  }
  active = new AbortController();
  return active;
}

export function endRun(reason = "completed"): void {
  if (!active) return;
  if (!active.signal.aborted) active.abort(reason);
  active = null;
}

export function activeSignal(): AbortSignal | undefined {
  return active?.signal;
}

export function isActive(): boolean {
  return active !== null && !active.signal.aborted;
}

/**
 * Compose two AbortSignals into one that fires when either does. Used to
 * forward the run-wide cancel signal to tool-scoped operations.
 */
export function mergeSignals(a?: AbortSignal, b?: AbortSignal): AbortSignal | undefined {
  if (!a) return b;
  if (!b) return a;
  if (typeof (AbortSignal as unknown as { any?: (s: AbortSignal[]) => AbortSignal }).any === "function") {
    return (AbortSignal as unknown as { any: (s: AbortSignal[]) => AbortSignal }).any([a, b]);
  }
  const ctrl = new AbortController();
  const onAbort = () => ctrl.abort();
  if (a.aborted || b.aborted) ctrl.abort();
  else {
    a.addEventListener("abort", onAbort, { once: true });
    b.addEventListener("abort", onAbort, { once: true });
  }
  return ctrl.signal;
}

export function installAbortBridges(pi: ExtensionAPI): void {
  pi.on("session_shutdown", async () => endRun("session_shutdown"));

  pi.registerCommand("clk-abort", {
    description: "Abort the active /clk orchestration run, if any.",
    handler: async (_args, ctx) => {
      if (!isActive()) {
        ctx.ui.notify("No active /clk run to abort.", "info");
        return;
      }
      endRun("user requested via /clk-abort");
      // Cancel the chief's current model turn cooperatively. pi-subagents
      // forwards the parent abort signal to in-flight child sessions, so
      // any spawned subagents are torn down too.
      if (!ctx.isIdle()) {
        ctx.abort();
      }
      ctx.ui.setStatus("clk-run", "aborted");
      ctx.ui.notify("CLK run aborted. Subagents in flight have been signalled to stop.", "warning");
    },
  });
}
