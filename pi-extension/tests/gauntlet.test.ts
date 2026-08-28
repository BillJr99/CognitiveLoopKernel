/**
 * Tests for src/gauntlet.ts (layer 12).
 *
 * Mirrors tests/test_gauntlet.py in the Python harness so a behaviour
 * drift between the two implementations shows up here — the presets,
 * round caps, env-var precedence, boolean token sets, and the
 * VERDICT / SCORE / MATERIAL_DEFECTS contract are shared.
 *
 * The spawn function is injected throughout, so nothing here needs tmux
 * or pi installed.
 */
import { test, describe, beforeEach } from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_SETTINGS,
  PRESET_ROUNDS,
  converged,
  currentOverride,
  gauntletSettings,
  lensesFor,
  parseAnswerKey,
  parseBool,
  parseCritique,
  parseGauntletCommand,
  renderAnswerKey,
  budgetSpent,
  claimBudgetReport,
  resetBudget,
  resetOverride,
  roundsFor,
  runGauntlet,
  setOverride,
  summariseGauntlet,
  type GauntletSettings,
} from "../src/gauntlet.ts";
import { dispatchWithQuality, type SpawnFn } from "../src/consensus.ts";

const GOOD = "This is a comfortably substantive response that exceeds the empty threshold without question.";

// Each stage task opens with a `GAUNTLET :: <stage>` header. Match on that
// rather than on prose: the critique and verification bodies both mention
// "acceptance answer key" and "revisions" in passing.
const M_KEY = "GAUNTLET :: acceptance answer key";
const M_CRITIQUE = "GAUNTLET :: adversarial critique";
const M_REVISE = "GAUNTLET :: revision";
const M_VERIFY = "GAUNTLET :: final verification";
const M_REPAIR = "GAUNTLET :: final repair";

/** A critic reply that converges (nothing material found). */
const CLEAN = "Nothing material found.\nMATERIAL_DEFECTS: 0\nVERDICT: accept\nSCORE: 0.95";
/** A critic reply that demands another round. */
const DIRTY = "The error path is untested.\nMATERIAL_DEFECTS: 2\nVERDICT: revise\nSCORE: 0.40";

function settings(over: Partial<GauntletSettings> = {}): GauntletSettings {
  return { ...DEFAULT_SETTINGS, ...over };
}

beforeEach(() => {
  resetOverride();
  resetBudget();
});

describe("parseBool", () => {
  test("accepts the same token sets as the Python side", () => {
    for (const t of ["1", "true", "TRUE", " yes ", "y", "on", "enabled"]) {
      assert.equal(parseBool(t, false), true, t);
    }
    for (const t of ["0", "false", "False", "no", "n", "off", "disabled"]) {
      assert.equal(parseBool(t, true), false, t);
    }
  });

  test("keeps the default on unrecognized input rather than reading as false", () => {
    // A typo must not silently disable a loop that defaults to on.
    assert.equal(parseBool("ture", true), true);
    assert.equal(parseBool("", true), true);
    assert.equal(parseBool(undefined, true), true);
  });
});

describe("gauntletSettings", () => {
  test("defaults to on with the standard preset", () => {
    const s = gauntletSettings({});
    assert.equal(s.enabled, true);
    assert.equal(s.preset, "standard");
    assert.equal(roundsFor(s), 3);
  });

  test("GAUNTLET_LOOP=False disables it", () => {
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "False" }).enabled, false);
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "0" }).enabled, false);
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "off" }).enabled, false);
  });

  test("CLK_ROBUSTNESS_GAUNTLET also disables it", () => {
    assert.equal(gauntletSettings({ CLK_ROBUSTNESS_GAUNTLET: "off" }).enabled, false);
  });

  test("GAUNTLET_LOOP wins over CLK_ROBUSTNESS_GAUNTLET", () => {
    const s = gauntletSettings({
      CLK_ROBUSTNESS_GAUNTLET: "off",
      GAUNTLET_LOOP: "true",
    });
    assert.equal(s.enabled, true);
  });

  test("preset drives the round cap", () => {
    assert.equal(roundsFor(gauntletSettings({ CLK_GAUNTLET_PRESET: "quick" })), 1);
    assert.equal(roundsFor(gauntletSettings({ CLK_GAUNTLET_PRESET: "rigorous" })), 5);
  });

  test("an unknown preset falls back to standard instead of throwing", () => {
    assert.equal(gauntletSettings({ CLK_GAUNTLET_PRESET: "nonsense" }).preset, "standard");
  });

  test("an explicit max-rounds beats the preset", () => {
    const s = gauntletSettings({ CLK_GAUNTLET_PRESET: "quick", CLK_GAUNTLET_MAX_ROUNDS: "4" });
    assert.equal(roundsFor(s), 4);
  });

  test("junk numeric env values fall back rather than producing NaN", () => {
    const s = gauntletSettings({
      CLK_GAUNTLET_MAX_ROUNDS: "lots",
      CLK_GAUNTLET_ACCEPT_THRESHOLD: "high",
    });
    assert.equal(roundsFor(s), PRESET_ROUNDS.standard);
    assert.equal(s.acceptThreshold, 0.8);
  });

  test("focus lenses are appended to the preset's", () => {
    const s = gauntletSettings({ CLK_GAUNTLET_FOCUS: "accessibility, i18n" });
    const lenses = lensesFor(s);
    assert.ok(lenses.includes("accessibility"));
    assert.ok(lenses.includes("i18n"));
    assert.ok(lenses.includes("correctness"));
  });
});

