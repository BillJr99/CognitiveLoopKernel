import { Type } from "typebox";
import { StringEnum } from "@earendil-works/pi-ai";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import {
  setRoster,
  appendProgress,
  markDone,
  setHomeBranch,
  getHomeBranch,
  recordRalphOutcome,
  consecutiveRalphReverts,
} from "./state.js";
import { runValidation, runValidations } from "./validate.js";
import {
  checkpoint,
  head,
  abortMerge,
  currentBranch,
  createAndCheckoutBranch,
  checkoutBranch,
  mergeBranch,
  saveAndSwitch,
  commitsAhead,
  hasRemote,
  pushBestEffort,
} from "./git.js";
import { activeSignal, mergeSignals, endRun } from "./abort.js";
import { classifyError, looksRedacted, recoveryHint, withRetry } from "./errors.js";
import { dispatchWithQuality, runConsensus } from "./consensus.js";
import { tmuxAvailable } from "./subagent.js";
import { summarise } from "./quality.js";

/**
 * Push the latest commit to `origin` when the user opted in via
 * `CLK_GITHUB_PUSH_ON_COMMIT=true` (same env var as the Python TUI). On
 * success, updates the clk-git status to "synced". On failure (or when
 * push isn't enabled but a remote exists), surfaces an `↑N` ahead count
 * so the user knows how many local checkpoints haven't reached origin.
 * Best-effort throughout — never throws.
 */
