/**
 * Response-quality scorer — TypeScript port of
 * clk_harness/orchestration/response_quality.py.
 *
 * Used by clk_consensus to pick the best of N stochastic samples and by
 * the clk_subagent quality re-dispatch loop to detect (and re-roll on)
 * empty / refused / malformed / low-confidence subagent outputs without
 * a single extra provider call. All checks are pure string / regex
 * operations.
 *
 * Mirrors the Python harness so a behaviour change in either side stays
 * one diff away from a matching change in the other.
 */

export interface ResponseQuality {
  ok: boolean;
  /** Rough 0..1 score, 1.0 = clean, lower = more flags / more severe. */
  score: number;
  flags: string[];
  reasons: string[];
  /**
   * False when the response should NOT be retried (an explicit refusal,
   * for instance — re-rolling will just produce the same refusal). The
   * caller is expected to escalate rather than retry in that case.
   */
  recoverable: boolean;
  /** CONFIDENCE: <n> line value, if present and parseable. */
  confidence?: number;
  /** NEEDS_REVIEW: true|false line value, if present. */
  needsReview?: boolean;
}

export interface ScoreOpts {
  /** Text shorter than this counts as "empty". Default 40. */
  minChars?: number;
  /**
   * When provided, every key must appear in some POST block's PRODUCES:
   * list for the response to pass. Empty array disables the check.
   */
  expectedOutputs?: string[];
  /**
   * When true, missing the CONFIDENCE: line itself becomes a flag.
   * Default false so existing prompts aren't penalised retroactively.
   */
  requireConfidence?: boolean;
}

