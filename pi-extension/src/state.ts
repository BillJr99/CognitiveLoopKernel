import { mkdir, readFile, writeFile, access, rename, unlink } from "node:fs/promises";
import { join } from "node:path";
import type { ExtensionAPI } from "@earendil-works/pi-coding-agent";
import type {
  ClkState,
  Roster,
  ProgressEntry,
  ProgressKind,
  RalphOutcome,
  SuperviseState,
} from "./types.js";

const ROOT = ".clk";

export const CLK_ENTRY_TYPE = "clk-state";

let memory: ClkState = { progress: [] };

async function ensureLayout(cwd: string): Promise<void> {
  await mkdir(join(cwd, ROOT, "state"), { recursive: true });
  await mkdir(join(cwd, ROOT, "logs"), { recursive: true });
}

/**
 * Atomic write with .bak rotation.
 *
 * Same semantics as the Python harness's clk_harness.config.save_json:
 * write to a sibling tempfile, rotate the previous file to .bak (when
 * it existed), then rename into place. A crash between any two of
 * those steps leaves either the old or the new file intact — never a
 * torn write. Used by every persist() call below so state on disk is
 * always a complete snapshot.
 */
async function atomicWrite(path: string, body: string, opts?: { backup?: boolean }): Promise<void> {
  const backup = opts?.backup ?? true;
  const tmp = `${path}.tmp`;
  await writeFile(tmp, body, "utf8");
  if (backup) {
    try {
      // If the destination already exists, rotate it to .bak. The
      // try/catch swallows ENOENT cleanly so first-time writes are no-ops.
      await access(path);
      await rename(path, `${path}.bak`);
    } catch {
      /* path doesn't exist yet — nothing to back up. */
    }
  }
  await rename(tmp, path);
}

/**
 * Restore the .bak copy of a state file, if one exists. Returns true
 * on success. Exposed for the upcoming /clk-undo command and for the
 * test harness.
 */
export async function restoreBackup(path: string): Promise<boolean> {
  try {
    await access(`${path}.bak`);
  } catch {
    return false;
  }
  try {
    await rename(`${path}.bak`, path);
    return true;
  } catch {
    return false;
  }
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
  await atomicWrite(
    join(cwd, ROOT, "state", "clk.json"),
    JSON.stringify(memory, null, 2),
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
  await atomicWrite(
    join(cwd, ROOT, "state", "idea.json"),
    JSON.stringify({ idea, capturedAt: memory.startedAt }, null, 2),
  );
}

export async function setRoster(cwd: string, roster: Roster, pi: ExtensionAPI): Promise<void> {
  memory.roster = roster;
  await persist(cwd, pi);
  await atomicWrite(
    join(cwd, ROOT, "state", "roster.json"),
    JSON.stringify(roster, null, 2),
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

const FRESH_SUPERVISE: SuperviseState = {
  noProgress: 0,
  continuations: 0,
  rescueAttempted: false,
};

export function getSupervise(): SuperviseState {
  return memory.supervise ?? { ...FRESH_SUPERVISE };
}

export async function setSupervise(
  cwd: string,
  s: SuperviseState,
  pi: ExtensionAPI,
): Promise<void> {
  memory.supervise = s;
  await persist(cwd, pi);
}

export async function resetSupervise(cwd: string, pi: ExtensionAPI): Promise<void> {
  memory.supervise = { ...FRESH_SUPERVISE };
  await persist(cwd, pi);
}

export async function recordRalphOutcome(
  cwd: string,
  branch: string,
  outcome: RalphOutcome["outcome"],
  pi: ExtensionAPI,
): Promise<void> {
  memory.ralphOutcomes = memory.ralphOutcomes ?? [];
  memory.ralphOutcomes.push({ branch, outcome, ts: Date.now() });
  // Keep the tail bounded; plateau detection only looks at the recent window.
  if (memory.ralphOutcomes.length > 50) {
    memory.ralphOutcomes = memory.ralphOutcomes.slice(-50);
  }
  await persist(cwd, pi);
}

/**
 * Consecutive reverted Ralph iterations, counted from the most recent
 * outcome backwards. A merge resets the streak — the plateau signal the
 * Python harness derives from its plateau_window.
 */
export function consecutiveRalphReverts(): number {
  const outcomes = memory.ralphOutcomes ?? [];
  let n = 0;
  for (let i = outcomes.length - 1; i >= 0; i--) {
    if (outcomes[i]!.outcome === "reverted") n++;
    else break;
  }
  return n;
}

export async function markDone(cwd: string, reason: string, pi: ExtensionAPI): Promise<void> {
  memory.doneReason = reason;
  await persist(cwd, pi);
  await atomicWrite(
    join(cwd, ROOT, "state", "done.md"),
    `# CLK done\n\nReason: ${reason}\nMarked: ${new Date().toISOString()}\n`,
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
