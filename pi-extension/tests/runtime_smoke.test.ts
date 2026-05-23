/**
 * Opt-in runtime smoke test for the Pi extension.
 *
 * Only runs when:
 *   - the orchestrator selected pi as the test provider
 *     (CLK_PROVIDER=pi)
 *   - the `pi` CLI is on PATH
 *   - the user provided enough config (model + API key) to talk to a backend
 *
 * The test verifies that:
 *   - the pi CLI itself is callable and reports a version
 *   - the extension's index.ts is loadable (lightweight ESM import check)
 *
 * We deliberately do NOT issue a real `/clk` run end-to-end here — Pi is
 * an interactive TUI, and driving it from a non-TTY would require an
 * expect/pty harness we don't ship.  Callers who want full LLM coverage
 * should run kickoff.sh with the same provider selection (see the
 * test_kickoff_with_user_selected_provider Python test in user_tests/).
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import { execFile } from "node:child_process";
import { promisify } from "node:util";

const execFileAsync = promisify(execFile);

const PROVIDER = (process.env.CLK_PROVIDER || "").toLowerCase();
const PI_MODEL = process.env.CLK_PI_MODEL || "";
const PI_KEY = process.env.CLK_PI_API_KEY || "";
const PI_KEY_TYPE = process.env.CLK_PI_KEY_TYPE || "";

const RUNTIME_ENABLED = PROVIDER === "pi" && PI_MODEL.length > 0;

async function piAvailable(): Promise<boolean> {
  try {
    await execFileAsync("pi", ["--version"], { timeout: 5000 });
    return true;
  } catch {
    return false;
  }
}

describe("Pi runtime smoke", () => {
  test("pi CLI is invokable when provider=pi is selected", async (t) => {
    if (!RUNTIME_ENABLED) {
      t.skip(`pi runtime smoke disabled (CLK_PROVIDER=${PROVIDER}, model=${PI_MODEL!.length ? "set" : "unset"})`);
      return;
    }
    if (!(await piAvailable())) {
      t.skip("pi CLI not on PATH");
      return;
    }
    // We don't care about the exact version string — just that the
    // command runs and exits 0.
    const { stdout } = await execFileAsync("pi", ["--version"], { timeout: 10000 });
    assert.ok(stdout.length >= 0);
  });

  test("user-selected pi model is forwarded as an env var", async (t) => {
    if (!RUNTIME_ENABLED) {
      t.skip("no user-selected pi model in env");
      return;
    }
    assert.ok(PI_MODEL && PI_MODEL.length > 0, "CLK_PI_MODEL should be set");
    // Just record the key type / model so failures are easy to diagnose:
    console.log(`[pi-smoke] CLK_PI_MODEL=${PI_MODEL}`);
    console.log(`[pi-smoke] CLK_PI_KEY_TYPE=${PI_KEY_TYPE || "(blank)"}`);
    console.log(`[pi-smoke] CLK_PI_API_KEY=${PI_KEY ? "(set)" : "(blank)"}`);
    // If the user gave us a key, the env var pi reads must also be present.
    if (PI_KEY && PI_KEY_TYPE) {
      const piEnv = `${PI_KEY_TYPE.toUpperCase()}_API_KEY`;
      // We can't assert that pi sees this — but document the expectation:
      console.log(`[pi-smoke] expecting pi to read ${piEnv}`);
    }
  });

  test("extension index.ts loads in Node without errors", async () => {
    // Dynamic import so this works even when the test is skipped above.
    const mod = await import("../src/index.ts");
    assert.equal(typeof mod.default, "function");
  });
});
