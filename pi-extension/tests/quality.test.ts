/**
 * Unit tests for src/quality.ts — pure regex/string scoring, no I/O.
 * Mirrors tests/test_response_quality.py in the Python harness so a
 * behaviour drift between the two implementations shows up here.
 */
import { test, describe } from "node:test";
import assert from "node:assert/strict";

import {
  scoreResponse,
  repairHint,
  isRecoverable,
  summarise,
} from "../src/quality.ts";

describe("scoreResponse — happy paths", () => {
  test("substantive prose passes with score 1.0", () => {
    const text = "This is a substantive response covering the requested work in detail. " +
      "It explains the approach, lists the files touched, and states next steps so the " +
      "chief can keep moving without a re-roll.";
    const q = scoreResponse(text);
    assert.equal(q.ok, true);
    assert.equal(q.score, 1.0);
    assert.deepEqual(q.flags, []);
  });

  test("substantive prose with a CONFIDENCE line is still ok", () => {
    const text = "Substantive enough response that exceeds the forty-character " +
      "minimum easily.\nCONFIDENCE: 0.82";
    const q = scoreResponse(text);
    assert.equal(q.ok, true);
    assert.equal(q.confidence, 0.82);
  });
});

describe("scoreResponse — failure modes", () => {
  test("empty body flags as 'empty' and is recoverable", () => {
    const q = scoreResponse("");
    assert.equal(q.ok, false);
    assert.ok(q.flags.includes("empty"));
    assert.equal(q.recoverable, true);
  });

  test("near-empty body flags as 'empty' too", () => {
    const q = scoreResponse("hi.");
    assert.equal(q.ok, false);
    assert.ok(q.flags.includes("empty"));
  });

  test("refusal phrase is flagged and NOT recoverable", () => {
    const q = scoreResponse("I cannot help with that request. As an AI language model, ...");
    assert.equal(q.ok, false);
    assert.ok(q.flags.includes("refusal"));
    assert.equal(q.recoverable, false);
  });

  test("missing END_ACTION imbalance is flagged", () => {
    const text = "Plenty of text here so we beat the empty threshold easily " +
      "and definitely.\nACTION: write_file\nfoo\n"; // no END_ACTION
    const q = scoreResponse(text);
    assert.ok(q.flags.includes("malformed_action"));
  });

  test("missing END_POST imbalance is flagged", () => {
    const text = "More than forty characters of preamble so the empty check passes.\n" +
      "POST: my_topic\nbody\n";
    const q = scoreResponse(text);
    assert.ok(q.flags.includes("malformed_post"));
  });

  test("low CONFIDENCE value gets the low_confidence flag", () => {
    const text = "Long enough body to clear the empty threshold without question.\nCONFIDENCE: 0.10";
    const q = scoreResponse(text);
    assert.ok(q.flags.includes("low_confidence"));
    assert.equal(q.confidence, 0.1);
  });

  test("NEEDS_REVIEW: true flips needs_review_self", () => {
    const text = "Body comfortably over the forty character minimum so empty does not fire.\n" +
      "NEEDS_REVIEW: true";
    const q = scoreResponse(text);
    assert.equal(q.needsReview, true);
    assert.ok(q.flags.includes("needs_review_self"));
  });

  test("missing expected output keys gets outputs_missing flag", () => {
    const text = "A response body comfortably exceeding the minimum length threshold.\n" +
      "POST: t1\nPRODUCES: foo, bar\nbody\nEND_POST";
    const q = scoreResponse(text, { expectedOutputs: ["foo", "missing_one"] });
    assert.ok(q.flags.includes("outputs_missing"));
    assert.match(q.reasons.join(" "), /missing_one/);
  });

  test("requireConfidence flag fires when CONFIDENCE absent", () => {
    const text = "Long enough body to comfortably clear the minimum length threshold.";
    const q = scoreResponse(text, { requireConfidence: true });
    assert.ok(q.flags.includes("confidence_missing"));
  });
});

describe("repairHint / isRecoverable / summarise", () => {
  test("repairHint returns an empty string for an ok response", () => {
    const q = scoreResponse("Long-enough response that passes the empty threshold.");
    assert.equal(repairHint(q), "");
  });

  test("repairHint quotes every reason as a bullet for failed responses", () => {
    const q = scoreResponse("hi");
    const hint = repairHint(q);
    assert.match(hint, /rejected by the harness/i);
    assert.match(hint, /minimum 40/);
  });

  test("isRecoverable is true for recoverable failures, false for refusals", () => {
    assert.equal(isRecoverable(scoreResponse("")), true);
    assert.equal(isRecoverable(scoreResponse("I cannot help with this.")), false);
    assert.equal(isRecoverable(scoreResponse("ok and substantive response over the minimum.")), false);
  });

  test("summarise gives a compact one-line description", () => {
    const ok = scoreResponse("Long substantive response well over the minimum threshold.");
    assert.match(summarise(ok), /^ok score=/);
    const bad = scoreResponse("");
    assert.match(summarise(bad), /^flags=empty score=/);
  });
});
