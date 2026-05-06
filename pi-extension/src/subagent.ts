import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import { existsSync } from "node:fs";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { Type } from "typebox";
import { activeSignal, mergeSignals } from "./abort.js";
import { classifyError, recoveryHint } from "./errors.js";

const execFileAsync = promisify(execFile);

const SUBAGENT_TIMEOUT_MS = 30 * 60 * 1000;
const POLL_INTERVAL_MS = 2000;

const MODEL_MAP: Record<string, string> = {
  "claude-opus": "anthropic/claude-opus-4-5",
  "claude-sonnet": "anthropic/claude-sonnet-4-5",
  "claude-haiku": "anthropic/claude-haiku-3-5",
  "gpt-4o": "openai/gpt-4o",
  "gpt-4o-mini": "openai/gpt-4o-mini",
};

const activeSessions = new Set<string>();

export async function tmuxAvailable(): Promise<boolean> {
  try {
    await execFileAsync("tmux", ["-V"]);
    return true;
  } catch {
    return false;
  }
}

/**
 * Resolve the pi binary: prefer PATH, fall back to project-local install.
 * Mirrors the logic in clk_harness/providers/pi.py _resolve_cmd().
 */
async function resolvePI(cwd: string): Promise<string | null> {
  try {
    const { stdout } = await execFileAsync("which", ["pi"]);
    const p = stdout.trim();
    if (p) return p;
  } catch { /* not on PATH */ }

  const local = join(cwd, ".clk", "tools", "pi", "bin", "pi");
  if (existsSync(local)) return resolve(local);
  return null;
}

function resolveModel(preferredModel?: string): string[] {
  if (!preferredModel) return [];
  const mapped = MODEL_MAP[preferredModel] ?? (preferredModel.includes("/") ? preferredModel : null);
  return mapped ? ["--model", mapped] : [];
}

export async function killAllSubagentSessions(): Promise<void> {
  const ids = [...activeSessions];
  activeSessions.clear();
  await Promise.allSettled(
    ids.map((id) => execFileAsync("tmux", ["kill-session", "-t", id]).catch(() => {})),
  );
}

interface SpawnOptions {
  agent: string;
  task: string;
  preferredModel?: string;
  cwd: string;
  signal?: AbortSignal;
  onUpdate?: (text: string) => void;
}