describe("runtime override (/clk-gauntlet)", () => {
  test("parses the command tokens", () => {
    assert.deepEqual(parseGauntletCommand("off"), { enabled: false });
    assert.deepEqual(parseGauntletCommand("ON"), { enabled: true });
    assert.deepEqual(parseGauntletCommand("rigorous"), { preset: "rigorous", enabled: true });
    assert.equal(parseGauntletCommand("sideways"), null);
    assert.equal(parseGauntletCommand(""), null);
  });

  test("the override beats the environment", () => {
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "true" }).enabled, true);
    setOverride({ enabled: false });
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "true" }).enabled, false);
    assert.deepEqual(currentOverride(), { enabled: false });
  });

  test("naming a preset also switches the loop on", () => {
    const off = { GAUNTLET_LOOP: "false" };
    assert.equal(gauntletSettings(off).enabled, false);
    setOverride({ preset: "quick" });
    const s = gauntletSettings(off);
    assert.equal(s.enabled, true, "asking for a preset while off should turn it on");
    assert.equal(s.preset, "quick");
  });

  test("resetOverride restores environment control", () => {
    setOverride({ enabled: false });
    resetOverride();
    assert.equal(gauntletSettings({ GAUNTLET_LOOP: "true" }).enabled, true);
  });
});

describe("parseAnswerKey", () => {
  test("extracts checks from a well-formed block", () => {
    const checks = parseAnswerKey(
      "Here is my plan.\n\nANSWER_KEY:\n" +
        "- tests_pass: `pytest -q` exits 0\n" +
        "- handles_404: unknown ids return 404, not 500\n" +
        "END_ANSWER_KEY\n\nRest of the work.",
    );
    assert.equal(checks.length, 2);
    assert.equal(checks[0]?.id, "tests_pass");
    assert.equal(checks[1]?.condition, "unknown ids return 404, not 500");
  });

  test("returns [] when there is no block", () => {
    assert.deepEqual(parseAnswerKey("just some prose"), []);
    assert.deepEqual(parseAnswerKey(""), []);
  });

  test("skips unparseable lines instead of failing the whole key", () => {
    const checks = parseAnswerKey(
      "ANSWER_KEY:\n- ok_check: it works\n!!! garbage !!!\n# a comment\n- b: fine\nEND_ANSWER_KEY",
    );
    assert.deepEqual(checks.map((c) => c.id), ["ok_check", "b"]);
  });

  test("de-duplicates repeated check ids", () => {
    const checks = parseAnswerKey(
      "ANSWER_KEY:\n- a: first\n- A: second\nEND_ANSWER_KEY",
    );
    assert.equal(checks.length, 1);
    assert.equal(checks[0]?.condition, "first");
  });

  test("renders back to the block form", () => {
    assert.equal(renderAnswerKey([{ id: "a", condition: "b" }]), "- a: b");
    assert.match(renderAnswerKey([]), /no acceptance checks/);
  });
});