async function pushIfEnabled(
  cwd: string,
  setStatus: (key: string, value: string) => void,
  signal?: AbortSignal,
): Promise<void> {
  try {
    if (!(await hasRemote(cwd, "origin", signal))) return;
    const pushOn = (process.env.CLK_GITHUB_PUSH_ON_COMMIT ?? "false").toLowerCase() === "true";
    if (pushOn) {
      const res = await pushBestEffort(cwd, "origin", undefined, signal);
      if (res.pushed) {
        setStatus("clk-git", "synced");
        return;
      }
      const ahead = await commitsAhead(cwd, signal);
      setStatus("clk-git", `↑${ahead} (push failed: ${res.reason ?? "unknown"})`);
      return;
    }
    const ahead = await commitsAhead(cwd, signal);
    if (ahead > 0) {
      setStatus("clk-git", `↑${ahead} unpushed (set CLK_GITHUB_PUSH_ON_COMMIT=true to auto-push)`);
    }
  } catch {
    /* best-effort — never block the tool result on push bookkeeping. */
  }
}

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
        await pushIfEnabled(ctx.cwd, ctx.ui.setStatus.bind(ctx.ui), sig);
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
      if (abandonedBranch.startsWith("ralph/")) {
        await recordRalphOutcome(ctx.cwd, abandonedBranch, "reverted", pi);
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
      "iteration. Commits any pending changes first, then merges and switches back to home. " +
      "Pass `validate` (a shell command, e.g. the test suite) and the merge is REFUSED unless " +
      "it exits 0 — the code-enforced quality gate. Always pass it when the project has tests.",
    promptSnippet:
      "Merge accepted feature branch into home branch; `validate` runs first and a non-zero exit refuses the merge.",
    parameters: Type.Object({
      message: Type.String({ description: "Commit message describing the accepted improvement." }),
      validate: Type.Optional(
        Type.String({
          description:
            "Shell command that must exit 0 for the merge to proceed (e.g. 'npm test', 'pytest -q'). " +
            "Runs on the feature branch after the checkpoint commit.",
        }),
      ),
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
        if (params.validate) {
          const v = await runValidation(ctx.cwd, params.validate, { signal: sig });
          if (!v.ok) {
            return {
              content: [{
                type: "text",
                text:
                  `clk_merge REFUSED: validation command failed (exit ${v.exitCode ?? "?"}).\n` +
                  `  $ ${v.command}\n${v.output}\n\n` +
                  `Still on feature branch ${featureBranch}. Fix the failure and call clk_merge ` +
                  "again, or abandon the iteration with clk_revert.",
              }],
              details: { validationFailed: true, exitCode: v.exitCode, featureBranch },
            };
          }
        }
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
      if (featureBranch.startsWith("ralph/")) {
        await recordRalphOutcome(ctx.cwd, featureBranch, "merged", pi);
      }
      await appendProgress(
        ctx.cwd,
        { kind: "merge", message: `merged ${featureBranch} → ${home}: ${params.message}` },
        pi,
      );
      ctx.ui.setStatus("clk-branch", `merged → ${home}`);
      if (mergeHead) ctx.ui.setStatus("clk-head", `HEAD: ${mergeHead.slice(0, 8)}`);
      await pushIfEnabled(ctx.cwd, ctx.ui.setStatus.bind(ctx.ui), sig);
      return {
        content: [{ type: "text", text: `merged ${featureBranch} into ${home}` }],
        details: { featureBranch, home, mergeHead },
      };
    },
  });

  // ---------------------------------------------------------------------
  // clk_consensus — stochastic auto-consensus fan-out
  // ---------------------------------------------------------------------
  pi.registerTool({
    name: "clk_consensus",
    label: "CLK Consensus",
    description:
      "Fan-out N parallel subagent samples for the SAME task; score each via the harness's " +
      "quality detector and return the highest-scoring one (plus all candidates for traceability). " +
      "Use this instead of clk_subagent whenever an answer is high-stakes (a design choice, a " +
      "validation verdict, a non-trivial code edit), or whenever the chief is uncertain. Default " +
      "samples=3; clamp 1..6.",
    promptSnippet:
      "Fan-out N stochastic samples for one task; quality-scored winner returned. " +
      "Use liberally for high-stakes or uncertain dispatches.",
    parameters: Type.Object({
      agent: Type.String({
        description: "Short role label (e.g. 'engineer', 'designer'). Embed the full persona in the task.",
      }),
      task: Type.String({
        description: "Complete task description, including role persona and context.",
      }),
      samples: Type.Optional(
        Type.Integer({ minimum: 1, maximum: 6, description: "How many samples to draw. Default 3." }),
      ),
      preferredModel: Type.Optional(
        Type.String({
          description:
            "Short alias (claude-opus, claude-sonnet, claude-haiku, gpt-4o, gpt-4o-mini) " +
            "or a provider/model string. Omit to use pi's default.",
        }),
      ),
      minChars: Type.Optional(
        Type.Integer({ minimum: 0, description: "Override minimum-response-length flag threshold (default 40)." }),
      ),
      expectedOutputs: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Outputs-contract keys each sample must list in a POST block's PRODUCES line. " +
            "Samples missing any key are scored down.",
        }),
      ),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      if (signal?.aborted || activeSignal()?.aborted) {
        return { content: [{ type: "text", text: "clk_consensus cancelled before start." }], details: {} };
      }
      if (!(await tmuxAvailable())) {
        return {
          content: [{
            type: "text",
            text: "clk_consensus unavailable: tmux is not installed. Install it with: brew install tmux / apt install tmux",
          }],
          details: {},
        };
      }
      if (looksRedacted(params.task)) {
        return {
          content: [{ type: "text", text: `clk_consensus skipped: 'task' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      const samples = Math.max(1, Math.min(6, params.samples ?? 3));
      try {
        const result = await runConsensus({
          agent: params.agent,
          task: params.task,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          samples,
          scoreOpts: {
            ...(params.minChars !== undefined ? { minChars: params.minChars } : {}),
            ...(params.expectedOutputs?.length ? { expectedOutputs: params.expectedOutputs } : {}),
          },
          onSample: (idx, message) =>
            onUpdate?.({
              content: [{ type: "text", text: `[consensus #${idx}] ${message}` }],
              details: {},
            }),
        });
        await appendProgress(
          ctx.cwd,
          {
            kind: "consensus",
            message: `${samples} samples for '${params.agent}': ${result.reason}`,
          },
          pi,
        );
        ctx.ui.setStatus("clk-last", `consensus: ${result.reason.slice(0, 80)}`);
        const recap = result.all
          .map((s) =>
            s.error
              ? `  #${s.index} error: ${s.error}`
              : `  #${s.index} score=${s.quality.score.toFixed(2)} ` +
                `(${summarise(s.quality)}) sessionId=${s.sessionId}`,
          )
          .join("\n");
        const body =
          `Consensus winner (sample #${result.best.index}, score ${result.best.quality.score.toFixed(2)}):\n\n` +
          (result.best.output || "(winner produced no output)") +
          `\n\n---\nAll samples:\n${recap}`;
        return {
          content: [{ type: "text", text: body }],
          details: {
            samples,
            winnerIndex: result.best.index,
            winnerScore: result.best.quality.score,
            allScores: result.all.map((s) => ({ index: s.index, score: s.quality.score, flags: s.quality.flags })),
          },
        };
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_consensus failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // ---------------------------------------------------------------------
  // clk_subagent_quality — single subagent + quality re-dispatch loop
  // ---------------------------------------------------------------------
  pi.registerTool({
    name: "clk_subagent_quality",
    label: "CLK Subagent (quality-validated)",
    description:
      "Dispatch ONE subagent and gate its output through the quality detector. On a recoverable " +
      "failure (empty / malformed / low-confidence), re-runs with a repair preamble up to " +
      "`maxRetries` extra times. Cheaper than clk_consensus when the task is simple but you still " +
      "want a quality gate. Default maxRetries=1.",
    promptSnippet: "Single subagent dispatch with automatic quality scoring + repair-preamble re-rolls.",
    parameters: Type.Object({
      agent: Type.String({ description: "Short role label." }),
      task: Type.String({ description: "Complete task description, including persona." }),
      preferredModel: Type.Optional(Type.String()),
      maxRetries: Type.Optional(
        Type.Integer({ minimum: 0, maximum: 4, description: "Extra dispatches on quality failures. Default 1." }),
      ),
      minChars: Type.Optional(Type.Integer({ minimum: 0 })),
      expectedOutputs: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Outputs-contract keys the response must list in a POST block's PRODUCES line; " +
            "missing keys trigger the repair re-roll with a concrete POST example.",
        }),
      ),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      if (signal?.aborted || activeSignal()?.aborted) {
        return { content: [{ type: "text", text: "clk_subagent_quality cancelled before start." }], details: {} };
      }
      if (!(await tmuxAvailable())) {
        return {
          content: [{ type: "text", text: "tmux not installed; cannot dispatch." }],
          details: {},
        };
      }
      if (looksRedacted(params.task)) {
        return {
          content: [{ type: "text", text: `clk_subagent_quality skipped: 'task' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      try {
        const result = await dispatchWithQuality({
          agent: params.agent,
          task: params.task,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          maxRetries: params.maxRetries ?? 1,
          scoreOpts: {
            ...(params.minChars !== undefined ? { minChars: params.minChars } : {}),
            ...(params.expectedOutputs?.length ? { expectedOutputs: params.expectedOutputs } : {}),
          },
          onRetry: (attempt, q) =>
            onUpdate?.({
              content: [{
                type: "text",
                text: `quality retry ${attempt}: ${summarise(q)} — re-rolling with repair preamble`,
              }],
              details: {},
            }),
        });
        ctx.ui.setStatus("clk-last", `quality: ${summarise(result.quality)}`);
        const body =
          (result.output || "(subagent produced no output)") +
          `\n\n---\nquality: ${summarise(result.quality)} after ${result.attempts} attempt(s).`;
        return {
          content: [{ type: "text", text: body }],
          details: {
            attempts: result.attempts,
            score: result.quality.score,
            ok: result.quality.ok,
            flags: result.quality.flags,
            sessionId: result.sessionId,
          },
        };
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_subagent_quality failed: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
    },
  });

  // ---------------------------------------------------------------------
  // clk_autoresearch — survey → investigate → critique loop
  // ---------------------------------------------------------------------
  pi.registerTool({
    name: "clk_autoresearch",
    label: "CLK Autoresearch",
    description:
      "Karpathy-style autoresearch loop: spawn a researcher subagent to investigate the question, " +
      "then a critic subagent to review the finding. Repeat for `iterations` cycles. Each finding " +
      "and critique is appended to the progress log. Use BEFORE non-trivial implementation work to " +
      "ground the chief in real findings rather than priors.",
    promptSnippet:
      "Iteratively investigate an open question via researcher + critic subagents.",
    parameters: Type.Object({
      question: Type.String({ description: "The open question or hypothesis to investigate." }),
      iterations: Type.Optional(
        Type.Integer({ minimum: 1, maximum: 5, description: "Number of investigate-then-critique cycles. Default 2." }),
      ),
      preferredModel: Type.Optional(Type.String()),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      if (signal?.aborted || activeSignal()?.aborted) {
        return { content: [{ type: "text", text: "clk_autoresearch cancelled before start." }], details: {} };
      }
      if (!(await tmuxAvailable())) {
        return {
          content: [{ type: "text", text: "tmux not installed; cannot dispatch." }],
          details: {},
        };
      }
      if (looksRedacted(params.question)) {
        return {
          content: [{ type: "text", text: `clk_autoresearch skipped: 'question' appears redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      const iterations = Math.max(1, Math.min(5, params.iterations ?? 2));
      const log: Array<{ iteration: number; finding: string; critique: string; findingScore: number; critiqueScore: number }> = [];

      for (let i = 1; i <= iterations; i++) {
        if (sig?.aborted) break;
        onUpdate?.({
          content: [{ type: "text", text: `autoresearch #${i}/${iterations}: investigating` }],
          details: {},
        });
        const researcherTask =
          `You are a researcher dispatched for autoresearch iteration #${i}. ` +
          `Investigate this question deeply and report findings:\n\n${params.question}\n\n` +
          (log.length > 0
            ? `Prior findings so far:\n${log.map((l) => `[iter ${l.iteration}] ${l.finding.slice(0, 300)}`).join("\n\n")}\n\n`
            : "") +
          "Produce concrete findings (cite files, measurements, logs). " +
          "End your response with a single line: CONFIDENCE: <0..1>";
        const research = await dispatchWithQuality({
          agent: "researcher",
          task: researcherTask,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          maxRetries: 1,
        });
        if (sig?.aborted) break;
        onUpdate?.({
          content: [{ type: "text", text: `autoresearch #${i}/${iterations}: critiquing` }],
          details: {},
        });
        const criticTask =
          `You are a critic for autoresearch iteration #${i}. The researcher reported:\n\n` +
          (research.output || "(empty)") +
          `\n\nOriginal question:\n${params.question}\n\n` +
          "Identify gaps, weak evidence, contradicting facts. Be specific. " +
          "End with: CONFIDENCE: <0..1>";
        const critic = await dispatchWithQuality({
          agent: "critic",
          task: criticTask,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          maxRetries: 1,
        });
        log.push({
          iteration: i,
          finding: research.output,
          critique: critic.output,
          findingScore: research.quality.score,
          critiqueScore: critic.quality.score,
        });
        await appendProgress(
          ctx.cwd,
          {
            kind: "autoresearch",
            message:
              `iter ${i}: research score=${research.quality.score.toFixed(2)} ` +
              `critic score=${critic.quality.score.toFixed(2)}`,
          },
          pi,
        );
      }
      const body =
        `Autoresearch on: ${params.question}\n\n` +
        log.map((l) =>
          `=== iteration ${l.iteration} ===\n` +
          `FINDING (score ${l.findingScore.toFixed(2)}):\n${l.finding}\n\n` +
          `CRITIQUE (score ${l.critiqueScore.toFixed(2)}):\n${l.critique}`,
        ).join("\n\n");
      ctx.ui.setStatus("clk-last", `autoresearch: ${iterations} iter(s) on ${params.question.slice(0, 40)}`);
      return {
        content: [{ type: "text", text: body || "(autoresearch produced no iterations — aborted?)" }],
        details: {
          question: params.question,
          iterations: log.length,
          findings: log.map((l) => ({ iteration: l.iteration, findingScore: l.findingScore, critiqueScore: l.critiqueScore })),
        },
      };
    },
  });

  // ---------------------------------------------------------------------
  // clk_ralph — branch / dispatch / evaluate / commit-or-revert iteration
  // ---------------------------------------------------------------------
  pi.registerTool({
    name: "clk_ralph",
    label: "CLK Ralph Iteration",
    description:
      "One Ralph iteration: create a feature branch, dispatch a consensus fan-out of N samples, " +
      "let the chief inspect the winning output (returned to it), then EITHER keep the branch " +
      "(clk_merge) OR abandon it (clk_revert) based on the chief's verdict. The branch creation " +
      "and dispatch are enforced in code so the chief can't skip the Ralph protocol. The chief " +
      "still drives the accept/reject decision via subsequent clk_merge or clk_revert calls.",
    promptSnippet:
      "Branch + consensus dispatch one iteration; chief reviews winner and accepts via clk_merge or rejects via clk_revert.",
    parameters: Type.Object({
      iterationName: Type.String({
        description:
          "Short kebab-case branch suffix, e.g. 'iter-3-optimize-db'. Will be prefixed with 'ralph/'.",
      }),
      agent: Type.String({ description: "Role label for the dispatched worker." }),
      task: Type.String({ description: "Full task description for the worker, including persona." }),
      samples: Type.Optional(
        Type.Integer({ minimum: 1, maximum: 6, description: "Consensus samples per iteration. Default 3." }),
      ),
      preferredModel: Type.Optional(Type.String()),
      expectedOutputs: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Outputs-contract keys the worker must list in a POST block's PRODUCES line. " +
            "Declare them whenever the task produces a named artifact.",
        }),
      ),
      acknowledgePlateau: Type.Optional(
        Type.Boolean({
          description:
            "Required to proceed after 3+ consecutive reverted Ralph iterations. Set true ONLY " +
            "when the task is a qualitatively different approach, not another marginal tweak.",
        }),
      ),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      if (signal?.aborted || activeSignal()?.aborted) {
        return { content: [{ type: "text", text: "clk_ralph cancelled before start." }], details: {} };
      }
      // Plateau guard — same signal the Python harness derives from its
      // plateau_window: consecutive rejected iterations mean another
      // marginal tweak will likely fail too. Force an explicit reframe.
      const reverts = consecutiveRalphReverts();
      if (reverts >= 3 && !params.acknowledgePlateau) {
        return {
          content: [{
            type: "text",
            text:
              `clk_ralph PLATEAU: the last ${reverts} Ralph iterations were all reverted. ` +
              "Do NOT retry a variation of the same change. Choose a qualitatively different " +
              "approach (new design, different metric, clk_autoresearch on the blocker first), " +
              "then re-call clk_ralph with acknowledgePlateau: true and the new approach in the task.",
          }],
          details: { plateau: true, consecutiveReverts: reverts },
        };
      }
      if (!(await tmuxAvailable())) {
        return {
          content: [{ type: "text", text: "tmux not installed; cannot dispatch." }],
          details: {},
        };
      }
      if (looksRedacted(params.task) || looksRedacted(params.iterationName)) {
        return {
          content: [{ type: "text", text: `clk_ralph skipped: parameters appear redacted. ${recoveryHint("redaction")}` }],
          details: {},
        };
      }
      const sig = mergeSignals(signal, activeSignal());
      const branchName = params.iterationName.startsWith("ralph/")
        ? params.iterationName
        : `ralph/${params.iterationName}`;
      let home = getHomeBranch();
      try {
        if (!home) {
          home = await currentBranch(ctx.cwd, sig);
          await setHomeBranch(ctx.cwd, home, pi);
        }
        await withRetry(() => createAndCheckoutBranch(ctx.cwd, branchName, sig), { signal: sig });
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_ralph failed to create branch '${branchName}': ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err) },
        };
      }
      onUpdate?.({
        content: [{ type: "text", text: `ralph: on branch ${branchName}, dispatching ${params.samples ?? 3} samples` }],
        details: {},
      });

      try {
        const consensus = await runConsensus({
          agent: params.agent,
          task: params.task,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          samples: params.samples ?? 3,
          scoreOpts: params.expectedOutputs?.length
            ? { expectedOutputs: params.expectedOutputs }
            : {},
          onSample: (idx, message) =>
            onUpdate?.({
              content: [{ type: "text", text: `[ralph/${branchName} #${idx}] ${message}` }],
              details: {},
            }),
        });
        await appendProgress(
          ctx.cwd,
          {
            kind: "ralph",
            message: `iteration ${branchName}: ${consensus.reason}`,
          },
          pi,
        );
        ctx.ui.setStatus("clk-branch", `ralph: ${branchName}`);
        const body =
          `Ralph iteration on branch ${branchName} — home=${home}.\n\n` +
          `Winning sample (#${consensus.best.index}, score ${consensus.best.quality.score.toFixed(2)}):\n\n` +
          (consensus.best.output || "(no output)") +
          "\n\n---\nReview the winner above. If it advances the goal, accept it with " +
          "`clk_merge({message: '<summary>'})`. If it doesn't, abandon the branch with " +
          "`clk_revert({reason: '<why>'})` (the branch will be preserved for review).";
        return {
          content: [{ type: "text", text: body }],
          details: {
            branch: branchName,
            home,
            winnerIndex: consensus.best.index,
            winnerScore: consensus.best.quality.score,
            allScores: consensus.all.map((s) => ({ index: s.index, score: s.quality.score, flags: s.quality.flags })),
          },
        };
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{ type: "text", text: `clk_ralph dispatch failed on ${branchName}: ${(err as Error).message}. ${recoveryHint(cls)}` }],
          details: { error: String(err), branch: branchName },
        };
      }
    },
  });

  pi.registerTool({
    name: "clk_done",
    label: "CLK Done",
    description:
      "Mark the project complete. Writes .clk/state/done.md and ends the orchestration run. " +
      "Only call when the MVP runs, tests pass, README, deployment plan + checklist, and at " +
      "least one user-facing path all exist. Pass `validate` (shell commands, e.g. the test " +
      "suite) and completion is REFUSED while any of them exits non-zero — always pass it " +
      "when the project has tests so 'done' is provable, not asserted.",
    promptSnippet:
      "Mark the run complete; `validate` commands must all exit 0 or completion is refused.",
    parameters: Type.Object({
      reason: Type.String({ description: "One-line summary of why the run is complete." }),
      validate: Type.Optional(
        Type.Array(Type.String(), {
          description:
            "Shell commands that must ALL exit 0 for the run to be marked done " +
            "(e.g. ['npm test', 'npm run build']).",
        }),
      ),
    }),
    async execute(_id, params, signal, _onUpdate, ctx) {
      const sig = mergeSignals(signal, activeSignal());
      if (params.validate && params.validate.length > 0) {
        const { ok, results } = await runValidations(ctx.cwd, params.validate, { signal: sig });
        if (!ok) {
          const failed = results[results.length - 1]!;
          return {
            content: [{
              type: "text",
              text:
                `clk_done REFUSED: validation command failed (exit ${failed.exitCode ?? "?"}).\n` +
                `  $ ${failed.command}\n${failed.output}\n\n` +
                "The run is NOT done while validation fails. Fix the failure and keep iterating.",
            }],
            details: { validationFailed: true, command: failed.command, exitCode: failed.exitCode },
          };
        }
      }
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