async function spawnSubagent(opts: SpawnOptions): Promise<string> {
  const sessionId = `clk-${randomUUID().slice(0, 8)}`;
  const dirPath = join(opts.cwd, ".clk", "subagents", sessionId);
  const taskPath = resolve(join(dirPath, "task.md"));
  const stdoutPath = resolve(join(dirPath, "stdout.txt"));

  await mkdir(dirPath, { recursive: true });

  // Prepend depth-cap notice so child pi sessions don't recursively spawn.
  const taskContent =
    `NOTE: You are a subagent dispatched by the CLK chief. Do not spawn further subagents.\n` +
    `Role: ${opts.agent}\n\n${opts.task}`;
  await writeFile(taskPath, taskContent, "utf8");

  const piBin = await resolvePI(opts.cwd);
  if (!piBin) {
    await rm(dirPath, { recursive: true, force: true });
    throw new Error("pi binary not found on PATH or at .clk/tools/pi/bin/pi");
  }

  const modelArgs = resolveModel(opts.preferredModel);
  // Build the shell command with single-quoted paths to handle spaces.
  const safeTask = taskPath.replace(/'/g, "'\\''");
  const safeOut = stdoutPath.replace(/'/g, "'\\''");
  const safePi = piBin.replace(/'/g, "'\\''");
  const modelStr = modelArgs.map((a) => `'${a.replace(/'/g, "'\\''")}'`).join(" ");
  const shellCmd = modelStr
    ? `'${safePi}' ${modelStr} --print < '${safeTask}' > '${safeOut}' 2>&1`
    : `'${safePi}' --print < '${safeTask}' > '${safeOut}' 2>&1`;

  // Attempt session creation; retry once on name collision.
  for (let attempt = 0; attempt < 2; attempt++) {
    try {
      await execFileAsync("tmux", ["new-session", "-d", "-s", sessionId, "-c", opts.cwd, "sh", "-c", shellCmd]);
      break;
    } catch (err) {
      if (attempt === 0 && String(err).includes("duplicate session")) continue;
      await rm(dirPath, { recursive: true, force: true });
      throw err;
    }
  }

  activeSessions.add(sessionId);

  const cleanup = async () => {
    activeSessions.delete(sessionId);
    try { await execFileAsync("tmux", ["kill-session", "-t", sessionId]); } catch { /* already gone */ }
    try { await rm(dirPath, { recursive: true, force: true }); } catch { /* best-effort */ }
  };

  return new Promise<string>((resolve, reject) => {
    const startMs = Date.now();
    let pollCount = 0;

    const onAbort = () => {
      clearInterval(timer);
      cleanup().then(() => reject(new Error("Aborted"))).catch(() => reject(new Error("Aborted")));
    };

    if (opts.signal?.aborted) { onAbort(); return; }
    opts.signal?.addEventListener("abort", onAbort, { once: true });

    const timer = setInterval(async () => {
      pollCount++;

      if (Date.now() - startMs > SUBAGENT_TIMEOUT_MS) {
        clearInterval(timer);
        opts.signal?.removeEventListener("abort", onAbort);
        await cleanup();
        reject(new Error(`subagent ${sessionId} timed out after ${SUBAGENT_TIMEOUT_MS / 60000} minutes`));
        return;
      }

      try {
        await execFileAsync("tmux", ["has-session", "-t", sessionId]);
        // Still running — emit a progress ping every 10 polls (~20 s).
        if (pollCount % 10 === 0) {
          const elapsed = Math.round((Date.now() - startMs) / 1000);
          opts.onUpdate?.(`subagent ${sessionId} (${opts.agent}) still running — ${elapsed}s elapsed`);
        }
      } catch {
        // Session exited.
        clearInterval(timer);
        opts.signal?.removeEventListener("abort", onAbort);
        let text = "";
        try { text = await readFile(stdoutPath, "utf8"); } catch { /* no output produced */ }
        await cleanup();
        resolve(text);
      }
    }, POLL_INTERVAL_MS);
  });
}

export function registerSubagentTool(pi: ExtensionAPI): void {
  pi.registerTool({
    name: "clk_subagent",
    label: "CLK Subagent",
    description:
      "Spawn a subagent as a background tmux pi session. The agent label is a role identifier " +
      "for traceability; the full persona must be embedded in the task string. " +
      "Multiple sibling calls in the same message run concurrently.",
    promptSnippet: "Dispatch a task to a background tmux pi subagent session.",
    parameters: Type.Object({
      agent: Type.String({
        description:
          "Short role label (e.g. 'worker', 'researcher', 'scout'). " +
          "Include the full persona in the task field.",
      }),
      task: Type.String({
        description: "Complete task description, including any role persona and context.",
      }),
      preferredModel: Type.Optional(
        Type.String({
          description:
            "Short model alias (claude-opus, claude-sonnet, claude-haiku, gpt-4o, gpt-4o-mini) " +
            "or a full provider/model string. Omit to use pi's default.",
        }),
      ),
    }),
    async execute(_id, params, signal, onUpdate, ctx) {
      if (!(await tmuxAvailable())) {
        return {
          content: [{
            type: "text",
            text: "subagent unavailable: tmux is not installed. Install it with: brew install tmux / apt install tmux",
          }],
          details: {},
        };
      }

      const sig = mergeSignals(signal, activeSignal());
      try {
        const result = await spawnSubagent({
          agent: params.agent,
          task: params.task,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          // Wrap the plain-string progress message into the ToolResult shape
          // Pi's onUpdate callback expects ({ content: [...] }).
          onUpdate: (text) => onUpdate({ content: [{ type: "text", text }] }),
        });
        return {
          content: [{ type: "text", text: result || "(subagent produced no output)" }],
          details: { agent: params.agent, sessionId: "completed" },
        };
      } catch (err) {
        const cls = classifyError(err);
        return {
          content: [{
            type: "text",
            text: `subagent failed (${cls}): ${(err as Error).message}. ${recoveryHint(cls)}`,
          }],
          details: { error: String(err) },
        };
      }
    },
  });
}
