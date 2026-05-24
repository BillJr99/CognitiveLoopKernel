/**
 * Tests for the new pi-extension safety nets:
 *
 *   - ensureRepo installs a hardened .gitignore + pre-push secret-scan hook.
 *   - state.ts persists every file atomically with .bak rotation.
 *   - restoreBackup swaps .bak back into place.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile, writeFile, stat } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { ensureRepo } from "../src/git.ts";
import {
  loadFromFiles,
  reset,
  setIdea,
  setRoster,
  restoreBackup,
} from "../src/state.ts";

const appendEntryCalls: Array<unknown> = [];
const fakePi: any = {
  appendEntry: (_t: string, d: unknown) => {
    appendEntryCalls.push(d);
  },
};

let cwd: string;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "clk-safety-"));
  appendEntryCalls.length = 0;
  reset();
});

afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
});

describe("ensureRepo safety nets", () => {
  test("writes a hardened .gitignore when absent", async () => {
    await ensureRepo(cwd);
    const ignore = await readFile(join(cwd, ".gitignore"), "utf8");
    // Confirm every secret-pattern line is present.
    assert.match(ignore, /^\.clk\/$/m);
    assert.match(ignore, /^\/\.env$/m);
    assert.match(ignore, /^\/\.env\.bak$/m);
    assert.match(ignore, /^\*\.pem$/m);
    assert.match(ignore, /^\*_id_rsa\*$/m);
  });

  test("does NOT clobber an existing .gitignore", async () => {
    await writeFile(join(cwd, ".gitignore"), "# my custom rules\nfoo/\n", "utf8");
    await ensureRepo(cwd);
    const ignore = await readFile(join(cwd, ".gitignore"), "utf8");
    assert.match(ignore, /# my custom rules/);
    assert.match(ignore, /foo\//);
    // No CLK rules were added either — user content wins.
    assert.doesNotMatch(ignore, /^\.clk\/$/m);
  });

  test("installs a pre-push secret-scan hook", async () => {
    await ensureRepo(cwd);
    const hook = await readFile(join(cwd, ".git", "hooks", "pre-push"), "utf8");
    assert.match(hook, /pre-push/);
    assert.match(hook, /ANTHROPIC_API_KEY/);
    assert.match(hook, /sk-/);
    assert.match(hook, /BEGIN .* PRIVATE KEY/);
    // Should be marked executable.
    const st = await stat(join(cwd, ".git", "hooks", "pre-push"));
    // 0o111 = any of owner/group/other executable bits set.
    assert.ok((st.mode & 0o111) !== 0, "pre-push hook is not executable");
  });

  test("does NOT clobber an existing pre-push hook", async () => {
    // Install a fake first via ensureRepo, then overwrite, then re-call.
    await ensureRepo(cwd);
    await writeFile(join(cwd, ".git", "hooks", "pre-push"), "#!/bin/sh\nexit 0\n", "utf8");
    await ensureRepo(cwd);
    const hook = await readFile(join(cwd, ".git", "hooks", "pre-push"), "utf8");
    assert.strictEqual(hook, "#!/bin/sh\nexit 0\n");
  });
});

describe("atomic state writes", () => {
  test("setIdea writes idea.json atomically and clk.json", async () => {
    await loadFromFiles(cwd);
    await setIdea(cwd, "a journaling app", fakePi);
    const body = await readFile(join(cwd, ".clk", "state", "idea.json"), "utf8");
    const parsed = JSON.parse(body);
    assert.strictEqual(parsed.idea, "a journaling app");
  });

  test("second setIdea rotates the previous idea.json to .bak", async () => {
    await loadFromFiles(cwd);
    await setIdea(cwd, "first", fakePi);
    await setIdea(cwd, "second", fakePi);
    const cur = JSON.parse(await readFile(join(cwd, ".clk", "state", "idea.json"), "utf8"));
    assert.strictEqual(cur.idea, "second");
    const bak = JSON.parse(await readFile(join(cwd, ".clk", "state", "idea.json.bak"), "utf8"));
    assert.strictEqual(bak.idea, "first");
  });

  test("no .tmp files remain after a successful write", async () => {
    await loadFromFiles(cwd);
    await setIdea(cwd, "x", fakePi);
    let leftover = false;
    try {
      await stat(join(cwd, ".clk", "state", "idea.json.tmp"));
      leftover = true;
    } catch {
      /* expected */
    }
    assert.ok(!leftover, ".tmp file was left behind");
  });

  test("setRoster also rotates .bak on subsequent writes", async () => {
    await loadFromFiles(cwd);
    await setRoster(
      cwd,
      { agents: [{ name: "qa", mission: "test it", systemPersona: "..." }] },
      fakePi,
    );
    await setRoster(
      cwd,
      { agents: [{ name: "qa", mission: "audit", systemPersona: "..." }] },
      fakePi,
    );
    const cur = JSON.parse(await readFile(join(cwd, ".clk", "state", "roster.json"), "utf8"));
    assert.strictEqual(cur.agents[0].mission, "audit");
    const bak = JSON.parse(await readFile(join(cwd, ".clk", "state", "roster.json.bak"), "utf8"));
    assert.strictEqual(bak.agents[0].mission, "test it");
  });

  test("restoreBackup swaps idea.json.bak back into place", async () => {
    await loadFromFiles(cwd);
    await setIdea(cwd, "first", fakePi);
    await setIdea(cwd, "second", fakePi);
    const ideaPath = join(cwd, ".clk", "state", "idea.json");
    const restored = await restoreBackup(ideaPath);
    assert.strictEqual(restored, true);
    const cur = JSON.parse(await readFile(ideaPath, "utf8"));
    assert.strictEqual(cur.idea, "first");
  });

  test("restoreBackup returns false when no .bak exists", async () => {
    await loadFromFiles(cwd);
    await setIdea(cwd, "only", fakePi);
    // No second write → no .bak.
    const restored = await restoreBackup(join(cwd, ".clk", "state", "idea.json"));
    assert.strictEqual(restored, false);
  });
});