describe("parseCritique", () => {
  test("reads verdict, score, and material count", () => {
    const c = parseCritique(DIRTY);
    assert.equal(c.verdict, "revise");
    assert.equal(c.score, 0.4);
    assert.equal(c.materialDefects, 2);
    assert.equal(converged(c), false);
  });

  test("a clean critique converges", () => {
    assert.equal(converged(parseCritique(CLEAN)), true);
  });

  test("zero material defects converges even without an accept verdict", () => {
    // "A clean critique is a valid outcome" — cosmetic nits alone must not
    // buy another expensive round.
    const c = parseCritique("Some nits.\nMATERIAL_DEFECTS: 0\nVERDICT: revise\nSCORE: 0.9");
    assert.equal(converged(c), true);
  });

  test("an accept scored below the threshold is not an accept", () => {
    const c = parseCritique("VERDICT: accept\nSCORE: 0.30", 0.8);
    assert.equal(c.verdict, "revise");
    assert.ok(c.materialDefects >= 1);
  });

  test("a missing score defaults by verdict", () => {
    assert.equal(parseCritique("VERDICT: accept").score, 1.0);
    assert.equal(parseCritique("VERDICT: revise").score, 0.4);
  });

  test("unparseable output is treated as needing revision, not as passing", () => {
    const c = parseCritique("the critic rambled and said nothing structured");
    assert.equal(c.verdict, "revise");
    assert.equal(converged(c), false);
  });

  test("a reject never converges", () => {
    const c = parseCritique("MATERIAL_DEFECTS: 0\nVERDICT: reject\nSCORE: 0.1");
    assert.equal(converged(c), false);
  });
});

