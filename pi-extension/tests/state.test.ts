/**
 * Unit tests for src/state.ts — verify state persistence under .clk/state/.
 *
 * Uses a fake ExtensionAPI that records appendEntry calls so we can assert
 * the entry stream without standing up real pi.
 */
import { test, describe, before, after, beforeEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import {
  loadFromFiles,
  reset,
  getState,
  setIdea,
  setRoster,
  appendProgress,
  setHomeBranch,
  getHomeBranch,
  markDone,
  isDone,
} from "../src/state.ts";

// Minimal stub for ExtensionAPI — state.ts only calls pi.appendEntry().
const appendEntryCalls: Array<{ type: string; data: any }> = [];
const fakePi: any = {
  appendEntry: (type: string, data: any) => {
    appendEntryCalls.push({ type, data });
  },
};

let cwd: string;

before(async () => {
  cwd = await mkdtemp(join(tmpdir(), "clk-state-"));
});

after(async () => {
  await rm(cwd, { recursive: true, force: true });
});

beforeEach(() => {
  appendEntryCalls.length = 0;
  reset();
});

describe("loadFromFiles", () => {
  test("returns empty state when no clk.json exists", async () => {
    const fresh = await mkdtemp(join(tmpdir(), "clk-fresh-"));
    try {
      const s = await loadFromFiles(fresh);
      assert.deepEqual(s.progress, []);
      assert.equal(s.idea, undefined);
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
  });
});

describe("setIdea", () => {
  test("persists the idea to clk.json and idea.json", async () => {
    await setIdea(cwd, "A journaling app", fakePi);
    const state = getState();
    assert.equal(state.idea, "A journaling app");
    assert.ok(state.startedAt && state.startedAt > 0);

    // idea.json is written separately for tooling that doesn't load clk.json.
    const ideaJson = JSON.parse(
      await readFile(join(cwd, ".clk", "state", "idea.json"), "utf8"),
    );
    assert.equal(ideaJson.idea, "A journaling app");
  });

  test("emits a clk-state entry to pi.appendEntry", async () => {
    await setIdea(cwd, "Another idea", fakePi);
    assert.ok(appendEntryCalls.length >= 1);
    assert.equal(appendEntryCalls[0].type, "clk-state");
    assert.ok(appendEntryCalls[0].data.snapshot.idea);
  });
});

describe("setRoster", () => {
  test("persists the roster to disk", async () => {
    const roster = {
      agents: [
        { name: "engineer", mission: "build it", systemPersona: "..." },
        { name: "qa", mission: "test it", systemPersona: "..." },
      ],
      castedAt: Date.now(),
      reason: "initial casting",
    };
    await setRoster(cwd, roster, fakePi);
    const saved = JSON.parse(
      await readFile(join(cwd, ".clk", "state", "roster.json"), "utf8"),
    );
    assert.equal(saved.agents.length, 2);
    assert.equal(saved.agents[0].name, "engineer");
  });
});

describe("appendProgress", () => {
  test("appends an entry to the in-memory log AND to progress.md", async () => {
    await appendProgress(cwd, { kind: "checkpoint", message: "milestone" }, fakePi);
    assert.equal(getState().progress.length, 1);
    assert.equal(getState().progress[0].kind, "checkpoint");
    const progress = await readFile(
      join(cwd, ".clk", "state", "progress.md"),
      "utf8",
    );
    assert.ok(progress.includes("milestone"));
  });
});

describe("homeBranch", () => {
  test("setHomeBranch / getHomeBranch round-trip", async () => {
    await setHomeBranch(cwd, "main", fakePi);
    assert.equal(getHomeBranch(), "main");
  });
});

describe("markDone / isDone", () => {
  test("isDone returns false before markDone is called", async () => {
    const fresh = await mkdtemp(join(tmpdir(), "clk-done-"));
    try {
      assert.equal(await isDone(fresh), false);
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
  });

  test("markDone writes done.md and isDone flips true", async () => {
    const fresh = await mkdtemp(join(tmpdir(), "clk-done2-"));
    try {
      await markDone(fresh, "MVP complete", fakePi);
      assert.equal(await isDone(fresh), true);
      const content = await readFile(
        join(fresh, ".clk", "state", "done.md"),
        "utf8",
      );
      assert.ok(content.includes("MVP complete"));
    } finally {
      await rm(fresh, { recursive: true, force: true });
    }
  });
});

describe("reset", () => {
  test("wipes the in-memory state", async () => {
    await setIdea(cwd, "ephemeral", fakePi);
    reset();
    assert.equal(getState().idea, undefined);
    assert.deepEqual(getState().progress, []);
  });
});