const CONFIDENCE_RE = /^\s*CONFIDENCE\s*:\s*([0-9]*\.?[0-9]+)\s*$/im;
const NEEDS_REVIEW_RE = /^\s*NEEDS_REVIEW\s*:\s*(true|yes|y|1|false|no|n|0)\s*$/im;
const REFUSAL_RES: RegExp[] = [
  /\bi\s+cannot\b/i,
  /\bi\s+can'?t\b\s+(?:help|assist|do|comply)/i,
  /\bi\s+(?:am|'m)\s+(?:sorry|unable)\b.*\b(?:cannot|can'?t|won'?t)\b/i,
  /\bas\s+an\s+ai\s+(?:language\s+)?model\b/i,
  /\bI\s+do\s+not\s+have\s+the\s+ability\b/i,
];
const HEADER_ACTION_RE = /^\s*ACTION\s*:\s*([A-Za-z]+)/gim;
const END_ACTION_RE = /^\s*END_ACTION\s*$/gim;
const POST_HEAD_RE = /^\s*POST\s*:\s*([A-Za-z][A-Za-z0-9_]*)\s*$/gim;
const POST_END_RE = /^\s*END_POST\s*$/gim;
const PRODUCES_RE = /^\s*PRODUCES\s*:\s*(.+)$/gim;

function parseConfidence(text: string): number | undefined {
  const m = CONFIDENCE_RE.exec(text);
  if (!m) return undefined;
  let v = Number.parseFloat(m[1]!);
  if (Number.isNaN(v)) return undefined;
  if (v < 0) v = 0;
  if (v > 1) v = Math.min(1, v / 100);
  return v;
}

function parseNeedsReview(text: string): boolean | undefined {
  const m = NEEDS_REVIEW_RE.exec(text);
  if (!m) return undefined;
  return ["true", "yes", "y", "1"].includes(m[1]!.toLowerCase());
}

function detectRefusal(text: string): boolean {
  return REFUSAL_RES.some((re) => re.test(text));
}

function countMatches(re: RegExp, text: string): number {
  // Global regexes need a fresh lastIndex on each call.
  const fresh = new RegExp(re.source, re.flags);
  let n = 0;
  while (fresh.exec(text) !== null) n++;
  return n;
}

function actionBlockImbalance(text: string): number {
  const heads = countMatches(HEADER_ACTION_RE, text);
  if (heads === 0) return 0;
  const ends = countMatches(END_ACTION_RE, text);
  return heads - ends;
}

function postBlockImbalance(text: string): number {
  const heads = countMatches(POST_HEAD_RE, text);
  if (heads === 0) return 0;
  const ends = countMatches(POST_END_RE, text);
  return heads - ends;
}

function declaredProduces(text: string): Set<string> {
  const out = new Set<string>();
  const fresh = new RegExp(PRODUCES_RE.source, PRODUCES_RE.flags);
  let m: RegExpExecArray | null;
  while ((m = fresh.exec(text)) !== null) {
    for (const key of m[1]!.split(",")) {
      const k = key.trim();
      if (k) out.add(k);
    }
  }
  return out;
}

function missingOutputs(text: string, expected: string[]): string[] {
  if (expected.length === 0) return [];
  const declared = declaredProduces(text);
  return expected.filter((k) => !declared.has(k));
}

/**
 * Score a single response text against the harness's quality rules.
 *
 * Always returns a `ResponseQuality` — never throws on a malformed
 * input, so callers can use the score even when the upstream provider
 * returned junk.
 */
export function scoreResponse(
  text: string | null | undefined,
  opts: ScoreOpts = {},
): ResponseQuality {
  const minChars = opts.minChars ?? 40;
  const expected = opts.expectedOutputs ?? [];
  const requireConfidence = opts.requireConfidence ?? false;

  const raw = text ?? "";
  const body = raw.trim();
  const flags: string[] = [];
  const reasons: string[] = [];
  let recoverable = true;
  const confidence = parseConfidence(raw);
  const needsReview = parseNeedsReview(raw);

  if (body.length < Math.max(1, minChars)) {
    flags.push("empty");
    reasons.push(
      `Response body was ${body.length} chars (minimum ${minChars}). Re-emit a substantive response.`,
    );
  }
  if (detectRefusal(raw)) {
    flags.push("refusal");
    reasons.push(
      "Response looked like a refusal. The task is in-scope for this harness; respond directly or, " +
        "if blocked, explain the obstacle so the chief can re-cast or escalate.",
    );
    recoverable = false;
  }
  // Tolerate a single unclosed block: providers routinely truncate the
  // final ACTION block at the response cap, and the parser accepts an
  // EOF-terminated last block. Two or more unclosed blocks means the
  // worker genuinely doesn't emit END_ACTION. (Mirrors the Python
  // harness's `act_balance > 1` rule.)
  const actBal = actionBlockImbalance(raw);
  if (actBal > 1) {
    flags.push("malformed_action");
    reasons.push(
      `${actBal} ACTION header(s) had no matching END_ACTION. Every ACTION block must terminate with a line END_ACTION.`,
    );
  }
  const postBal = postBlockImbalance(raw);
  if (postBal > 0) {
    flags.push("malformed_post");
    reasons.push(
      `${postBal} POST header(s) had no matching END_POST. Every POST block must terminate with a line END_POST.`,
    );
  }
  const missing = missingOutputs(raw, expected);
  if (missing.length > 0) {
    flags.push("outputs_missing");
    // Concrete copy-paste example with the actual missing keys filled in —
    // a generic "include a PRODUCES list" hint left small models guessing.
    const producesLine = missing.join(", ");
    reasons.push(
      "Declared output contract keys not satisfied: " +
        missing.join(", ") +
        ". You MUST emit a POST block that lists every missing key in its " +
        "PRODUCES line. Exact format:\n" +
        "  POST: finding\n" +
        `  PRODUCES: ${producesLine}\n` +
        "  BODY:\n" +
        "  <your summary here>\n" +
        "  END_POST\n" +
        "The PRODUCES line must contain every unsatisfied key above, " +
        "comma-separated on a single line.",
    );
  }
  if (confidence !== undefined && confidence < 0.5) {
    flags.push("low_confidence");
    reasons.push(
      `You reported CONFIDENCE: ${confidence.toFixed(2)}. Either improve the response or escalate.`,
    );
  }
  if (needsReview === true) {
    flags.push("needs_review_self");
    reasons.push(
      "You set NEEDS_REVIEW: true. Sharpen the answer or call out the specific uncertainty.",
    );
  }
  if (requireConfidence && confidence === undefined) {
    flags.push("confidence_missing");
    reasons.push(
      "Response did not include a CONFIDENCE: <0..1> line. Emit one final line stating your confidence so the harness can decide whether to re-sample.",
    );
  }

  const deductions: Record<string, number> = {
    empty: 0.6,
    refusal: 0.5,
    malformed_action: 0.4,
    malformed_post: 0.3,
    outputs_missing: 0.4,
    low_confidence: 0.3,
    needs_review_self: 0.2,
    confidence_missing: 0.1,
  };
  let s = 1.0;
  for (const f of flags) s -= deductions[f] ?? 0.2;
  const score = Math.max(0, Math.round(s * 1000) / 1000);

  return {
    ok: flags.length === 0,
    score,
    flags,
    reasons,
    recoverable,
    confidence,
    needsReview,
  };
}

/**
 * Build a re-dispatch preamble that names every flag so the worker
 * fixes the specific issues instead of re-rolling at random.
 */
export function repairHint(q: ResponseQuality): string {
  if (q.ok || q.reasons.length === 0) return "";
  const bullets = q.reasons.map((r) => `- ${r}`).join("\n");
  return (
    "Your previous response was rejected by the harness for the following reasons:\n" +
    bullets +
    "\nRe-emit a complete response that fixes every item above."
  );
}

/** Convenience: is the verdict worth re-rolling on? */
export function isRecoverable(q: ResponseQuality): boolean {
  return !q.ok && q.recoverable;
}

const PROGRESS_RE = /^\s*PROGRESS\s*:\s*(yes|no|true|false)\s*$/gim;

/**
 * Self-reported per-turn progress marker — port of the Python harness's
 * response_quality.progress_signal(). Returns true/false for an explicit
 * `PROGRESS: yes|no` line (the LAST marker wins when several appear),
 * or undefined when the response carries no marker. Callers feed this
 * into stall detection: an explicit "no" counts against the no-progress
 * budget even when files changed (busywork detection).
 */
export function progressSignal(text: string | null | undefined): boolean | undefined {
  if (!text) return undefined;
  const fresh = new RegExp(PROGRESS_RE.source, PROGRESS_RE.flags);
  let last: string | undefined;
  let m: RegExpExecArray | null;
  while ((m = fresh.exec(text)) !== null) last = m[1]!.toLowerCase();
  if (last === undefined) return undefined;
  return last === "yes" || last === "true";
}

export function summarise(q: ResponseQuality): string {
  if (q.ok) return `ok score=${q.score.toFixed(2)}`;
  return `flags=${q.flags.join(",") || "?"} score=${q.score.toFixed(2)}`;
}
