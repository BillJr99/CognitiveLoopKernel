import { createRequire } from "node:module";
import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join } from "node:path";
import { homedir } from "node:os";
import { execSync } from "node:child_process";
import type {
  ExtensionAPI,
  ExtensionCommandContext,
} from "@mariozechner/pi-coding-agent";
import {
  loadFromFiles,
  reset,
  getState,
  setIdea,
  appendProgress,
  isDone,
} from "./state.js";
import { ensureRepo } from "./git.js";
import { clkChiefPrimer } from "./prompts.js";
import { registerClkTools } from "./tools.js";
import { startRun, endRun, installAbortBridges, activeSignal } from "./abort.js";
import { classifyError, recoveryHint, withRetry } from "./errors.js";

function piSubagentsInstalled(cwd?: string): boolean {
  // 1. npm resolution — works when pi-subagents is in the same node_modules
  //    tree (e.g. declared as a dependency and npm-installed).
  try {
    createRequire(import.meta.url).resolve("pi-subagents");
    return true;
  } catch { /* not in npm tree */ }

  // 2. Pi's settings.json — lists every extension Pi knows about regardless
  //    of where it's stored on disk.
  const settingsPaths = [
    join(homedir(), ".pi", "agent", "settings.json"),
    join(homedir(), ".pi", "settings.json"),
  ];
  for (const sp of settingsPaths) {
    try {
      const settings = JSON.parse(readFileSync(sp, "utf8")) as Record<string, unknown>;
      const extensions = settings.extensions;
      if (Array.isArray(extensions) && extensions.some(
        (e: unknown) => typeof e === "string" && /\bpi-subagents\b/i.test(e),
      )) return true;
    } catch { /* file missing or malformed */ }
  }

  // 3. Scan Pi's extensions directory — any subdirectory whose name or whose
  //    package.json "name" field matches pi-subagents.
  const piExtDirs = [
    join(homedir(), ".pi", "agent", "extensions"),
    join(homedir(), ".pi", "extensions"),
    ...(cwd ? [join(cwd, ".pi", "extensions")] : []),
  ];
  for (const extDir of piExtDirs) {
    try {
      for (const entry of readdirSync(extDir, { withFileTypes: true })) {
        if (!entry.isDirectory()) continue;
        if (/^pi-subagents$/i.test(entry.name)) return true;
        // Also check the package.json inside the subdirectory.
        try {
          const pkg = JSON.parse(
            readFileSync(join(extDir, entry.name, "package.json"), "utf8"),
          ) as Record<string, unknown>;
          if (typeof pkg.name === "string" && /pi-subagents/i.test(pkg.name)) return true;
        } catch { /* no package.json */ }
      }
    } catch { /* directory missing */ }
  }

  // 4. Global npm — `pi install npm:pi-subagents` delegates to npm install -g.
  //    This check is last because execSync blocks the event loop; try fast
  //    filesystem checks first.
  try {
    const globalRoot = execSync("npm root -g", { timeout: 5000 }).toString().trim();
    if (existsSync(join(globalRoot, "pi-subagents"))) return true;
    try {
      createRequire(import.meta.url).resolve("pi-subagents", { paths: [globalRoot] });
      return true;
    } catch { /* not there */ }
  } catch { /* npm not on PATH or timed out */ }

  return false;
}

export default async function (pi: ExtensionAPI): Promise<void> {
  // Allow consensus operations to nest one level deeper than pi-subagents'
  // default (parent → consensus group → judge). Setting it here as a process
  // env var means pi-subagents' child spawn picks it up automatically.
  if (!process.env.PI_SUBAGENT_MAX_DEPTH) {
    process.env.PI_SUBAGENT_MAX_DEPTH = "3";
  }

  installAbortBridges(pi);
  registerClkTools(pi);

  pi.on("session_start", async (_event, ctx) => {
    reset();
    if (!piSubagentsInstalled(ctx.cwd)) {
      ctx.ui.notify(
        "CLK requires the pi-subagents extension, which provides the `subagent` tool. Install it with: pi install npm:pi-subagents",
        "warning",
      );
    }
    await loadFromFiles(ctx.cwd);
    const s = getState();
    if (s.idea) {
      ctx.ui.setStatus("clk-idea", `idea: ${s.idea.slice(0, 60)}`);
    }
    if (s.roster) {
      ctx.ui.setStatus(
        "clk-roster",
        `roster: ${s.roster.agents.map((a) => a.name).join(", ")}`,
      );
    }
    if (await isDone(ctx.cwd)) {
      ctx.ui.setStatus("clk-done", `done: ${s.doneReason ?? "marked"}`);
    }
  });

  pi.registerCommand("clk", {
    description:
      "Cognitive Loop Kernel: cast a team and drive an idea to a working " +
      "system through dispatch + consensus + Ralph + autoresearch.",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const idea = (args ?? "").trim();
      if (!idea) {
        ctx.ui.notify(
          "Usage: /clk <one-line idea>. Example: /clk a local-first journaling app that summarises my week",
          "warning",
        );
        return;
      }

      let ctrl: AbortController;
      try {
        ctrl = startRun();
      } catch (err) {
        ctx.ui.notify(String((err as Error).message), "error");
        return;
      }

      try {
        await ensureRepo(ctx.cwd);
        await setIdea(ctx.cwd, idea, pi);
        await appendProgress(
          ctx.cwd,
          { kind: "note", message: `idea captured: ${idea}` },
          pi,
        );
        ctx.ui.setStatus("clk-idea", `idea: ${idea.slice(0, 60)}`);
        ctx.ui.setStatus("clk-run", "active");
        ctx.ui.notify(
          "CLK run started. The chief is taking over. Esc cancels the current turn; /clk-abort ends the run.",
          "info",
        );

        // Don't stomp on an in-flight chief turn from a previous /clk.
        await ctx.waitForIdle();
        if (ctrl.signal.aborted) return;

        // Hand off to the chief LLM. Wrap with retry so transient provider
        // errors (rate limits, network blips) don't abort the run.
        const sig = activeSignal();
        await withRetry(
          () => pi.sendUserMessage(clkChiefPrimer(idea)),
          {
            signal: sig,
            // Free-tier upstream rate limits can persist for 30–120 s; use a
            // 15 s base so the four attempts span ~3.5 min (15→30→60→120 s).
            maxAttempts: 4,
            baseDelayMs: 15000,
            onRetry: (err, attempt, delayMs) => {
              const cls = classifyError(err);
              ctx.ui.notify(
                `CLK: provider error (${cls}) on attempt ${attempt} — retrying in ${delayMs / 1000}s. ${recoveryHint(cls)}`,
                "warning",
              );
            },
          },
        );
      } catch (err) {
        if ((err as Error)?.message === "Aborted" || (err as Error)?.message?.includes("Aborted")) {
          // User cancelled — don't mark as errored, state is preserved for resume.
          return;
        }
        const cls = classifyError(err);
        const hint = recoveryHint(cls);
        endRun(`error: ${(err as Error).message}`);
        ctx.ui.setStatus("clk-run", "errored");
        ctx.ui.notify(`/clk failed (${cls}): ${(err as Error).message}. ${hint}`, "error");
      }
    },
  });

  // Tear down the run lifecycle when the chief signals completion. agent_end
  // fires once per user prompt, but we only end on the turn that actually
  // wrote done.md, which is also the turn that called endRun() inside
  // clk_done — so this is mostly a safety net for the file-side check.
  pi.on("agent_end", async (_event, ctx) => {
    if (await isDone(ctx.cwd)) {
      endRun("done.md observed");
      ctx.ui.setStatus("clk-run", "done");
    }
  });
}