describe("runGauntlet", () => {
  test("is a no-op when disabled", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: "should never run", sessionId: "s" };
    };
    const res = await runGauntlet({
      agent: "engineer",
      task: "build it",
      candidate: GOOD,
      cwd: "/tmp",
      settings: settings({ enabled: false }),
      spawn,
    });
    assert.equal(calls, 0);
    assert.equal(res.output, GOOD);
    assert.equal(res.skipped, "disabled");
  });

  test("skips an empty candidate — there is nothing to critique", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: "x", sessionId: "s" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: "   ", cwd: "/tmp", spawn,
      settings: settings(),
    });
    assert.equal(calls, 0);
    assert.equal(res.skipped, "empty candidate");
  });

  test("stops after one round on a clean critique", async () => {
    const tasks: string[] = [];
    const spawn: SpawnFn = async (opts) => {
      tasks.push(opts.task);
      if (opts.task.includes(M_CRITIQUE)) return { output: CLEAN, sessionId: "c" };
      if (opts.task.includes(M_VERIFY)) return { output: CLEAN, sessionId: "v" };
      return { output: "ANSWER_KEY:\n- a: it works\nEND_ANSWER_KEY", sessionId: "k" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "build it", candidate: GOOD, cwd: "/tmp", spawn,
      settings: settings({ preset: "rigorous" }), // cap 5, but it should stop at 1
    });
    assert.equal(res.rounds.length, 1);
    assert.equal(res.converged, true);
    assert.equal(res.output, GOOD, "a converged candidate is returned unchanged");
    assert.equal(res.verified, "accept");
    assert.equal(tasks.filter((t) => t.includes(M_REVISE)).length, 0);
  });

  test("revises and stops at the round cap when the critic never accepts", async () => {
    let revisions = 0;
    const spawn: SpawnFn = async (opts) => {
      if (opts.task.includes(M_CRITIQUE)) return { output: DIRTY, sessionId: "c" };
      if (opts.task.includes(M_VERIFY)) return { output: CLEAN, sessionId: "v" };
      if (opts.task.includes(M_REVISE)) {
        revisions += 1;
        return { output: `revision ${revisions} ${GOOD}`, sessionId: `r${revisions}` };
      }
      return { output: "ANSWER_KEY:\n- a: it works\nEND_ANSWER_KEY", sessionId: "k" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "build it", candidate: GOOD, cwd: "/tmp", spawn,
      settings: settings({ preset: "quick" }), // cap 1
    });
    assert.equal(res.rounds.length, 1);
    assert.equal(res.converged, false);
    assert.equal(revisions, 1);
    assert.match(res.output, /^revision 1 /);
  });

  test("honors the preset round cap", async () => {
    const spawn: SpawnFn = async (opts) => {
      if (opts.task.includes(M_CRITIQUE)) return { output: DIRTY, sessionId: "c" };
      if (opts.task.includes(M_VERIFY)) return { output: CLEAN, sessionId: "v" };
      if (opts.task.includes(M_REVISE)) return { output: GOOD + " revised", sessionId: "r" };
      return { output: "ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY", sessionId: "k" };
    };
    for (const [preset, expected] of [["quick", 1], ["standard", 3], ["rigorous", 5]] as const) {
      const res = await runGauntlet({
        agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
        settings: settings({ preset }),
      });
      assert.equal(res.rounds.length, expected, preset);
    }
  });

  test("reuses the worker's own ANSWER_KEY instead of paying to derive one", async () => {
    const tasks: string[] = [];
    const spawn: SpawnFn = async (opts) => {
      tasks.push(opts.task);
      return { output: CLEAN, sessionId: "s" };
    };
    const candidate =
      "ANSWER_KEY:\n- mine: the worker wrote this\nEND_ANSWER_KEY\n\n" + GOOD;
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate, cwd: "/tmp", spawn, settings: settings(),
    });
    assert.deepEqual(res.checks.map((c) => c.id), ["mine"]);
    assert.equal(
      tasks.filter((t) => t.includes(M_KEY)).length, 0,
      "should not spend a spawn deriving a key the worker already supplied",
    );
  });

  test("derives a key when the worker supplied none", async () => {
    const spawn: SpawnFn = async (opts) => {
      if (opts.task.includes(M_KEY)) {
        return { output: "ANSWER_KEY:\n- derived: from the critic\nEND_ANSWER_KEY", sessionId: "k" };
      }
      return { output: CLEAN, sessionId: "s" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: settings(),
    });
    assert.deepEqual(res.checks.map((c) => c.id), ["derived"]);
  });

  test("repairs once when final verification still finds a defect", async () => {
    let repairs = 0;
    const spawn: SpawnFn = async (opts) => {
      if (opts.task.includes(M_CRITIQUE)) return { output: CLEAN, sessionId: "c" };
      if (opts.task.includes(M_VERIFY)) return { output: DIRTY, sessionId: "v" };
      if (opts.task.includes(M_REPAIR)) {
        repairs += 1;
        return { output: "repaired " + GOOD, sessionId: "p" };
      }
      return { output: "ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY", sessionId: "k" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: settings(),
    });
    assert.equal(repairs, 1, "exactly one bounded repair, never a second");
    assert.match(res.output, /^repaired /);
  });

  test("keeps the candidate when the critic spawn throws", async () => {
    const spawn: SpawnFn = async () => {
      throw new Error("tmux exploded");
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: settings(),
    });
    assert.equal(res.output, GOOD, "a broken critic must never lose the work");
  });

  test("keeps the last good candidate when a revision comes back empty", async () => {
    const spawn: SpawnFn = async (opts) => {
      if (opts.task.includes(M_CRITIQUE)) return { output: DIRTY, sessionId: "c" };
      if (opts.task.includes(M_REVISE)) return { output: "", sessionId: "r" };
      return { output: "", sessionId: "k" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
      settings: settings({ finalVerification: false }),
    });
    assert.equal(res.output, GOOD);
  });

  test("stops early when the abort signal fires", async () => {
    const controller = new AbortController();
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      controller.abort();
      return { output: DIRTY, sessionId: "c" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
      signal: controller.signal, settings: settings({ preset: "rigorous" }),
    });
    assert.ok(calls <= 2, `expected an early stop, got ${calls} spawns`);
    assert.equal(res.output, GOOD);
  });

  test("skips the final verification when it is turned off", async () => {
    const tasks: string[] = [];
    const spawn: SpawnFn = async (opts) => {
      tasks.push(opts.task);
      return { output: CLEAN, sessionId: "s" };
    };
    await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
      settings: settings({ finalVerification: false }),
    });
    assert.equal(tasks.filter((t) => t.includes(M_VERIFY)).length, 0);
  });

  test("summarises what it did", () => {
    const line = summariseGauntlet({
      output: "x", checks: [{ id: "a", condition: "b" }],
      rounds: [{ round: 1, verdict: "accept", score: 0.9, materialDefects: 0 }],
      converged: true, spawns: 2, verified: "accept",
    });
    assert.match(line, /1 critique round/);
    assert.match(line, /converged/);
    assert.match(summariseGauntlet({
      output: "x", checks: [], rounds: [], converged: false, spawns: 0, skipped: "disabled",
    }), /skipped \(disabled\)/);
  });
});

