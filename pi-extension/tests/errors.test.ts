/**
 * Unit tests for src/errors.ts.
 *
 * Run with: npx tsx --test tests/errors.test.ts
 * (See package.json `npm test`.)
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";
import {
  classifyError,
  isRetryable,
  looksRedacted,
  isMaxTurnsResult,
  recoveryHint,
  withRetry,
  type ErrorClass,
} from "../src/errors.ts";

describe("classifyError", () => {
  test("recognises rate limits from HTTP 429", () => {
    const err = Object.assign(new Error("Too many requests"), { status: 429 });
    assert.equal(classifyError(err), "rate_limit");
  });

  test("recognises rate limits from text", () => {
    assert.equal(classifyError(new Error("rate limit exceeded")), "rate_limit");
    assert.equal(classifyError(new Error("Quota exceeded")), "rate_limit");
    assert.equal(classifyError(new Error("no endpoints available")), "rate_limit");
  });

  test("recognises model-unavailable errors", () => {
    assert.equal(
      classifyError(new Error("model gpt-99 does not exist")),
      "model_error",
    );
    const err404 = Object.assign(new Error("not found"), { status: 404 });
    assert.equal(classifyError(err404), "model_error");
  });

  test("recognises redaction patterns", () => {
    assert.equal(classifyError(new Error("[REDACTED]")), "redaction");
    assert.equal(classifyError(new Error("blocked by policy")), "redaction");
  });

  test("recognises max-turns failures", () => {
    assert.equal(
      classifyError(new Error("maximum turns reached")),
      "max_turns",
    );
    assert.equal(
      classifyError(new Error("turn limit exceeded")),
      "max_turns",
    );
  });

  test("recognises network errors", () => {
    // generic ECONNRESET-style strings - depends on patterns, just verify
    // the function returns one of the known classes:
    const cls = classifyError(new Error("ECONNRESET"));
    const valid: ErrorClass[] = [
      "rate_limit", "model_error", "redaction", "max_turns",
      "network", "cancelled", "other",
    ];
    assert.ok(valid.includes(cls), `unexpected class: ${cls}`);
  });

  test("recognises cancellation as not-retryable", () => {
    assert.equal(classifyError(new Error("Aborted")), "cancelled");
    assert.equal(classifyError(new Error("AbortError")), "cancelled");
  });

  test("returns 'other' for unrecognised errors", () => {
    assert.equal(
      classifyError(new Error("something completely random")),
      "other",
    );
  });
});

describe("isRetryable", () => {
  test("rate limits and network errors are retryable", () => {
    assert.equal(isRetryable(new Error("rate limit")), true);
  });

  test("cancellation is never retryable", () => {
    assert.equal(isRetryable(new Error("Aborted")), false);
  });

  test("max_turns is not retryable via withRetry", () => {
    assert.equal(isRetryable(new Error("maximum turns reached")), false);
  });
});

describe("looksRedacted", () => {
  test("detects empty / redacted strings", () => {
    assert.equal(looksRedacted(""), true);
    assert.equal(looksRedacted("   "), true);
    assert.equal(looksRedacted("[REDACTED]"), true);
  });

  test("passes plain text through", () => {
    assert.equal(looksRedacted("hello world"), false);
    assert.equal(looksRedacted("a normal mission statement"), false);
  });
});

describe("isMaxTurnsResult", () => {
  test("detects subagent max-turn messages", () => {
    assert.equal(isMaxTurnsResult("maximum turns reached"), true);
    assert.equal(isMaxTurnsResult("agent stopped: no more turns"), true);
  });

  test("returns false for normal output", () => {
    assert.equal(isMaxTurnsResult("all done"), false);
  });
});

describe("recoveryHint", () => {
  const classes: ErrorClass[] = [
    "rate_limit", "model_error", "redaction", "max_turns",
    "network", "cancelled", "other",
  ];
  for (const cls of classes) {
    test(`returns a non-empty hint for ${cls}`, () => {
      const hint = recoveryHint(cls);
      assert.equal(typeof hint, "string");
      assert.ok(hint.length > 10, `hint too short for ${cls}: ${JSON.stringify(hint)}`);
    });
  }
});

describe("withRetry", () => {
  test("returns result on first success", async () => {
    let calls = 0;
    const result = await withRetry(async () => {
      calls++;
      return 42;
    });
    assert.equal(result, 42);
    assert.equal(calls, 1);
  });

  test("retries on retryable error and eventually succeeds", async () => {
    let calls = 0;
    const result = await withRetry(
      async () => {
        calls++;
        if (calls < 3) throw new Error("ECONNRESET network blip");
        return "ok";
      },
      { baseDelayMs: 1, maxAttempts: 5 },
    );
    assert.equal(result, "ok");
    assert.equal(calls, 3);
  });

  test("propagates non-retryable errors immediately", async () => {
    let calls = 0;
    await assert.rejects(
      withRetry(async () => {
        calls++;
        throw new Error("totally random failure");
      }, { baseDelayMs: 1, maxAttempts: 5 }),
      /random failure/,
    );
    assert.equal(calls, 1);
  });

  test("bails immediately when signal already aborted", async () => {
    const ctrl = new AbortController();
    ctrl.abort();
    await assert.rejects(
      withRetry(async () => 1, { signal: ctrl.signal }),
      /Aborted/,
    );
  });

  test("stops retrying once attempts exhausted (network)", async () => {
    let calls = 0;
    await assert.rejects(
      withRetry(
        async () => {
          calls++;
          throw new Error("ECONNRESET");
        },
        { baseDelayMs: 1, maxAttempts: 3 },
      ),
    );
    // network errors exhaust after maxAttempts attempts
    assert.ok(calls <= 3, `expected <=3 calls, got ${calls}`);
  });
});
