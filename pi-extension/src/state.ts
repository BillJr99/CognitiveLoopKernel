import { mkdir, readFile, writeFile, access } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import type { ClkState, Roster, ProgressEntry, ProgressKind } from "./types.js";

const ROOT = ".clk";

export const CLK_ENTRY_TYPE = "clk-state";

let memory: ClkState = { progress: [] };

async function ensureLayout(cwd: string): Promise<void> {
  await mkdir(join(cwd, ROOT, "state"), { recursive: true });
  await mkdir(join(cwd, ROOT, "logs"), { recursive: true });
}

export async function loadFromFiles(cwd: string): Promise<ClkState> {
  await ensureLayout(cwd);
  try {
    const raw = await readFile(join(cwd, ROOT, "state", "clk.json"), "utf8");
    memory = JSON.parse(raw) as ClkState;
    if (!Array.isArray(memory.progress)) memory.progress = [];
  } catch {
    memory = { progress: [] };
  }
  return memory;
}

async function persist(cwd: string, pi: ExtensionAPI): Promise<void> {
  await ensureLayout(cwd);
  await writeFile(
    join(cwd, ROOT, "state", "clk.json"),
    JSON.stringify(memory, null, 2),
    "utf8",
  );
  pi.appendEntry(CLK_ENTRY_TYPE, { snapshot: memory });
}

export function getState(): ClkState {
  return memory;
}

export function reset(): void {
  memory = { progress: [] };
}

export async function setIdea(cwd: string, idea: string, pi: ExtensionAPI): Promise<void> {
  memory.idea = idea;
  memory.startedAt = memory.startedAt ?? Date.now();
  await persist(cwd, pi);
  await writeFile(
    join(cwd, ROOT, "state", "idea.json"),
    JSON.stringify({ idea, capturedAt: memory.startedAt }, null, 2),
    "utf8",
  );
}

export async function setRoster(cwd: string, roster: Roster, pi: ExtensionAPI): Promise<void> {
  memory.roster = roster;
  await persist(cwd, pi);
  await writeFile(
    join(cwd, ROOT, "state", "roster.json"),
    JSON.stringify(roster, null, 2),
    "utf8",
  );
}

export async function appendProgress(
  cwd: string,
  entry: { kind: ProgressKind; message: string },
  pi: ExtensionAPI,
): Promise<void> {
  const full: ProgressEntry = { ts: Date.now(), kind: entry.kind, message: entry.message };
  memory.progress.push(full);
  await persist(cwd, pi);
  const line = `${new Date(full.ts).toISOString()} [${full.kind}] ${full.message}\n`;
  await writeFile(join(cwd, ROOT, "state", "progress.md"), line, { flag: "a", encoding: "utf8" });
}

export async function setHomeBranch(cwd: string, branch: string, pi: ExtensionAPI): Promise<void> {
  memory.homeBranch = branch;
  await persist(cwd, pi);
}

export function getHomeBranch(): string | undefined {
  return memory.homeBranch;
}

export async function markDone(cwd: string, reason: string, pi: ExtensionAPI): Promise<void> {
  memory.doneReason = reason;
  await persist(cwd, pi);
  await writeFile(
    join(cwd, ROOT, "state", "done.md"),
    `# CLK done\n\nReason: ${reason}\nMarked: ${new Date().toISOString()}\n`,
    "utf8",
  );
}

export async function isDone(cwd: string): Promise<boolean> {
  try {
    await access(join(cwd, ROOT, "state", "done.md"));
    return true;
  } catch {
    return false;
  }
}
