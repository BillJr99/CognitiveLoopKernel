/**
 * Unit tests for src/prompts.ts — verify the chief primer contains the
 * captured idea and key orchestration rules.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { clkChiefPrimer } from "../src/prompts.ts";

describe("clkChiefPrimer", () => {
  test("includes the captured idea verbatim", () => {
    const idea = "A local-first journaling app that summarises my week";
    const out = clkChiefPrimer(idea);
    assert.ok(out.includes(idea), "primer should include the idea");
  });

  test("mentions the core CLK tools", () => {
    const out = clkChiefPrimer("anything");
    for (const tool of [
      "clk_cast",
      "clk_subagent",
      "clk_subagent_quality",
      "clk_consensus",
      "clk_autoresearch",
      "clk_ralph",
      "clk_checkpoint",
      "clk_done",
      "clk_todos",
      "clk_delegate",
    ]) {
      assert.ok(out.includes(tool), `primer should reference ${tool}`);
    }
  });

  test("teaches the mutable TODOS checklist convention", () => {
    const out = clkChiefPrimer("anything");
    assert.ok(out.includes("clk_todos"), "primer should mention the clk_todos tool");
    assert.ok(/overwrite/i.test(out), "primer should explain last-write-wins semantics");
  });

  test("teaches the context-offload scratch/ convention", () => {
    const out = clkChiefPrimer("anything");
    assert.ok(out.includes("scratch/"), "primer should mention the scratch/ convention");
    assert.ok(/offload/i.test(out), "primer should mention context offload");
  });

  test("describes clk_delegate as context-isolated", () => {
    const out = clkChiefPrimer("anything").toLowerCase();
    assert.ok(out.includes("clk_delegate"));
    assert.ok(out.includes("context-isolated") || out.includes("isolated"),
      "primer should describe clk_delegate isolation");
  });

  test("references casting and completion criteria", () => {
    const out = clkChiefPrimer("anything").toLowerCase();
    assert.ok(out.includes("cast"), "primer should mention casting");
    assert.ok(out.includes("done") || out.includes("completion"),
      "primer should mention completion criteria");
  });

  test("returns a non-trivial string", () => {
    const out = clkChiefPrimer("x");
    assert.equal(typeof out, "string");
    assert.ok(out.length > 500, "primer should be at least 500 chars");
  });
});