describe("session dispatch budget", () => {
  test("defaults to 500", () => {
    assert.equal(gauntletSettings({}).maxDispatches, 500);
    assert.equal(gauntletSettings({ CLK_GAUNTLET_MAX_DISPATCHES: "42" }).maxDispatches, 42);
  });

  test("junk env falls back to the default", () => {
    assert.equal(gauntletSettings({ CLK_GAUNTLET_MAX_DISPATCHES: "many" }).maxDispatches, 500);
  });

  test("stops spending once the budget is exhausted", async () => {
    let calls = 0;
    // Stage-aware stub: a revision must return worker text, not critic text.
    const spawn: SpawnFn = async (opts) => {
      calls += 1;
      if (opts.task.includes(M_CRITIQUE)) return { output: DIRTY, sessionId: "c" };
      if (opts.task.includes(M_REVISE)) return { output: "revised " + GOOD, sessionId: "r" };
      return { output: "ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY", sessionId: "k" };
    };
    const res = await runGauntlet({
      agent: "engineer", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
      // key + critique + revise = 3, then the budget stops the next critique.
      settings: settings({ preset: "rigorous", maxDispatches: 3 }),
    });
    assert.equal(calls, 3, "must not spawn past the budget");
    assert.equal(res.skipped, "dispatch budget exhausted");
    assert.match(res.output, /^revised /, "work done before the cap is kept");
  });

  test("the budget spans calls, not just one dispatch", async () => {
    // The whole point: a round cap resets per dispatch, so only a session
    // budget bounds a long run.
    const spawn: SpawnFn = async () => ({ output: CLEAN, sessionId: "s" });
    const s = settings({ maxDispatches: 4 });
    await runGauntlet({ agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s });
    const firstRun = budgetSpent();
    assert.ok(firstRun > 0);
    await runGauntlet({ agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s });
    assert.ok(budgetSpent() <= 4, `budget overspent: ${budgetSpent()}`);
  });

  test("a later call is skipped outright once the budget is gone", async () => {
    const spawn: SpawnFn = async () => ({ output: CLEAN, sessionId: "s" });
    const s = settings({ maxDispatches: 1 });
    await runGauntlet({ agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s });
    const second = await runGauntlet({
      agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s,
    });
    assert.equal(second.skipped, "dispatch budget exhausted");
  });

  test("exhaustion is reported once, not on every later call", () => {
    // A long run must not repeat the same notice hundreds of times.
    assert.equal(claimBudgetReport(), true);
    assert.equal(claimBudgetReport(), false);
    assert.equal(claimBudgetReport(), false);
    resetBudget();
    assert.equal(claimBudgetReport(), true, "a fresh run reports again");
  });

  test("0 means unlimited", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: DIRTY, sessionId: "s" };
    };
    await runGauntlet({
      agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn,
      settings: settings({ preset: "rigorous", maxDispatches: 0 }),
    });
    // key + 5 x (critique + revise) + verify + repair
    assert.ok(calls > 5, `expected an uncapped run, got ${calls} spawns`);
  });

  test("resetBudget restores a fresh budget", async () => {
    const spawn: SpawnFn = async () => ({ output: CLEAN, sessionId: "s" });
    // 3 is enough for one clean pass: key + critique + verification.
    const s = settings({ maxDispatches: 3 });
    const first = await runGauntlet({
      agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s,
    });
    assert.equal(first.skipped, undefined);

    // Without a reset the next run is refused outright...
    const second = await runGauntlet({
      agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s,
    });
    assert.equal(second.skipped, "dispatch budget exhausted");

    // ...and a reset (what /clk does per run) restores it.
    resetBudget();
    assert.equal(budgetSpent(), 0);
    const third = await runGauntlet({
      agent: "a", task: "t", candidate: GOOD, cwd: "/tmp", spawn, settings: s,
    });
    assert.equal(third.skipped, undefined);
  });
});

describe("integration with the dispatch paths", () => {
  test("dispatchWithQuality runs the gauntlet over the accepted candidate", async () => {
    const tasks: string[] = [];
    const spawn: SpawnFn = async (opts) => {
      tasks.push(opts.task);
      if (opts.task.includes(M_CRITIQUE)) return { output: CLEAN, sessionId: "c" };
      if (opts.task.includes(M_VERIFY)) return { output: CLEAN, sessionId: "v" };
      if (opts.task.includes(M_KEY)) {
        return { output: "ANSWER_KEY:\n- a: x\nEND_ANSWER_KEY", sessionId: "k" };
      }
      return { output: GOOD, sessionId: "s" };
    };
    const res = await dispatchWithQuality({
      agent: "worker", task: "do the thing", cwd: "/tmp", spawn,
      gauntlet: settings(),
    });
    assert.ok(res.gauntlet, "the gauntlet result should be reported back");
    assert.equal(res.gauntlet?.converged, true);
    assert.equal(res.output, GOOD);
    assert.ok(tasks.some((t) => t.includes(M_CRITIQUE)));
  });

  test("gauntlet: false leaves the dispatch path untouched", async () => {
    let calls = 0;
    const spawn: SpawnFn = async () => {
      calls += 1;
      return { output: GOOD, sessionId: "s" };
    };
    const res = await dispatchWithQuality({
      agent: "worker", task: "do the thing", cwd: "/tmp", spawn, gauntlet: false,
    });
    assert.equal(calls, 1);
    assert.equal(res.gauntlet, undefined);
  });
});
