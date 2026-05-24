import { Type } from "typebox";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { setRoster, appendProgress, markDone, setHomeBranch, getHomeBranch } from "./state.js";
import {
  checkpoint,
  head,
  abortMerge,
  currentBranch,
  createAndCheckoutBranch,
  checkoutBranch,
  mergeBranch,
  saveAndSwitch,
} from "./git.js";
import { activeSignal, mergeSignals, endRun } from "./abort.js";
import { classifyError, looksRedacted, recoveryHint, withRetry } from "./errors.js";

export function registerClkTools(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "clk_cast",
    label: "CLK Cast",
    description:
      "Persist a roster of project-specific specialist roles. Call this before dispatching " +
      "and any time the team needs to change. Each role is a name + mission + system persona " +
      "the chief authored.",
    promptSnippet: "Persist the dynamically cast team roster.",
    promptGuidelines: [
      "Always call clk_cast before the first clk_subagent dispatch and any time the project's needs change.",
    ],
    parameters: Type.Object({
      reason: Type.String({ description: "Why this casting decision was made." }),
      agents: Type.Array(
        Type.Object({
          name: Type.String({
            description: "Short snake_case role name, e.g. 'data_steward'.",
          }),
          mission: Type.String({
            description: "One sentence describing what this role owns.",
          }),
          systemPersona: Type.String({
            description:
              "Multi-line persona to prepend when dispatching this role via the clk_subagent tool.",
          }),
          preferredModel: Type.Optional(Type.String()),
        }),
      ),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (looksRedacted(params.reason)) {
        return {
          content: [{ type: "text", text: `clk_cast skipped: the 'reason' field appears to have been redacted by a privacy filter. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const validAgents = params.agents.filter(
        (a) => !looksRedacted(a.name) && !looksRedacted(a.mission),
      );
      if (validAgents.length === 0) {
        return {
          content: [{ type: "text", text: `clk_cast skipped: all agent entries appear redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const roster = {
        agents: validAgents,
        castedAt: Date.now(),
        reason: params.reason,
      };
      await setRoster(ctx.cwd, roster, pi);
      await appendProgress(
        ctx.cwd,
        {
          kind: "cast",
          message: `cast ${validAgents.length} role(s): ${validAgents.map((a) => a.name).join(", ")} — ${params.reason}`,
        },
        pi,
      );
      ctx.ui.setStatus(
        "clk-roster",
        `roster: ${validAgents.map((a) => a.name).join(", ")}`,
      );
      const skipped = params.agents.length - validAgents.length;
      return {
        content: [
          {
            type: "text",
            text:
              `Roster persisted (${validAgents.length} role(s)${skipped > 0 ? `; ${skipped} skipped due to redaction` : ""}). ` +
              `Dispatch via the clk_subagent tool, prefixing each task with the role's persona and mission.`,
          },
        ],
        details: { roster },
      };
    },
  });

  pi.registerTool({
    name: "clk_progress",
    label: "CLK Progress",
    description:
      "Append a one-line entry to the CLK progress log. Use at every meaningful transition: " +
      "dispatch started, consensus reached, Ralph iteration complete, validation pass/fail.",
    promptSnippet: "Record a meaningful state transition in the CLK progress log.",
    parameters: Type.Object({
      kind: StringEnum([
        "dispatch",
        "consensus",
        "ralph",
        "autoresearch",
        "branch",
        "merge",
        "note",
      ] as const),
      message: Type.String(),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      if (looksRedacted(params.message)) {
        return {
          content: [{ type: "text", text: `clk_progress skipped: the 'message' field appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      await appendProgress(ctx.cwd, { kind: params.kind, message: params.message }, pi);
      ctx.ui.setStatus("clk-last", `${params.kind}: ${params.message.slice(0, 80)}`);
      return { content: [{ type: "text", text: "logged" }], details: {} };
    },
  });

  pi.registerTool({
    name: "clk_checkpoint",
    label: "CLK Checkpoint",
    description:
      "Stage all working-tree changes and create a git commit with the given message. Use after " +
      "any successful agent dispatch. Returns the new HEAD SHA so the chief can revert later.",
    promptSnippet: "Commit current changes; returns the new SHA for use with clk_revert.",
    parameters: Type.Object({
      message: Type.String({ description: "Commit message (CLK prefixes it with [clk])." }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (looksRedacted(params.message)) {
        return {
          content: [{ type: "text", text: `clk_checkpoint skipped: 'message' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      let sha: string | null;
      try {
        sha = await withRetry(() => checkpoint(ctx.cwd, `[clk] ${params.message}`, sig), { signal: sig });
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_checkpoint failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      if (sha) {
        await appendProgress(
          ctx.cwd,
          { kind: "checkpoint", message: `${params.message} @ ${sha.slice(0, 8)}` },
          pi,
        );
        ctx.ui.setStatus("clk-head", `HEAD: ${sha.slice(0, 8)}`);
      }
      return {
        content: [
          {
            type: "text",
            text: sha ? `committed ${sha}` : "no changes to commit",
          },
        ],
        details: { sha },
      };
    },
  });

  pi.registerTool({
    name: "clk_revert",
    label: "CLK Revert",
    description:
      "Abandon the current feature branch after a failed Ralph iteration. Commits any " +
      "uncommitted work to the current branch (preserving it for review), then switches back " +
      "to the home branch without merging. The rejected branch is kept intact — never deleted.",
    promptSnippet: "Abandon current feature branch and return to home branch; rejected work is preserved on its branch.",
    parameters: Type.Object({
      reason: Type.String({ description: "Why this iteration was rejected." }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (looksRedacted(params.reason)) {
        return {
          content: [{ type: "text", text: `clk_revert skipped: 'reason' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      const home = getHomeBranch();
      if (!home) {
        return {
          content: [{ type: "text", text: "clk_revert: no home branch recorded — call clk_branch first to create a feature branch." }],
          details: {},
        };
      }
      let abandonedBranch: string;
      try {
        abandonedBranch = await currentBranch(ctx.cwd, sig);
        if (abandonedBranch === home) {
          return {
            content: [{ type: "text", text: `clk_revert: already on home branch '${home}'; no feature branch to revert. Call clk_branch first.` }],
            details: {},
          };
        }
        await withRetry(
          () => saveAndSwitch(ctx.cwd, `[clk] rejected: ${params.reason}`, home, sig),
          { signal: sig },
        );
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_revert failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      await appendProgress(
        ctx.cwd,
        { kind: "revert", message: `rejected branch preserved: ${abandonedBranch} — ${params.reason}` },
        pi,
      );
      ctx.ui.setStatus("clk-head", `back on ${home} (${abandonedBranch} preserved)`);
      return {
        content: [{ type: "text", text: `switched back to ${home}; rejected work preserved on branch ${abandonedBranch}` }],
        details: { home, abandonedBranch },
      };
    },
  });

  pi.registerTool({
    name: "clk_branch",
    label: "CLK Branch",
    description:
      "Create a new feature branch for a Ralph iteration and switch to it. The current " +
      "(home) branch is recorded automatically. Call this at the start of every Ralph " +
      "iteration before dispatching work.",
    promptSnippet: "Create and switch to a feature branch for the current Ralph iteration.",
    parameters: Type.Object({
      name: Type.String({
        description:
          "Branch name, e.g. 'ralph/iter-3-optimize-db-queries'. Use lowercase kebab-case.",
      }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (looksRedacted(params.name)) {
        return {
          content: [{ type: "text", text: `clk_branch skipped: 'name' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      let home = getHomeBranch();
      try {
        if (!home) {
          home = await currentBranch(ctx.cwd, sig);
          await setHomeBranch(ctx.cwd, home, pi);
        }
        await withRetry(
          () => createAndCheckoutBranch(ctx.cwd, params.name, sig),
          { signal: sig },
        );
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_branch failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      await appendProgress(
        ctx.cwd,
        { kind: "branch", message: `created feature branch ${params.name} (home: ${home})` },
        pi,
      );
      ctx.ui.setStatus("clk-branch", `branch: ${params.name}`);
      return {
        content: [{ type: "text", text: `switched to new branch ${params.name} (home: ${home})` }],
        details: { branch: params.name, home },
      };
    },
  });

  pi.registerTool({
    name: "clk_merge",
    label: "CLK Merge",
    description:
      "Merge the current feature branch into the home branch after a successful Ralph " +
      "iteration. Commits any pending changes first, then merges and switches back to home.",
    promptSnippet: "Merge accepted feature branch into home branch; call after validation passes.",
    parameters: Type.Object({
      message: Type.String({ description: "Commit message describing the accepted improvement." }),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (looksRedacted(params.message)) {
        return {
          content: [{ type: "text", text: `clk_merge skipped: 'message' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      const home = getHomeBranch();
      if (!home) {
        return {
          content: [{ type: "text", text: "clk_merge: no home branch recorded — call clk_branch first." }],
          details: {},
        };
      }
      let featureBranch: string;
      let sha: string | null;
      try {
        featureBranch = await currentBranch(ctx.cwd, sig);
        if (featureBranch === home) {
          return {
            content: [{ type: "text", text: `clk_merge: already on home branch ${home}; nothing to merge.` }],
            details: {},
          };
        }
        sha = await withRetry(
          () => checkpoint(ctx.cwd, `[clk] ${params.message}`, sig),
          { signal: sig },
        );
        await withRetry(() => checkoutBranch(ctx.cwd, home, sig), { signal: sig });
        try {
          await withRetry(() => mergeBranch(ctx.cwd, featureBranch, sig), { signal: sig });
        } catch (mergeErr) {
          // Merge failed (e.g. conflict): abort and return to feature branch so the
          // repo is left in a clean, known state rather than mid-merge on home.
          try { await abortMerge(ctx.cwd, sig); } catch { /* best-effort */ }
          try { await checkoutBranch(ctx.cwd, featureBranch, sig); } catch { /* best-effort */ }
          const cls = classifyError(mergeErr);
          return {
            content: [{ type: "text", text: `clk_merge failed during merge: ${(mergeErr as Error).message}. Repo returned to feature branch ${featureBranch}. ${recoveryHint(cls)}` }],
            details: { error: String(mergeErr) },
          };
        }
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_merge failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      // Use post-merge HEAD so the displayed SHA reflects the merge commit.
      const mergeHead = await head(ctx.cwd, sig);
      await appendProgress(
        ctx.cwd,
        { kind: "merge", message: `merged ${featureBranch} → ${home}: ${params.message}` },
        pi,
      );
      ctx.ui.setStatus("clk-branch", `merged → ${home}`);
      if (mergeHead) ctx.ui.setStatus("clk-head", `HEAD: ${mergeHead.slice(0, 8)}`);
      return {
        content: [{ type: "text", text: `merged ${featureBranch} into ${home}` }],
        details: { featureBranch, home, mergeHead },
      };
    },
  });

  pi.registerTool({
    name: "clk_done",
    label: "CLK Done",
    description:
      "Mark the project complete. Writes .clk/state/done.md and ends the orchestration run. " +
      "Only call when the MVP runs, tests pass, README, deployment plan + checklist, and at " +
      "least one user-facing path all exist.",
    promptSnippet:
      "Mark the run complete; only when every completion criterion is satisfied.",
    parameters: Type.Object({
      reason: Type.String({ description: "One-line summary of why the run is complete." }),
    }),
    async execute(_id, params, _signal, _onUpdate, ctx) {
      await markDone(ctx.cwd, params.reason, pi);
      await appendProgress(ctx.cwd, { kind: "done", message: params.reason }, pi);
      ctx.ui.setStatus("clk-done", `done: ${params.reason}`);
      endRun("clk_done");
      return {
        content: [{ type: "text", text: `marked done: ${params.reason}` }],
        details: {},
        terminate: true,
      };
    },
  });
}
