import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { access, appendFile, mkdir, readFile, rm, writeFile } from "node:fs/promises";
import { constants } from "node:fs";
import { join, resolve } from "node:path";
import { randomUUID } from "node:crypto";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import { Type } from "typebox";
import { activeSignal, mergeSignals } from "./abort.js";
import { classifyError, recoveryHint } from "./errors.js";

const execFileAsync = promisify(execFile);

const SUBAGENT_TIMEOUT_MS = 30 * 60 * 1000;
const POLL_INTERVAL_MS = 2000;
// Pi's accounting system fails on very large tool results. Cap output here
// so the chief always receives a well-formed, accountable response.
const MAX_OUTPUT_CHARS = 80_000;
// How many characters of subagent stdout to retain in the session log for diagnostics.
const LOG_HEAD_CHARS = 2000;

const MODEL_MAP: Record<string, string> = {
  "claude-opus": "anthropic/claude-opus-4-5",
  "claude-sonnet": "anthropic/claude-sonnet-4-5",
  "claude-haiku": "anthropic/claude-haiku-3-5",
  "gpt-4o": "openai/gpt-4o",
  "gpt-4o-mini": "openai/gpt-4o-mini",
};

const activeSessions = new Set<string>();

async function writeLog(cwd: string, sessionId: string, lines: string[]): Promise<void> {
  try {
    const logDir = join(cwd, ".clk", "logs");
    await mkdir(logDir, { recursive: true });
    const logPath = join(logDir, `${sessionId}.log`);
    const ts = new Date().toISOString();
    await appendFile(logPath, lines.map((l) => `[${ts}] ${l}`).join("\n") + "\n", "utf8");
  } catch { /* logging must never crash the caller */ }
}

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
async function resolvePI(cwd: string): Promise<{ path: string } | { error: string }> {
  try {
    const { stdout } = await execFileAsync("which", ["pi"]);
    const p = stdout.trim();
    if (p) return { path: p };
  } catch { /* not on PATH */ }

  const local = join(cwd, ".clk", "tools", "pi", "bin", "pi");
  try {
    await access(local, constants.X_OK);
    return { path: resolve(local) };
  } catch (err) {
    const code = (err as NodeJS.ErrnoException).code;
    if (code === "EACCES" || code === "EPERM") {
      return { error: `pi binary at ${local} exists but is not executable — run: chmod +x ${local}` };
    }
  }
  return { error: "pi binary not found on PATH or at .clk/tools/pi/bin/pi" };
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

export interface SpawnOptions {
  agent: string;
  task: string;
  preferredModel?: string;
  cwd: string;
  signal?: AbortSignal;
  onUpdate?: (text: string) => void;
}

export async function spawnSubagent(opts: SpawnOptions): Promise<{ output: string; sessionId: string }> {
  const sessionId = `clk-${randomUUID().slice(0, 8)}`;
  const dirPath = join(opts.cwd, ".clk", "subagents", sessionId);
  const taskPath = resolve(join(dirPath, "task.md"));
  const stdoutPath = resolve(join(dirPath, "stdout.txt"));

  await mkdir(dirPath, { recursive: true });

  // Prepend depth-cap notice so child pi sessions don't recursively spawn
  // and don't call clk_* tools (prompt-level enforcement).
  const taskContent =
    `NOTE: You are a subagent dispatched by the CLK chief. ` +
    `Do not spawn further subagents and do not call any clk_* tools.\n` +
    `Role: ${opts.agent}\n\n${opts.task}`;
  await writeFile(taskPath, taskContent, "utf8");

  await writeLog(opts.cwd, sessionId, [`spawn agent=${opts.agent} model=${opts.preferredModel ?? "default"}`]);

  const piResult = await resolvePI(opts.cwd);
  if ("error" in piResult) {
    await rm(dirPath, { recursive: true, force: true });
    await writeLog(opts.cwd, sessionId, [`pi-resolve-error: ${piResult.error}`]);
    throw new Error(piResult.error);
  }
  const piBin = piResult.path;

  const modelArgs = resolveModel(opts.preferredModel);
  // Build the shell command with single-quoted paths to handle spaces.
  const safeTask = taskPath.replace(/'/g, "'\\''");
  const safeOut = stdoutPath.replace(/'/g, "'\\''");
  const safePi = piBin.replace(/'/g, "'\\''");
  const modelStr = modelArgs.map((a) => `'${a.replace(/'/g, "'\\''")}'`).join(" ");
  const shellCmd = modelStr
    ? `'${safePi}' ${modelStr} --print < '${safeTask}' > '${safeOut}' 2>&1`
    : `'${safePi}' --print < '${safeTask}' > '${safeOut}' 2>&1`;

  try {
    await execFileAsync("tmux", ["new-session", "-d", "-s", sessionId, "-c", opts.cwd, "sh", "-c", shellCmd]);
    await writeLog(opts.cwd, sessionId, [`tmux-started session=${sessionId}`]);
  } catch (err) {
    await rm(dirPath, { recursive: true, force: true });
    await writeLog(opts.cwd, sessionId, [`tmux-start-error: ${(err as Error).message}`]);
    throw err;
  }

  activeSessions.add(sessionId);

  const cleanup = async (reason: string) => {
    activeSessions.delete(sessionId);
    await writeLog(opts.cwd, sessionId, [`cleanup reason=${reason}`]);
    try { await execFileAsync("tmux", ["kill-session", "-t", sessionId]); } catch { /* already gone */ }
    try { await rm(dirPath, { recursive: true, force: true }); } catch { /* best-effort */ }
  };

  return new Promise<{ output: string; sessionId: string }>((resolve, reject) => {
    const startMs = Date.now();
    let pollCount = 0;
    // Declare timer before onAbort so the abort handler can safely clear it
    // even if abort fires before setInterval assigns the handle.
    let timer: ReturnType<typeof setInterval> | undefined;

    const onAbort = () => {
      if (timer !== undefined) clearInterval(timer);
      const elapsed = Math.round((Date.now() - startMs) / 1000);
      // writeLog is best-effort — cleanup runs unconditionally regardless of
      // whether the log write succeeds.
      writeLog(opts.cwd, sessionId, [`aborted elapsed=${elapsed}s`]).catch(() => {});
      cleanup("abort").then(() => reject(new Error("Aborted"))).catch(() => reject(new Error("Aborted")));
    };

    if (opts.signal?.aborted) { onAbort(); return; }
    opts.signal?.addEventListener("abort", onAbort, { once: true });

    timer = setInterval(async () => {
      pollCount++;

      if (Date.now() - startMs > SUBAGENT_TIMEOUT_MS) {
        clearInterval(timer!);
        opts.signal?.removeEventListener("abort", onAbort);
        const elapsed = Math.round((Date.now() - startMs) / 1000);
        await writeLog(opts.cwd, sessionId, [`timeout elapsed=${elapsed}s`]);
        await cleanup("timeout");
        reject(new Error(`subagent ${sessionId} timed out after ${SUBAGENT_TIMEOUT_MS / 60000} minutes`));
        return;
      }

      try {
        await execFileAsync("tmux", ["has-session", "-t", sessionId]);
        // Still running — emit a progress ping every 10 polls (~20 s).
        if (pollCount % 10 === 0) {
          const elapsed = Math.round((Date.now() - startMs) / 1000);
          await writeLog(opts.cwd, sessionId, [`running elapsed=${elapsed}s polls=${pollCount}`]);
          opts.onUpdate?.(`subagent ${sessionId} (${opts.agent}) still running — ${elapsed}s elapsed`);
        }
      } catch {
        // Session exited.
        clearInterval(timer!);
        opts.signal?.removeEventListener("abort", onAbort);
        let text = "";
        try { text = await readFile(stdoutPath, "utf8"); } catch { /* no output produced */ }
        const elapsed = Math.round((Date.now() - startMs) / 1000);
        // Log full output (up to LOG_HEAD_CHARS) so we can diagnose large/failing runs.
        await writeLog(opts.cwd, sessionId, [
          `exited elapsed=${elapsed}s output-bytes=${Buffer.byteLength(text, "utf8")}`,
          `output-head: ${text.slice(0, LOG_HEAD_CHARS).replace(/\n/g, "\\n")}`,
        ]);
        await cleanup("exit");
        // If the output looks like a bare error message (starts with "Error:")
        // throw it so the caller can classify and surface a recovery hint.
        const trimmed = text.trim();
        if (trimmed.startsWith("Error:") || trimmed.startsWith("Uncaught Error:")) {
          reject(new Error(trimmed));
          return;
        }
        resolve({ output: text, sessionId });
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
      if (signal?.aborted || activeSignal()?.aborted) {
        return { content: [{ type: "text", text: "clk_subagent cancelled before start." }], details: {} };
      }

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
        const { output, sessionId } = await spawnSubagent({
          agent: params.agent,
          task: params.task,
          preferredModel: params.preferredModel,
          cwd: ctx.cwd,
          signal: sig,
          // Wrap the plain-string progress message into the AgentToolResult
          // shape Pi's onUpdate callback expects. `details` is required by
          // the AgentToolResult<T> interface even for intermediate updates;
          // we pass {} here since we have no structured payload yet — the
          // final return below carries the real {agent, sessionId} details.
          // onUpdate itself is optional per AgentTool.execute's signature.
          onUpdate: (text) =>
            onUpdate?.({ content: [{ type: "text", text }], details: {} }),
        });
        let text = output || "(subagent produced no output)";
        if (text.length > MAX_OUTPUT_CHARS) {
          const omitted = text.length - MAX_OUTPUT_CHARS;
          text =
            text.slice(0, MAX_OUTPUT_CHARS) +
            `\n\n[output truncated: ${omitted} additional characters omitted — ` +
            `see .clk/logs/${sessionId}.log for the first ${LOG_HEAD_CHARS} characters of the full output]`;
        }
        return {
          content: [{ type: "text", text }],
          details: { agent: params.agent, sessionId },
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
