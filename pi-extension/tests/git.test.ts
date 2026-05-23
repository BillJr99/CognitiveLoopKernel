/**
 * Integration tests for src/git.ts — exercises the real `git` binary in
 * an ephemeral tmpdir.
 */
import { test, describe, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, mkdir } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import {
  isRepo,
  ensureRepo,
  head,
  checkpoint,
  revertTo,
  currentBranch,
  createAndCheckoutBranch,
  checkoutBranch,
  mergeBranch,
  saveAndSwitch,
} from "../src/git.ts";

const execFileAsync = promisify(execFile);

async function gitConfig(cwd: string, key: string, value: string): Promise<void> {
  await execFileAsync("git", ["config", key, value], { cwd });
}

async function disableSigning(cwd: string): Promise<void> {
  // Some sandboxed CI environments require commit signing but lack a working
  // signing setup; opt out per-repo so tests always succeed.
  await gitConfig(cwd, "commit.gpgsign", "false");
  await gitConfig(cwd, "tag.gpgsign", "false");
}

let workdir: string;

before(async () => {
  workdir = await mkdtemp(join(tmpdir(), "clk-git-"));
});

after(async () => {
  await rm(workdir, { recursive: true, force: true });
});

describe("isRepo / ensureRepo", () => {
  test("isRepo is false in a fresh dir", async () => {
    const fresh = await mkdtemp(join(tmpdir(), "clk-fresh-"));
    try {
      assert.equal(await isRepo(fresh), false);
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
  });

  test("ensureRepo initialises a git repo", async () => {
    const fresh = await mkdtemp(join(tmpdir(), "clk-init-"));
    try {
      await ensureRepo(fresh);
      assert.equal(await isRepo(fresh), true);
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
  });

  test("ensureRepo is a no-op when already a repo", async () => {
    await ensureRepo(workdir);
    await ensureRepo(workdir);
    assert.equal(await isRepo(workdir), true);
  });
});

describe("checkpoint", () => {
  test("commits staged changes and returns new HEAD", async () => {
    const dir = await mkdtemp(join(tmpdir(), "clk-cp-"));
    try {
      await ensureRepo(dir);
      await gitConfig(dir, "user.name", "test");
      await gitConfig(dir, "user.email", "test@clk.invalid");
      await disableSigning(dir);
      await writeFile(join(dir, "a.txt"), "hello");
      const sha = await checkpoint(dir, "[clk] add a.txt");
      assert.ok(sha, "checkpoint should return a SHA");
      assert.equal(sha, await head(dir));
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });

  test("returns null when there's nothing to commit", async () => {
    const dir = await mkdtemp(join(tmpdir(), "clk-cp-empty-"));
    try {
      await ensureRepo(dir);
      await gitConfig(dir, "user.name", "test");
      await gitConfig(dir, "user.email", "test@clk.invalid");
      await disableSigning(dir);
      // make an initial commit so HEAD exists
      await writeFile(join(dir, "seed.txt"), "seed");
      await checkpoint(dir, "[clk] seed");
      // now an empty checkpoint
      assert.equal(await checkpoint(dir, "[clk] no-op"), null);
    } finally {
      await rm(dir, { recursive: true, force: true });
    }
  });
});

describe("branching", () => {
  let dir: string;
  let baseSha: string;

  before(async () => {
    dir = await mkdtemp(join(tmpdir(), "clk-branch-"));
    await ensureRepo(dir);
    await gitConfig(dir, "user.name", "test");
    await gitConfig(dir, "user.email", "test@clk.invalid");
    await disableSigning(dir);
    await writeFile(join(dir, "base.txt"), "base");
    const sha = await checkpoint(dir, "[clk] base");
    assert.ok(sha);
    baseSha = sha!;
  });

  after(async () => {
    await rm(dir, { recursive: true, force: true });
  });

  test("createAndCheckoutBranch + currentBranch", async () => {
    await createAndCheckoutBranch(dir, "feature/x");
    assert.equal(await currentBranch(dir), "feature/x");
  });

  test("commit on branch + mergeBranch back into trunk", async () => {
    await writeFile(join(dir, "feat.txt"), "feature work");
    const featSha = await checkpoint(dir, "[clk] feature");
    assert.ok(featSha);

    // go back to the original branch
    const trunk = "main";
    // detect trunk name (might be 'main' or 'master')
    const branches = (await execFileAsync(
      "git", ["branch", "--list"], { cwd: dir },
    )).stdout;
    const trunkName = branches.includes("main") ? "main" : "master";
    await checkoutBranch(dir, trunkName);
    assert.equal(await currentBranch(dir), trunkName);

    await mergeBranch(dir, "feature/x");
    // After merge, feat.txt should exist on trunk.
    const { stdout } = await execFileAsync(
      "git", ["log", "--oneline"], { cwd: dir },
    );
    assert.ok(stdout.includes("merge feature/x"));
  });

  test("revertTo resets to a prior SHA", async () => {
    // Make a junk commit, then revert
    await writeFile(join(dir, "junk.txt"), "garbage");
    const junkSha = await checkpoint(dir, "[clk] junk");
    assert.ok(junkSha);
    assert.notEqual(junkSha, baseSha);
    await revertTo(dir, baseSha);
    assert.equal(await head(dir), baseSha);
  });
});
