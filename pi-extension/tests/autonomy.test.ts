/**
 * Code-enforced autonomy gates: clk_done / clk_merge validation
 * refusals, the clk_ralph plateau guard, and the runValidation helper.
 * These are the extension's equivalent of per-stage `validation:`
 * commands and plateau detection in the Python harness.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, writeFile, access } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

import { runValidation, runValidations } from "../src/validate.ts";
import { registerClkTools } from "../src/tools.ts";
import { reset, recordRalphOutcome, consecutiveRalphReverts, setHomeBranch } from "../src/state.ts";
import { endRun } from "../src/abort.ts";

const execFileAsync = promisify(execFile);

const fakePi = {
  registerTool: (def: any) => { toolDefs[def.name] = def; },
  sendUserMessage: () => {},
  appendEntry: () => {},
} as never;
let toolDefs: Record<string, any> = {};

function fakeCtx(cwd: string) {
  const statuses: Record<string, string> = {};
  return {
    cwd,
    ui: {
      setStatus: (k: string, v: string) => { statuses[k] = v; },
      notify: () => {},
    },
    _statuses: statuses,
  };
}

function resultText(res: any): string {
  return (res.content ?? []).map((c: any) => c.text).join("\n");
}

let cwd: string;

beforeEach(async () => {
  cwd = await mkdtemp(join(tmpdir(), "clk-auto-"));
  toolDefs = {};
  reset();
  endRun("test setup");
  registerClkTools(fakePi);
});

afterEach(async () => {
  endRun("test teardown");
  await rm(cwd, { recursive: true, force: true });
});

describe("runValidation", () => {
  test("passing command returns ok with output", async () => {
    const r = await runValidation(cwd, "echo all-good");
    assert.equal(r.ok, true);
    assert.match(r.output, /all-good/);
    assert.equal(r.exitCode, 0);
  });

  test("failing command returns ok=false with the exit code", async () => {
    const r = await runValidation(cwd, "echo broken >&2; exit 3");
    assert.equal(r.ok, false);
    assert.match(r.output, /broken/);
    assert.equal(r.exitCode, 3);
  });

  test("runValidations stops at the first failure", async () => {
    const { ok, results } = await runValidations(cwd, ["true", "false", "echo never"]);
    assert.equal(ok, false);
    assert.equal(results.length, 2);
  });
});

describe("clk_done validation gate", () => {
  test("refuses completion while a validate command fails", async () => {
    const ctx = fakeCtx(cwd);
    const res = await toolDefs["clk_done"].execute(
      "id", { reason: "all done", validate: ["exit 1"] }, undefined, undefined, ctx,
    );
    assert.match(resultText(res), /REFUSED/);
    assert.equal(res.details.validationFailed, true);
    // done.md must NOT exist after a refusal.
    await assert.rejects(access(join(cwd, ".clk", "state", "done.md")));
  });

  test("marks done when every validate command passes", async () => {
    const ctx = fakeCtx(cwd);
    const res = await toolDefs["clk_done"].execute(
      "id", { reason: "complete", validate: ["true", "echo ok"] }, undefined, undefined, ctx,
    );
    assert.match(resultText(res), /marked done/);
    await access(join(cwd, ".clk", "state", "done.md")); // exists
  });
});

describe("clk_ralph plateau guard", () => {
  test("refuses a 4th identical attempt after 3 consecutive reverts", async () => {
    for (let i = 1; i <= 3; i++) {
      await recordRalphOutcome(cwd, `ralph/iter-${i}`, "reverted", fakePi);
    }
    assert.equal(consecutiveRalphReverts(), 3);
    const ctx = fakeCtx(cwd);
    const res = await toolDefs["clk_ralph"].execute(
      "id",
      { iterationName: "iter-4", agent: "engineer", task: "another tweak" },
      undefined, undefined, ctx,
    );
    assert.match(resultText(res), /PLATEAU/);
    assert.equal(res.details.plateau, true);
    assert.equal(res.details.consecutiveReverts, 3);
  });

  test("a merge resets the revert streak", async () => {
    await recordRalphOutcome(cwd, "ralph/a", "reverted", fakePi);
    await recordRalphOutcome(cwd, "ralph/b", "reverted", fakePi);
    await recordRalphOutcome(cwd, "ralph/c", "merged", fakePi);
    assert.equal(consecutiveRalphReverts(), 0);
  });
});

describe("clk_merge validation gate (real git)", () => {
  async function git(...args: string[]): Promise<void> {
    await execFileAsync("git", args, { cwd });
  }

  test("refuses the merge and stays on the feature branch when validate fails", async () => {
    await git("init", "-q", "-b", "main");
    await git("config", "user.name", "t");
    await git("config", "user.email", "t@t");
    await git("config", "commit.gpgsign", "false");
    await writeFile(join(cwd, "a.txt"), "base\n");
    await git("add", "-A");
    await git("commit", "-q", "-m", "base");
    await setHomeBranch(cwd, "main", fakePi);
    await git("checkout", "-q", "-b", "ralph/iter-1");
    await writeFile(join(cwd, "a.txt"), "changed\n");

    const ctx = fakeCtx(cwd);
    const res = await toolDefs["clk_merge"].execute(
      "id", { message: "win", validate: "exit 7" }, undefined, undefined, ctx,
    );
    assert.match(resultText(res), /REFUSED/);
    assert.equal(res.details.validationFailed, true);
    const { stdout } = await execFileAsync(
      "git", ["rev-parse", "--abbrev-ref", "HEAD"], { cwd },
    );
    assert.equal(stdout.trim(), "ralph/iter-1"); // never left the branch
  });
});
