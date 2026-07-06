/**
 * Tests for the TODOS checklist: setTodos persistence (last-write-wins)
 * and the clk_todos tool handler.
 */
import { test, describe, beforeEach, afterEach } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import { registerClkTools } from "../src/tools.ts";
import { reset, setTodos, getTodos } from "../src/state.ts";
import { endRun } from "../src/abort.ts";

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
  cwd = await mkdtemp(join(tmpdir(), "clk-todos-"));
  toolDefs = {};
  reset();
  endRun("test setup");
  registerClkTools(fakePi);
});
afterEach(async () => {
  await rm(cwd, { recursive: true, force: true });
});

describe("setTodos", () => {
  test("persists the checklist to .clk/state/todos.json", async () => {
    const items = [
      { status: "todo" as const, text: "a" },
      { status: "doing" as const, text: "b" },
    ];
    await setTodos(cwd, items, fakePi);
    const raw = JSON.parse(await readFile(join(cwd, ".clk", "state", "todos.json"), "utf8"));
    assert.deepEqual(raw, items);
    assert.deepEqual(getTodos(), items);
  });

  test("overwrites last-write-wins", async () => {
    await setTodos(cwd, [
      { status: "todo", text: "one" },
      { status: "todo", text: "two" },
    ], fakePi);
    await setTodos(cwd, [{ status: "done", text: "one" }], fakePi);
    assert.deepEqual(getTodos(), [{ status: "done", text: "one" }]);
  });
});

describe("clk_todos tool", () => {
  test("registers and overwrites via the handler, rendering marks", async () => {
    const res = await toolDefs["clk_todos"].execute(
      "id",
      { items: [
        { status: "todo", text: "wire parser" },
        { status: "done", text: "read docs" },
      ] },
      undefined, undefined, fakeCtx(cwd),
    );
    const text = resultText(res);
    assert.ok(text.includes("- [ ] wire parser"));
    assert.ok(text.includes("- [x] read docs"));
    assert.equal(res.details.count, 2);
    assert.equal(res.details.open, 1);
    assert.deepEqual(getTodos(), [
      { status: "todo", text: "wire parser" },
      { status: "done", text: "read docs" },
    ]);
  });

  test("clearing the list is allowed", async () => {
    const res = await toolDefs["clk_todos"].execute(
      "id", { items: [] }, undefined, undefined, fakeCtx(cwd),
    );
    assert.ok(resultText(res).includes("cleared"));
    assert.deepEqual(getTodos(), []);
  });
});
