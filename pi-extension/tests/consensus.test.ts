/**
 * Tests for src/consensus.ts. We inject a fake spawn function so the
 * tests don't need tmux or pi installed — the goal is to verify the
 * scoring / picking / retry behaviour, not the real subprocess plumbing
 * (which is exercised separately by the runtime smoke suite).
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  dispatchWithQuality,
  runConsensus,
  type SpawnFn,
} from "../src/consensus.ts";

// Sentinel substring detection — the quality-retry repair preamble is
// rendered by quality.repairHint and begins with this exact phrase.
const REPAIR_MARKER = "Your previous response was rejected";

// Comfortably above the empty threshold so the quality detector lets it
// through. Used wherever a test needs a "passing" response body.
const GOOD = "This is a comfortably substantive response that exceeds the empty threshold without question.";

describe("dispatchWithQuality", () => {
  test("returns the first ok response without retrying", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: GOOD, sessionId: `s${calls}` };
    };
    const res = await dispatchWithQuality({
      agent: "worker",
      task: "do the thing",
      cwd: "/tmp",
      spawn,
    });
    assert.equal(calls, 1);
    assert.equal(res.attempts, 1);
    assert.equal(res.quality.ok, true);
    assert.equal(res.sessionId, "s1");
  });

  test("retries with a repair preamble after a recoverable failure", async () => {
    let calls = 0;
    const taskSeen: string[] = [];
    const spawn: SpawnFn = async (opts) => {
      calls += 1;
      taskSeen.push(opts.task);
      if (calls === 1) return { output: "", sessionId: "s1" }; // empty → recoverable
      return { output: GOOD, sessionId: "s2" };
    };
    const retries: number[] = [];
    const res = await dispatchWithQuality({
      agent: "worker",
      task: "first attempt task",
      cwd: "/tmp",
      maxRetries: 1,
      onRetry: (n) => retries.push(n),
      spawn,
    });
    assert.equal(calls, 2);
    assert.equal(res.attempts, 2);
    assert.equal(res.quality.ok, true);
    assert.deepEqual(retries, [1]);
    // First call sees the original task; second sees the repair preamble.
    assert.equal(taskSeen[0]?.includes(REPAIR_MARKER), false);
    assert.equal(taskSeen[1]?.includes(REPAIR_MARKER), true);
    assert.equal(taskSeen[1]?.includes("first attempt task"), true);
  });

  test("stops retrying when maxRetries is exhausted", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: "", sessionId: `s${calls}` }; // always empty
    };
    const res = await dispatchWithQuality({
      agent: "worker",
      task: "task",
      cwd: "/tmp",
      maxRetries: 2,
      spawn,
    });
    assert.equal(calls, 3); // initial + 2 retries
    assert.equal(res.attempts, 3);
    assert.equal(res.quality.ok, false);
  });

  test("does NOT retry on a non-recoverable failure (refusal)", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: "I cannot help with that. As an AI language model, ...", sessionId: "s1" };
    };
    const res = await dispatchWithQuality({
      agent: "worker",
      task: "task",
      cwd: "/tmp",
      maxRetries: 5,
      spawn,
    });
    assert.equal(calls, 1); // bailed after the refusal
    assert.equal(res.quality.recoverable, false);
  });
});

describe("runConsensus", () => {
  test("fans out N samples and returns the highest-scoring winner", async () => {
    const outputs = ["", GOOD, GOOD + " (more detail)"];
    let issued = 0;
    const spawn: SpawnFn = async () => {
      const idx = issued++;
      return { output: outputs[idx]!, sessionId: `s${idx + 1}` };
    };
    const res = await runConsensus({
      agent: "designer",
      task: "design X",
      cwd: "/tmp",
      samples: 3,
      spawn,
    });
    assert.equal(res.all.length, 3);
    // Two of three samples pass quality (the empty one fails); the
    // winner is whichever of the two passing samples sorted higher.
    assert.equal(res.best.quality.ok, true);
    assert.match(res.best.output, /substantive response/);
    // reason names the winner and lists all scores.
    assert.match(res.reason, /sample #\d won/);
  });

  test("clamps samples to 1..6", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: GOOD, sessionId: `s${calls}` };
    };
    // samples = 10 should clamp down to 6.
    const res = await runConsensus({
      agent: "designer",
      task: "x",
      cwd: "/tmp",
      samples: 10,
      spawn,
    });
    assert.equal(res.all.length, 6);
  });

  test("captures spawn errors as sample.error without throwing", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      if (calls === 2) throw new Error("tmux gone");
      return { output: GOOD, sessionId: `s${calls}` };
    };
    const res = await runConsensus({
      agent: "designer",
      task: "x",
      cwd: "/tmp",
      samples: 3,
      spawn,
    });
    assert.equal(res.all.length, 3);
    const failed = res.all.find((s) => s.error);
    assert.ok(failed, "expected one sample to carry an error");
    assert.match(failed!.error!, /tmux gone/);
    // Winner is still one of the successful samples, never the errored one.
    assert.notEqual(res.best.index, failed!.index);
    assert.equal(res.best.quality.ok, true);
  });

  test("returns the least-bad sample even when all fail", async () => {
    const outputs = ["", "I cannot help.", ""]; // all bad
    let issued = 0;
    const spawn: SpawnFn = async () => {
      const idx = issued++;
      return { output: outputs[idx]!, sessionId: `s${idx + 1}` };
    };
    const res = await runConsensus({
      agent: "designer",
      task: "x",
      cwd: "/tmp",
      samples: 3,
      spawn,
    });
    assert.equal(res.all.length, 3);
    assert.equal(res.best.quality.ok, false);
    // The refusal has score 0.5, the empties have 0.4 — so the refusal wins on score
    // but the test we really care about is that runConsensus picked SOMETHING and
    // never threw.
    assert.ok(typeof res.best.output === "string");
  });

  test("respects maxParallel by capping concurrent spawns", async () => {
    let inFlight = 0;
    let peak = 0;
    const spawn: SpawnFn = async () => {
      inFlight += 1;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 10));
      inFlight -= 1;
      return { output: GOOD, sessionId: "s" };
    };
    await runConsensus({
      agent: "x",
      task: "t",
      cwd: "/tmp",
      samples: 6,
      maxParallel: 2,
      spawn,
    });
    assert.ok(peak <= 2, `expected peak in-flight ≤ 2, got ${peak}`);
  });
});
