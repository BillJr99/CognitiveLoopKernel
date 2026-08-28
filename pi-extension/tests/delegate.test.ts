/**
 * Tests for runDelegate (context-isolated sub-agent). A fake spawn is
 * injected so no tmux / pi is needed — we verify the isolation + distill
 * preamble, context hand-in, and result truncation, not the real
 * subprocess plumbing.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import { runDelegate, type SpawnFn } from "../src/consensus.ts";

describe("runDelegate", () => {
  test("hands the child an isolation + distill preamble, context, and task", async () => {
    let seen: any = null;
    const spawn: SpawnFn = async (opts) => {
      seen = opts;
      return { output: "the distilled answer", sessionId: "s1" };
    };
    const res = await runDelegate({
      agent: "engineer",
      task: "build the helper",
      context: "focus on X",
      cwd: "/tmp",
      spawn,
      gauntlet: false,
    });
    assert.equal(res.output, "the distilled answer");
    assert.equal(res.sessionId, "s1");
    assert.equal(seen.agent, "engineer");
    assert.ok(seen.task.includes("context-isolated"), "carries the isolation preamble");
    assert.ok(
      seen.task.includes("concise, self-contained summary"),
      "instructs the child to return only a distilled result",
    );
    assert.ok(seen.task.includes("Context:\nfocus on X"), "hands in the context");
    assert.ok(seen.task.includes("Task:\nbuild the helper"), "includes the task body");
  });

  test("context is optional", async () => {
    let seen: any = null;
    const spawn: SpawnFn = async (opts) => {
      seen = opts;
      return { output: "ok", sessionId: "s" };
    };
    await runDelegate({ agent: "qa", task: "run checks", cwd: "/tmp", spawn, gauntlet: false });
    assert.ok(!seen.task.includes("Context:"), "no Context section when omitted");
    assert.ok(seen.task.includes("Task:\nrun checks"));
  });

  test("empty child output yields a placeholder", async () => {
    const spawn: SpawnFn = async () => ({ output: "", sessionId: "s" });
    const res = await runDelegate({ agent: "x", task: "t", cwd: "/tmp", spawn, gauntlet: false });
    assert.ok(res.output.includes("no output"));
  });

  test("caps the distilled result at maxResultChars", async () => {
    const spawn: SpawnFn = async () => ({ output: "z".repeat(50), sessionId: "s" });
    const res = await runDelegate({ agent: "x", task: "t", cwd: "/tmp", spawn, gauntlet: false, maxResultChars: 10 });
    assert.ok(res.output.startsWith("zzzzzzzzzz"));
    assert.ok(res.output.includes("truncated"));
  });
});
