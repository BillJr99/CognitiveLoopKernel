import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

async function git(cwd: string, args: string[], signal?: AbortSignal): Promise<string> {
  const { stdout } = await execFileAsync("git", args, {
    cwd,
    signal,
    maxBuffer: 16 * 1024 * 1024,
  });
  return stdout.trim();
}

export async function isRepo(cwd: string): Promise<boolean> {
  try {
    await git(cwd, ["rev-parse", "--git-dir"]);
    return true;
  } catch {
    return false;
  }
}

export async function ensureRepo(cwd: string): Promise<void> {
  if (await isRepo(cwd)) return;
  await git(cwd, ["init"]);
}

export async function head(cwd: string, signal?: AbortSignal): Promise<string | null> {
  try {
    return await git(cwd, ["rev-parse", "HEAD"], signal);
  } catch {
    return null;
  }
}

/**
 * Stage all changes and commit. Returns the new HEAD SHA, or null when there
 * was nothing to commit.
 */
export async function checkpoint(
  cwd: string,
  message: string,
  signal?: AbortSignal,
): Promise<string | null> {
  await ensureRepo(cwd);
  await git(cwd, ["add", "-A"], signal);
  let dirty = false;
  try {
    await git(cwd, ["diff", "--cached", "--quiet"], signal);
  } catch {
    dirty = true;
  }
  if (!dirty) return null;
  await git(cwd, ["commit", "-m", message], signal);
  return await head(cwd, signal);
}

export async function revertTo(cwd: string, sha: string, signal?: AbortSignal): Promise<void> {
  await git(cwd, ["reset", "--hard", sha], signal);
}
