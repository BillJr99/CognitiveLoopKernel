import type {
  ExtensionAPI,
  ExtensionCommandContext,
} from "@earendil-works/pi-coding-agent";
import { execFile } from "node:child_process";
import { access } from "node:fs/promises";
import { join } from "node:path";
import { promisify } from "node:util";
import {
  loadFromFiles,
  reset,
  getState,
  setIdea,
  appendProgress,
  isDone,
} from "./state.js";
import { ensureRepo, commitsAhead, hasRemote } from "./git.js";
import { clkChiefPrimer } from "./prompts.js";
import { registerClkTools } from "./tools.js";
import { registerSubagentTool, tmuxAvailable } from "./subagent.js";
import { startRun, endRun, installAbortBridges, activeSignal } from "./abort.js";
import { classifyError, recoveryHint, withRetry } from "./errors.js";

const execFileAsync = promisify(execFile);

/**
 * Return the first non-empty line of `s`, trimmed and truncated to `max`
 * characters. Used for status-bar labels where a multi-line idea (or
 * objective) would otherwise leak a fragment of line 2 into the status
 * display — the same bug the Python TUI fixed in commit 24f379b.
 */
export function firstLineShort(s: string, max = 60): string {
  const line = s.split("\n").find((l) => l.trim());
  return (line ?? s).trim().slice(0, max);
}

