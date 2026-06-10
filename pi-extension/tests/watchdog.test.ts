/**
 * Tests for src/watchdog.ts — the supervise loop ported from the
 * Python harness's WorkflowRunner.run(). The decision ladder
 * (continue → stall rescue → stop) is pure and tested exhaustively;
 * the agent_end wiring is exercised with a fake pi + tmp workspace.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  evaluateTurn,
  continuationMessage,
  rescueMessage,
  onAgentEnd,
  limitsFromEnv,
  DEFAULT_STALL_CAP,
  DEFAULT_MAX_CONTINUES,
  type TurnSnapshot,
} from "../src/watchdog.ts";
import { startRun, endRun } from "../src/abort.ts";
import { reset, setIdea, getState, markDone, getSupervise } from "../src/state.ts";
import type { SuperviseState } from "../src/types.ts";

const LIMITS = { stallCap: 3, maxContinues: 100 };

function fresh(over: Partial<SuperviseState> = {}): SuperviseState {
  return { noProgress: 0, continuations: 0, rescueAttempted: false, ...over };
}

function snap(head: string | null, progressCount: number): TurnSnapshot {
  return { head, progressCount };
}

describe("evaluateTurn — decision ladder", () => {
  test("first observed turn is material (no baseline yet)", () => {
    const { next, decision } = evaluateTurn(fresh(), snap("aaaa", 0), LIMITS);
    assert.equal(decision.action, "continue");
    assert.equal(next.noProgress, 0);
    assert.equal(next.continuations, 1);
    assert.equal(next.lastHead, "aaaa");
  });

  test("HEAD movement resets the stall counter", () => {
    const prev = fresh({ noProgress: 2, lastHead: "aaaa", lastProgressCount: 5 });
    const { next, decision } = evaluateTurn(prev, snap("bbbb", 5), LIMITS);
    assert.equal(decision.action, "continue");
    assert.equal(next.noProgress, 0);
  });

  test("new progress entries count as material even without commits", () => {
    const prev = fresh({ noProgress: 2, lastHead: "aaaa", lastProgressCount: 5 });
    const { next, decision } = evaluateTurn(prev, snap("aaaa", 6), LIMITS);
    assert.equal(decision.action, "continue");
    assert.equal(next.noProgress, 0);
  });

  test("identical snapshot increments the stall counter but continues below the cap", () => {
    const prev = fresh({ noProgress: 0, lastHead: "aaaa", lastProgressCount: 5 });
    const { next, decision } = evaluateTurn(prev, snap("aaaa", 5), LIMITS);
    assert.equal(decision.action, "continue");
    assert.equal(next.noProgress, 1);
    assert.match(decision.reason, /1\/3/);
  });

  test("stall cap fires the one-shot rescue and resets the counter", () => {
    const prev = fresh({ noProgress: 2, lastHead: "aaaa", lastProgressCount: 5 });
    const { next, decision } = evaluateTurn(prev, snap("aaaa", 5), LIMITS);
    assert.equal(decision.action, "rescue");
    assert.equal(next.rescueAttempted, true);
    assert.equal(next.noProgress, 0);
  });

  test("stalling again after the rescue stops the run", () => {
    const prev = fresh({
      noProgress: 2, rescueAttempted: true, lastHead: "aaaa", lastProgressCount: 5,
    });
    const { decision } = evaluateTurn(prev, snap("aaaa", 5), LIMITS);
    assert.equal(decision.action, "stop");
    assert.match(decision.reason, /rescue/);
  });

  test("progress after a rescue keeps the run alive (rescue stays spent)", () => {
    const prev = fresh({
      noProgress: 0, rescueAttempted: true, lastHead: "aaaa", lastProgressCount: 5,
    });
    const { next, decision } = evaluateTurn(prev, snap("bbbb", 6), LIMITS);
    assert.equal(decision.action, "continue");
    assert.equal(next.rescueAttempted, true);
  });

  test("auto-continue cap stops the run regardless of progress", () => {
    const prev = fresh({ continuations: 100, lastHead: "aaaa", lastProgressCount: 5 });
    const { decision } = evaluateTurn(prev, snap("bbbb", 9), LIMITS);
    assert.equal(decision.action, "stop");
    assert.match(decision.reason, /cap/);
  });

  test("limitsFromEnv honors overrides and falls back to defaults", () => {
    const custom = limitsFromEnv({ CLK_STALL_CAP: "5", CLK_MAX_AUTO_CONTINUES: "10" } as NodeJS.ProcessEnv);
    assert.deepEqual(custom, { stallCap: 5, maxContinues: 10 });
    const fallback = limitsFromEnv({ CLK_STALL_CAP: "junk" } as NodeJS.ProcessEnv);
    assert.equal(fallback.stallCap, DEFAULT_STALL_CAP);
    assert.equal(fallback.maxContinues, DEFAULT_MAX_CONTINUES);
  });
});

describe("watchdog messages", () => {
  test("continuation message recaps the run and points at clk_done's bar", () => {
    const state = {
      idea: "a journaling app",
      progress: [{ ts: 1, kind: "note" as const, message: "started" }],
    };
    const msg = continuationMessage(state as never, snap("abcd1234", 1));
    assert.match(msg, /journaling app/);
    assert.match(msg, /abcd1234/);
    assert.match(msg, /clk_done ONLY/);
    assert.match(msg, /low-bar/);
  });

  test("rescue message forbids repeating and demands a commit or done", () => {
    const msg = rescueMessage({ idea: "x", progress: [] } as never, snap(null, 0), "3 stalled turns");
    assert.match(msg, /STALL RESCUE/);
    assert.match(msg, /RESTRUCTURE/);
    assert.match(msg, /UNBLOCK/);
    assert.match(msg, /clk_done/);
  });
});

describe("onAgentEnd wiring", () => {
  let cwd: string;
  const sent: string[] = [];
  const fakePi = {
    sendUserMessage: (m: string) => { sent.push(m); },
    appendEntry: () => {},
  } as never;

  function fakeCtx() {
    const statuses: Record<string, string> = {};
    const notices: string[] = [];
    return {
      cwd,
      ui: {
        setStatus: (k: string, v: string) => { statuses[k] = v; },
        notify: (m: string) => { notices.push(m); },
      },
      _statuses: statuses,
      _notices: notices,
    };
  }

  beforeEach(async () => {
    cwd = await mkdtemp(join(tmpdir(), "clk-wd-"));
    sent.length = 0;
    reset();
    endRun("test setup");
  });

  afterEach(async () => {
    endRun("test teardown");
    await rm(cwd, { recursive: true, force: true });
  });

  test("does nothing when no run is active", async () => {
    await setIdea(cwd, "idea", fakePi);
    const decision = await onAgentEnd(fakePi, fakeCtx(), { getHead: async () => "aaaa" });
    assert.equal(decision, null);
    assert.equal(sent.length, 0);
  });

  test("re-prompts the chief when a run is active and not done", async () => {
    await setIdea(cwd, "build a thing", fakePi);
    startRun();
    const decision = await onAgentEnd(fakePi, fakeCtx(), { getHead: async () => "aaaa" });
    assert.equal(decision?.action, "continue");
    assert.equal(sent.length, 1);
    assert.match(sent[0]!, /watchdog/i);
    assert.match(sent[0]!, /build a thing/);
    // Counters persisted for the next turn.
    assert.equal(getSupervise().continuations, 1);
  });

  test("does nothing once done.md exists", async () => {
    await setIdea(cwd, "idea", fakePi);
    await markDone(cwd, "finished", fakePi);
    startRun();
    const decision = await onAgentEnd(fakePi, fakeCtx(), { getHead: async () => "aaaa" });
    assert.equal(decision, null);
    assert.equal(sent.length, 0);
  });

  test("walks the full ladder: continue → rescue → stop ends the run", async () => {
    await setIdea(cwd, "idea", fakePi);
    startRun();
    const ctx = fakeCtx();
    const deps = { getHead: async () => "static" };
    // Turn 1 establishes the baseline (material); then identical
    // snapshots stall. progress.length stays fixed because the fake pi
    // never appends progress.
    const actions: string[] = [];
    for (let i = 0; i < 10; i++) {
      const d = await onAgentEnd(fakePi, ctx, deps);
      if (d === null) break;
      actions.push(d.action);
      if (d.action === "stop") break;
    }
    assert.ok(actions.includes("rescue"), `expected a rescue in ${actions.join(",")}`);
    assert.equal(actions[actions.length - 1], "stop");
    assert.equal(ctx._statuses["clk-run"], "stalled");
    // After stop the run is no longer active, so the next turn no-ops.
    const after = await onAgentEnd(fakePi, ctx, deps);
    assert.equal(after, null);
    assert.deepEqual(getState().idea, "idea"); // state preserved for /clk-resume
  });
});
