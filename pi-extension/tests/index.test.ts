/**
 * Integration test for src/index.ts — exercise the extension's default
 * export using a stub ExtensionAPI.  We verify that the extension:
 *   - registers the /clk command
 *   - registers at least the documented clk_* tools
 *   - hooks the session_start / agent_end events
 *
 * No live Pi process is needed; we drive the lifecycle by calling the
 * recorded handlers directly.
 */
import { test, describe, before, after } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

import clkExtension, { firstLineShort } from "../src/index.ts";

// ---------------------------------------------------------------------------
// Fake pi.ExtensionAPI -- just enough surface for the extension to register
// itself and have its handlers invoked.
// ---------------------------------------------------------------------------

type Listener = (event: any, ctx: any) => Promise<void> | void;

function makeFakePi() {
  const tools: any[] = [];
  const commands: Record<string, any> = {};
  const listeners: Record<string, Listener[]> = {};
  const entries: Array<{ type: string; data: any }> = [];

  const pi = {
    registerTool: (def: any) => { tools.push(def); },
    registerCommand: (name: string, def: any) => { commands[name] = def; },
    on: (event: string, handler: Listener) => {
      (listeners[event] ||= []).push(handler);
    },
    sendUserMessage: async (_msg: string) => {},
    appendEntry: (type: string, data: any) => { entries.push({ type, data }); },
    // The extension calls these from index.ts → abort.ts.
    onAbort: () => {},
  };

  return { pi, tools, commands, listeners, entries };
}

function makeFakeCtx(cwd: string) {
  const notifications: any[] = [];
  const statuses: Record<string, string> = {};
  return {
    cwd,
    waitForIdle: async () => {},
    ui: {
      notify: (msg: string, level: string) => notifications.push({ msg, level }),
      setStatus: (key: string, value: string) => { statuses[key] = value; },
    },
    _notifications: notifications,
    _statuses: statuses,
  };
}

// ---------------------------------------------------------------------------
// Tests
// ---------------------------------------------------------------------------

describe("clkExtension default export", () => {
  test("is a function", () => {
    assert.equal(typeof clkExtension, "function");
  });

  test("registers tools and the /clk command", async () => {
    const { pi, tools, commands } = makeFakePi();
    await clkExtension(pi as any);

    const toolNames = tools.map((t) => t.name);
    for (const required of ["clk_cast", "clk_progress", "clk_checkpoint", "clk_done"]) {
      assert.ok(
        toolNames.includes(required),
        `tool ${required} not registered (got ${toolNames.join(", ")})`,
      );
    }
    assert.ok(commands["clk"], "/clk command was not registered");
    assert.equal(typeof commands["clk"].handler, "function");
  });

  test("hooks session_start and agent_end", async () => {
    const { pi, listeners } = makeFakePi();
    await clkExtension(pi as any);
    assert.ok((listeners["session_start"] || []).length >= 1);
    assert.ok((listeners["agent_end"] || []).length >= 1);
  });

  test("session_start with an empty workspace runs cleanly", async () => {
    const { pi, listeners } = makeFakePi();
    await clkExtension(pi as any);
    const tmp = await mkdtemp(join(tmpdir(), "clk-idx-"));
    try {
      const ctx = makeFakeCtx(tmp);
      for (const fn of listeners["session_start"] || []) {
        await fn({}, ctx);
      }
      // setStatus might be called only when state exists; just check no throws.
      assert.ok(true);
    } finally {
      await rm(tmp, { recursive: true, force: true });
    }
  });

  test("firstLineShort returns only the first non-empty line, trimmed and capped", () => {
    // Single line — returned verbatim up to the cap.
    assert.equal(firstLineShort("hello world", 60), "hello world");
    // Multi-line — the second line must never leak into the status string.
    assert.equal(firstLineShort("refactor X\n\nbecause Y", 60), "refactor X");
    // Leading blank lines are skipped so the first *content* line wins.
    assert.equal(firstLineShort("\n\nactual idea\nmore", 60), "actual idea");
    // Long single line is truncated to max chars; no newline appears.
    const long = "a".repeat(120);
    const out = firstLineShort(long, 60);
    assert.equal(out.length, 60);
    assert.equal(out.includes("\n"), false);
  });

  test("/clk command rejects empty idea with a warning", async () => {
    const { pi, commands } = makeFakePi();
    await clkExtension(pi as any);
    const tmp = await mkdtemp(join(tmpdir(), "clk-idx2-"));
    try {
      const ctx = makeFakeCtx(tmp);
      await commands["clk"].handler("", ctx);
      const warned = ctx._notifications.some(
        (n: any) => n.level === "warning" && /idea/i.test(n.msg),
      );
      assert.ok(warned, `expected a warning notification; got ${JSON.stringify(ctx._notifications)}`);
    } finally {
      await rm(tmp, { recursive: true, force: true });
    }
  });
});
