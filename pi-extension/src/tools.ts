import { Type } from "typebox";
import { StringEnum } from "@mariozechner/pi-ai";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { setRoster, appendProgress, markDone } from "./state.js";
import { checkpoint, revertTo } from "./git.js";
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
      "Always call clk_cast before the first subagent dispatch and any time the project's needs change.",
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
              "Multi-line persona to prepend when dispatching this role via the subagent tool.",
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
              `Dispatch via the subagent tool, prefixing each task with the role's persona and mission.`,
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
      "Hard-reset the working tree to a previous SHA. Use this when validation regresses " +
      "after a dispatch — the harness never silently keeps broken work.",
    promptSnippet: "Hard-revert to a SHA returned by an earlier clk_checkpoint.",
    parameters: Type.Object({
      sha: Type.String(),
      reason: Type.String(),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      if (looksRedacted(params.sha)) {
        return {
          content: [{ type: "text", text: `clk_revert skipped: 'sha' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      try {
        await withRetry(() => revertTo(ctx.cwd, params.sha, sig), { signal: sig });
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_revert failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      await appendProgress(
        ctx.cwd,
        { kind: "revert", message: `${params.reason} → ${params.sha.slice(0, 8)}` },
        pi,
      );
      ctx.ui.setStatus("clk-head", `HEAD: ${params.sha.slice(0, 8)} (reverted)`);
      return {
        content: [{ type: "text", text: `reverted to ${params.sha}` }],
        details: {},
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