export default async function (pi: ExtensionAPI): Promise<void> {
  installAbortBridges(pi);
  registerClkTools(pi);
  registerSubagentTool(pi);

  pi.on("session_start", async (_event, ctx) => {
    reset();
    if (!(await tmuxAvailable())) {
      ctx.ui.notify(
        "CLK requires tmux to spawn subagent sessions. Install it with: brew install tmux / apt install tmux",
        "warning",
      );
    }
    await loadFromFiles(ctx.cwd);
    const s = getState();
    // First-run welcome — keyed off the absence of any captured idea.
    // Tells the user what /clk does and where the safety nets live, so
    // they're never staring at a blank Pi prompt wondering "now what?".
    if (!s.idea) {
      ctx.ui.notify(
        "CLK is loaded. Type `/clk <your idea>` to start a run, or " +
          "`/clk-help` for the full command list. Commits are auto-checkpointed; " +
          "a pre-push hook scans for API keys before they leave your machine.",
        "info",
      );
    } else {
      ctx.ui.setStatus("clk-idea", `idea: ${firstLineShort(s.idea)}`);
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

  // /clk-help — the empowerment command. Lists every CLK command and
  // its purpose so the user always knows the next move.
  pi.registerCommand("clk-help", {
    description: "List every CLK command and its purpose.",
    handler: async (_args: string, ctx: ExtensionCommandContext) => {
      const lines = [
        "CLK commands inside Pi:",
        "  /clk <idea>      Start a new CLK run. The chief casts a team and",
        "                   drives it to a working implementation via dispatch,",
        "                   consensus, Ralph refinement, and autoresearch.",
        "  /clk-abort       End the current run. Preserves state for resume.",
        "  /clk-help        Show this list.",
        "  /clk-doctor      Health-check tmux + git + workspace state.",
        "  /clk-undo        Preview the last CLK commit; `/clk-undo confirm`",
        "                   creates a new revert commit on top of it.",
        "",
        "Safety nets active in this workspace:",
        "  - Hardened .gitignore blocks .env / .env.bak / *.pem / id_rsa.",
        "  - .git/hooks/pre-push aborts pushes containing API-key patterns.",
        "  - .clk/state/*.{json,md} are written atomically with .bak rotation.",
        "  - Each completed iteration is checkpointed with `git commit`.",
        "",
        "Re-read this anytime with /clk-help. If something looks stuck, the",
        "agent_end hook will report it; `/clk-doctor` triages provider and",
        "tooling problems independently.",
      ];
      ctx.ui.notify(lines.join("\n"), "info");
    },
  });

  // /clk-undo — revert the last CLK-authored commit. Two-step (preview
  // then `/clk-undo confirm`) so it's never accidental. Same UX shape
  // as the Python TUI's /undo command.
  pi.registerCommand("clk-undo", {
    description: "Revert the last CLK commit. `/clk-undo confirm` actually reverts.",
    handler: async (args: string, ctx: ExtensionCommandContext) => {
      const confirm = (args ?? "").trim().toLowerCase() === "confirm";
      try {
        // Refuse if there are uncommitted changes so the revert doesn't
        // lose in-progress work.
        const { stdout: statusOut } = await execFileAsync(
          "git", ["status", "--porcelain"], { cwd: ctx.cwd },
        );
        if (statusOut.trim()) {
          ctx.ui.notify(
            "/clk-undo refused: there are uncommitted changes. " +
              "Commit or stash them first.",
            "warning",
          );
          return;
        }
        const { stdout: head } = await execFileAsync(
          "git", ["log", "-1", "--stat"], { cwd: ctx.cwd },
        );
        if (!confirm) {
          ctx.ui.notify(
            "Last commit (HEAD):\n" + head.split("\n").slice(0, 30).join("\n") +
              "\n\nType `/clk-undo confirm` to revert this commit (creates a new revert commit).",
            "info",
          );
          return;
        }
        await execFileAsync(
          "git", ["revert", "--no-edit", "HEAD"], { cwd: ctx.cwd },
        );
        ctx.ui.notify("/clk-undo: HEAD reverted with a new commit.", "info");
      } catch (err) {
        ctx.ui.notify(`/clk-undo failed: ${(err as Error).message}`, "error");
      }
    },
  });

  // /clk-doctor — quick triage. Checks the conditions CLK needs to work:
  // tmux on PATH, a git repo, the .clk/ layout, and any captured state.
  pi.registerCommand("clk-doctor", {
    description: "Health-check tmux + git + workspace state.",
    handler: async (_args: string, ctx: ExtensionCommandContext) => {
      const findings: string[] = [];
      async function checkBin(name: string): Promise<boolean> {
        try {
          await execFileAsync("command", ["-v", name], { shell: "/bin/bash" });
          return true;
        } catch {
          try {
            await execFileAsync(name, ["--version"]);
            return true;
          } catch {
            return false;
          }
        }
      }
      async function fileOk(path: string): Promise<boolean> {
        try {
          await access(path);
          return true;
        } catch {
          return false;
        }
      }

      const tmuxOk = await tmuxAvailable();
      findings.push(
        tmuxOk
          ? "  ✓ ok    tmux available"
          : "  ✗ fail  tmux NOT installed (install: brew install tmux / apt install tmux)",
      );
      const gitOk = await checkBin("git");
      findings.push(
        gitOk
          ? "  ✓ ok    git available"
          : "  ✗ fail  git NOT installed",
      );
      const repoOk = await fileOk(join(ctx.cwd, ".git"));
      findings.push(
        repoOk
          ? "  ✓ ok    cwd is a git repo"
          : "  ! warn  cwd is NOT a git repo (CLK will git init on the first /clk)",
      );
      const clkOk = await fileOk(join(ctx.cwd, ".clk", "state"));
      findings.push(
        clkOk
          ? "  ✓ ok    .clk/state/ exists"
          : "  ! warn  .clk/state/ missing (will be created on the first /clk)",
      );
      const ignoreOk = await fileOk(join(ctx.cwd, ".gitignore"));
      findings.push(
        ignoreOk
          ? "  ✓ ok    .gitignore exists"
          : "  ! warn  .gitignore missing (will be written on first /clk)",
      );
      const hookOk = await fileOk(join(ctx.cwd, ".git", "hooks", "pre-push"));
      findings.push(
        hookOk
          ? "  ✓ ok    pre-push secret scanner installed"
          : "  ! warn  pre-push hook missing (will be installed on first /clk)",
      );

      const idea = getState().idea;
      findings.push(idea ? `  ✓ ok    idea: ${firstLineShort(idea)}` : "  - info  no idea captured yet");

      // Unpushed-commits check — mirrors the Python TUI's ahead counter
      // so the user knows when local checkpoints haven't reached origin.
      if (repoOk && await hasRemote(ctx.cwd)) {
        const ahead = await commitsAhead(ctx.cwd);
        if (ahead > 0) {
          findings.push(`  ! warn  ${ahead} commit(s) ahead of origin (auto-push only fires when CLK_GITHUB_PUSH_ON_COMMIT=true)`);
        } else {
          findings.push("  ✓ ok    in sync with origin");
        }
      }

      ctx.ui.notify(["CLK doctor:", ...findings].join("\n"), "info");
    },
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
        ctx.ui.setStatus("clk-idea", `idea: ${firstLineShort(idea)}`);
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
          // pi.sendUserMessage returns void (it just enqueues the message
          // onto Pi's turn queue), so we wrap it in an async fn that
          // returns Promise<void>. withRetry's type parameter is satisfied,
          // and any synchronous throw from the enqueue path still
          // triggers a retry.
          async () => { pi.sendUserMessage(clkChiefPrimer(idea)); },
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
