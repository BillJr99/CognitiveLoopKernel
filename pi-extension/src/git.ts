import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { mkdir, writeFile, access, chmod } from "node:fs/promises";
import { join } from "node:path";

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

/**
 * Hardened .gitignore patterns. Same list used by the Python harness's
 * kickoff.sh — secrets-by-pattern blocked so even an accidental
 * `git add .` won't push an API key. Listed here so the extension can
 * write the file even on repos that didn't go through kickoff.sh.
 */
const HARDENED_GITIGNORE = `# CLK harness state — ignore entirely.
.clk/
# Context-offload scratch space — disposable working memory, never committed.
scratch/
# Secrets — these patterns are also checked by the pre-push hook.
/.env
/.env.example
/.env.bak
/.env.partial
.env.local
*.pem
*.key
*_id_rsa*
/secrets/
/.secrets/
# Editor / OS junk
__pycache__/
*.pyc
.DS_Store
.idea/
.vscode/
`;

/**
 * Bash pre-push hook that scans the to-be-pushed objects for obvious
 * API-key patterns and aborts on a hit. Bypass with `git push
 * --no-verify` when sure. Pure bash so it ships without extra deps.
 */
const PRE_PUSH_HOOK = `#!/usr/bin/env bash
# CLK (Pi extension) pre-push secret scan. Bypass with \`git push --no-verify\`.
set -eo pipefail
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
  range="$local_sha"
  if [ "$remote_sha" != "0000000000000000000000000000000000000000" ]; then
    range="$remote_sha..$local_sha"
  fi
  hits=$(git log -p "$range" 2>/dev/null | grep -E \\
    -e 'ANTHROPIC_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'OPENAI_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'OPENROUTER_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'GEMINI_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'GOOGLE_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'sk-[A-Za-z0-9]{20,}' \\
    -e 'xoxb-[A-Za-z0-9-]{20,}' \\
    -e 'BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY' \\
    || true)
  if [ -n "$hits" ]; then
    echo "[pre-push] aborting — possible secret(s) in $range:" >&2
    echo "$hits" | head -n 5 >&2
    echo "" >&2
    echo "To override: git push --no-verify  (only when you're sure)." >&2
    exit 1
  fi
done
`;

async function fileExists(path: string): Promise<boolean> {
  try {
    await access(path);
    return true;
  } catch {
    return false;
  }
}

/**
 * Write the hardened .gitignore if one doesn't exist, and install the
 * pre-push secret-scan hook. Idempotent — both files only get written
 * when absent, so we never clobber user-customised content.
 */
async function installSafetyNets(cwd: string): Promise<void> {
  const ignorePath = join(cwd, ".gitignore");
  if (!(await fileExists(ignorePath))) {
    try {
      await writeFile(ignorePath, HARDENED_GITIGNORE, "utf8");
    } catch {
      /* Best-effort — never block run startup on this. */
    }
  }
  const hookPath = join(cwd, ".git", "hooks", "pre-push");
  if (!(await fileExists(hookPath))) {
    try {
      await mkdir(join(cwd, ".git", "hooks"), { recursive: true });
      await writeFile(hookPath, PRE_PUSH_HOOK, "utf8");
      await chmod(hookPath, 0o755);
    } catch {
      /* Best-effort — never block run startup on this either. */
    }
  }
}

export async function ensureRepo(cwd: string): Promise<void> {
  if (!(await isRepo(cwd))) {
    await git(cwd, ["init"]);
  }
  // Always (re-)check the safety nets so existing repos that pre-date
  // CLK still get a hardened .gitignore + pre-push hook on first run.
  await installSafetyNets(cwd);
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
  // Remove untracked files/dirs that a failed dispatch may have created.
  await git(cwd, ["clean", "-fd"], signal);
}

export async function abortMerge(cwd: string, signal?: AbortSignal): Promise<void> {
  await git(cwd, ["merge", "--abort"], signal);
}

export async function currentBranch(cwd: string, signal?: AbortSignal): Promise<string> {
  return await git(cwd, ["rev-parse", "--abbrev-ref", "HEAD"], signal);
}

export async function createAndCheckoutBranch(
  cwd: string,
  name: string,
  signal?: AbortSignal,
): Promise<void> {
  await git(cwd, ["checkout", "-b", name], signal);
}

export async function checkoutBranch(
  cwd: string,
  name: string,
  signal?: AbortSignal,
): Promise<void> {
  await git(cwd, ["checkout", name], signal);
}

export async function mergeBranch(
  cwd: string,
  branchName: string,
  signal?: AbortSignal,
): Promise<void> {
  await git(cwd, ["merge", "--no-ff", branchName, "-m", `[clk] merge ${branchName}`], signal);
}

/**
 * Commit any pending changes on the current branch (to preserve rejected work),
 * then switch to the target branch without merging.
 */
export async function saveAndSwitch(
  cwd: string,
  commitMessage: string,
  targetBranch: string,
  signal?: AbortSignal,
): Promise<void> {
  await git(cwd, ["add", "-A"], signal);
  let dirty = false;
  try {
    await git(cwd, ["diff", "--cached", "--quiet"], signal);
  } catch {
    dirty = true;
  }
  if (dirty) {
    await git(cwd, ["commit", "-m", commitMessage], signal);
  }
  await git(cwd, ["checkout", targetBranch], signal);
}

/** True when the repo has a remote with the given name. */
export async function hasRemote(
  cwd: string,
  name = "origin",
  signal?: AbortSignal,
): Promise<boolean> {
  try {
    await git(cwd, ["remote", "get-url", name], signal);
    return true;
  } catch {
    return false;
  }
}

/**
 * Count of local commits not yet on the upstream tracked branch. Returns
 * 0 on any failure (no remote, no upstream, detached HEAD, network down)
 * so callers can use it directly as a UI counter.
 */
export async function commitsAhead(
  cwd: string,
  signal?: AbortSignal,
): Promise<number> {
  try {
    await git(cwd, ["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"], signal);
  } catch {
    return 0;
  }
  try {
    const out = await git(cwd, ["rev-list", "--count", "@{u}..HEAD"], signal);
    return Number.parseInt(out, 10) || 0;
  } catch {
    return 0;
  }
}

/**
 * Best-effort `git push` — never throws. Returns `{ pushed: true }` on
 * success, otherwise `{ pushed: false, reason }` with stderr-derived
 * detail so the caller can surface a hint without writing its own
 * error-handling.
 */
export async function pushBestEffort(
  cwd: string,
  remote = "origin",
  branch?: string,
  signal?: AbortSignal,
): Promise<{ pushed: boolean; reason?: string }> {
  if (!(await hasRemote(cwd, remote, signal))) {
    return { pushed: false, reason: "no remote configured" };
  }
  const args = ["push", remote, branch ?? "HEAD"];
  try {
    await git(cwd, args, signal);
    return { pushed: true };
  } catch (err) {
    const raw = (err as { stderr?: string }).stderr;
    const reason = (typeof raw === "string" && raw.trim())
      ? raw.trim().split("\n").slice(-1)[0]?.slice(0, 200)
      : (err as Error).message?.slice(0, 200);
    return { pushed: false, reason: reason || "unknown error" };
  }
}
