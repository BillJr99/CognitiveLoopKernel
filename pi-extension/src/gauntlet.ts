/**
 * Gauntlet loop — layer 12 of the robustness loops.
 *
 * Port of clk_harness/orchestration/gauntlet.py. Keep the two in step:
 * the presets, round caps, env-var names, boolean token sets, and the
 * VERDICT / SCORE / MATERIAL_DEFECTS output contract are all shared, so a
 * drift between them is a bug in whichever side moved.
 *
 * Every other critique layer here judges a subagent's output against a
 * critic's in-the-moment opinion, so "good" gets invented after the work
 * is already done. The gauntlet inverts that order:
 *
 *   1. Answer key       — checkable acceptance criteria, written first.
 *      Reuses the worker's own ANSWER_KEY: block when it emitted one
 *      (free); otherwise one derivation spawn.
 *   2. Candidate 0      — the existing dispatch, untouched.
 *   3. Critique         — adversarial, judged against the key.
 *   4. Revise + iterate — until no material defect or the round cap.
 *   5. Final verify     — against the original task plus every check,
 *      with one bounded final repair.
 *
 * Presets cap the critique rounds: quick=1, standard=3 (default),
 * rigorous=5. Rounds stop early on a clean critique, so the cap is a
 * worst case rather than the usual spend.
 *
 * Kill switches, highest precedence first: the /clk-gauntlet slash
 * command, GAUNTLET_LOOP=false, CLK_ROBUSTNESS_GAUNTLET=off.
 */

import { spawnSubagent as defaultSpawnSubagent } from "./subagent.js";
import type { SpawnFn } from "./consensus.js";

// ---------------------------------------------------------------------------
// Presets
// ---------------------------------------------------------------------------

export type GauntletPreset = "quick" | "standard" | "rigorous";

/** Round caps per preset. Mirrors gauntlet.py PRESET_ROUNDS. */
export const PRESET_ROUNDS: Record<GauntletPreset, number> = {
  quick: 1,
  standard: 3,
  rigorous: 5,
};

export const DEFAULT_PRESET: GauntletPreset = "standard";

/** Critique lenses per preset. Mirrors gauntlet.py PRESET_LENSES. */
export const PRESET_LENSES: Record<GauntletPreset, readonly string[]> = {
  quick: ["requirements", "correctness"],
  standard: [
    "requirements",
    "correctness",
    "reasoning",
    "hidden assumptions",
    "edge cases",
    "feasibility",
  ],
  rigorous: [
    "requirements",
    "factual correctness",
    "reasoning validity",
    "hidden assumptions",
    "counterexamples",
    "edge cases",
    "internal consistency",
    "evidence quality",
    "implementation feasibility",
    "user-impact risk",
  ],
};

function isPreset(value: string): value is GauntletPreset {
  return value === "quick" || value === "standard" || value === "rigorous";
}

// ---------------------------------------------------------------------------
// Settings
// ---------------------------------------------------------------------------

/**
 * Same token sets as gauntlet.py parse_bool. The rest of this extension
 * uses a bare `=== "true"` check (tools.ts CLK_GITHUB_PUSH_ON_COMMIT), but
 * that would make GAUNTLET_LOOP=0 behave differently here than on the
 * Python side, so the gauntlet parses the full set both ways.
 */
const TRUE_TOKENS = new Set(["1", "true", "yes", "y", "on", "enabled"]);
const FALSE_TOKENS = new Set(["0", "false", "no", "n", "off", "disabled"]);

/**
 * Strict tri-state boolean. An unrecognized value keeps the default rather
 * than silently reading as false — the gauntlet defaults to on, and a typo
 * in GAUNTLET_LOOP should not quietly switch it off.
 */
export function parseBool(value: string | undefined, fallback: boolean): boolean {
  if (value === undefined) return fallback;
  const token = value.trim().toLowerCase();
  if (!token) return fallback;
  if (TRUE_TOKENS.has(token)) return true;
  if (FALSE_TOKENS.has(token)) return false;
  return fallback;
}

export interface GauntletSettings {
  enabled: boolean;
  preset: GauntletPreset;
  /** Critique rounds per dispatch. 0 = derive from the preset (default 3). */
  maxRounds: number;
  /** Total gauntlet spawns allowed for the whole session. 0 = unlimited. */
  maxDispatches: number;
  answerKey: boolean;
  finalVerification: boolean;
  acceptThreshold: number;
  focus: string[];
}

export const DEFAULT_SETTINGS: GauntletSettings = {
  enabled: true,
  preset: DEFAULT_PRESET,
  maxRounds: 0,
  maxDispatches: 500,
  answerKey: true,
  finalVerification: true,
  acceptThreshold: 0.8,
  focus: [],
};

/** Effective round cap: an explicit maxRounds wins over the preset. */
export function roundsFor(settings: GauntletSettings): number {
  if (settings.maxRounds > 0) return settings.maxRounds;
  return PRESET_ROUNDS[settings.preset] ?? PRESET_ROUNDS[DEFAULT_PRESET];
}

/** Critique lenses for the preset plus any configured focus. */
export function lensesFor(settings: GauntletSettings): string[] {
  const base = [...(PRESET_LENSES[settings.preset] ?? PRESET_LENSES[DEFAULT_PRESET])];
  for (const extra of settings.focus) {
    if (!base.includes(extra)) base.push(extra);
  }
  return base;
}

function intFromEnv(raw: string | undefined, fallback: number): number {
  const n = Number.parseInt(raw ?? "", 10);
  return Number.isFinite(n) && n >= 0 ? n : fallback;
}

function floatFromEnv(raw: string | undefined, fallback: number): number {
  const n = Number.parseFloat(raw ?? "");
  return Number.isFinite(n) && n >= 0 && n <= 1 ? n : fallback;
}

function csv(raw: string | undefined): string[] {
  if (!raw) return [];
  return raw.split(",").map((s) => s.trim()).filter(Boolean);
}

/**
 * Resolve settings from the environment.
 *
 * Same injectable-env + fallback-on-junk shape as watchdog.ts
 * limitsFromEnv, so tests can drive it without touching process.env.
 * GAUNTLET_LOOP is applied after CLK_ROBUSTNESS_GAUNTLET so the short,
 * documented name wins — matching the Python precedence.
 */
export function gauntletSettings(env: NodeJS.ProcessEnv = process.env): GauntletSettings {
  let enabled = parseBool(env.CLK_ROBUSTNESS_GAUNTLET, DEFAULT_SETTINGS.enabled);
  enabled = parseBool(env.GAUNTLET_LOOP, enabled);

  const rawPreset = (env.CLK_GAUNTLET_PRESET ?? "").trim().toLowerCase();
  const preset: GauntletPreset = isPreset(rawPreset) ? rawPreset : DEFAULT_PRESET;

  const settings: GauntletSettings = {
    enabled,
    preset,
    maxRounds: intFromEnv(env.CLK_GAUNTLET_MAX_ROUNDS, DEFAULT_SETTINGS.maxRounds),
    maxDispatches: intFromEnv(
      env.CLK_GAUNTLET_MAX_DISPATCHES,
      DEFAULT_SETTINGS.maxDispatches,
    ),
    answerKey: parseBool(env.CLK_GAUNTLET_ANSWER_KEY, DEFAULT_SETTINGS.answerKey),
    finalVerification: parseBool(
      env.CLK_GAUNTLET_FINAL_VERIFICATION,
      DEFAULT_SETTINGS.finalVerification,
    ),
    acceptThreshold: floatFromEnv(
      env.CLK_GAUNTLET_ACCEPT_THRESHOLD,
      DEFAULT_SETTINGS.acceptThreshold,
    ),
    focus: csv(env.CLK_GAUNTLET_FOCUS),
  };

  // A /clk-gauntlet override set this session beats the environment.
  return applyOverride(settings);
}

// ---------------------------------------------------------------------------
// Runtime override (/clk-gauntlet)
// ---------------------------------------------------------------------------

interface Override {
  enabled?: boolean;
  preset?: GauntletPreset;
}

// Module-scope singleton, same pattern as abort.ts. Tests must call
// resetOverride() in beforeEach.
let override: Override = {};

/** Set by the /clk-gauntlet slash command. Wins over the environment. */
export function setOverride(next: Override): void {
  override = { ...override, ...next };
}

/** Clear the runtime override (tests, and /clk-gauntlet reset). */
export function resetOverride(): void {
  override = {};
}

/** The override currently in force, for status reporting. */
export function currentOverride(): Override {
  return { ...override };
}

function applyOverride(settings: GauntletSettings): GauntletSettings {
  const out = { ...settings };
  if (override.enabled !== undefined) out.enabled = override.enabled;
  if (override.preset !== undefined) {
    out.preset = override.preset;
    // Naming a preset implies wanting the loop on; asking for `rigorous`
    // while it is off is never what someone means.
    if (override.enabled === undefined) out.enabled = true;
  }
  return out;
}

// ---------------------------------------------------------------------------
// Session dispatch budget
// ---------------------------------------------------------------------------

/**
 * Session-wide cap on gauntlet spawns.
 *
 * The round cap bounds a *single* dispatch; this bounds the whole session.
 * Without it a long run could spend an unbounded number of critique spawns,
 * because the round cap resets on every dispatch.
 *
 * Module-scope like the override singleton — tests must call resetBudget()
 * in beforeEach.
 */
let budgetUsed = 0;
// Exhaustion is surfaced once per session, not on every subsequent call: a
// long run would otherwise repeat the same notice indefinitely.
let budgetReported = false;

/** Charge one spawn against the session budget. False when it is spent. */
export function spendBudget(limit: number): boolean {
  if (limit > 0 && budgetUsed >= limit) return false;
  budgetUsed += 1;
  return true;
}

/** True when the session budget is fully spent. */
export function budgetExhausted(limit: number): boolean {
  return limit > 0 && budgetUsed >= limit;
}

/** Spawns charged to the session budget so far. */
export function budgetSpent(): number {
  return budgetUsed;
}

/** True the first time exhaustion is worth surfacing, false after. */
export function claimBudgetReport(): boolean {
  if (budgetReported) return false;
  budgetReported = true;
  return true;
}

/** Reset the session budget (tests, and a fresh /clk run). */
export function resetBudget(): void {
  budgetUsed = 0;
  budgetReported = false;
}

/**
 * Parse a /clk-gauntlet argument into an override.
 * Returns null when the token is not recognized.
 */
export function parseGauntletCommand(arg: string): Override | null {
  const token = (arg ?? "").trim().toLowerCase();
  if (!token) return null;
  if (TRUE_TOKENS.has(token) || token === "enable") return { enabled: true };
  if (FALSE_TOKENS.has(token) || token === "disable") return { enabled: false };
  if (isPreset(token)) return { preset: token, enabled: true };
  return null;
}

// ---------------------------------------------------------------------------
// Parsing
// ---------------------------------------------------------------------------

export interface AnswerKeyCheck {
  id: string;
  condition: string;
}

const ANSWER_KEY_RE =
  /^[ \t]*ANSWER_KEY:[ \t]*\r?\n([\s\S]*?)^[ \t]*END_ANSWER_KEY[ \t]*$/im;
const CHECK_LINE_RE = /^[ \t]*(?:[-*]\s*)?([A-Za-z][A-Za-z0-9_.-]*)\s*[:|]\s*(.+?)\s*$/;
const VERDICT_RE = /^\s*VERDICT\s*:\s*(accept|revise|reject)\b/im;
const SCORE_RE = /^\s*SCORE\s*:\s*([0-9]*\.?[0-9]+)/im;
const MATERIAL_RE = /^\s*MATERIAL_DEFECTS\s*:\s*(\d+)\b/im;

/**
 * Extract ANSWER_KEY: / END_ANSWER_KEY checks from a response.
 * Tolerant: an unparseable line is skipped rather than failing the whole
 * key, and a missing block yields [] so the caller can decide whether to
 * spend a spawn deriving one.
 */
export function parseAnswerKey(text: string): AnswerKeyCheck[] {
  if (!text || !text.toUpperCase().includes("ANSWER_KEY")) return [];
  const match = ANSWER_KEY_RE.exec(text);
  if (!match) return [];
  const checks: AnswerKeyCheck[] = [];
  const seen = new Set<string>();
  for (const raw of (match[1] ?? "").split(/\r?\n/)) {
    const line = raw.trim();
    if (!line || line.startsWith("#")) continue;
    const hit = CHECK_LINE_RE.exec(line);
    if (!hit) continue;
    const id = (hit[1] ?? "").trim();
    const condition = (hit[2] ?? "").trim();
    if (!condition || seen.has(id.toLowerCase())) continue;
    seen.add(id.toLowerCase());
    checks.push({ id, condition });
  }
  return checks;
}

export function renderAnswerKey(checks: readonly AnswerKeyCheck[]): string {
  if (checks.length === 0) return "(no acceptance checks derived)";
  return checks.map((c) => `- ${c.id}: ${c.condition}`).join("\n");
}

export interface Critique {
  verdict: "accept" | "revise" | "reject";
  score: number;
  materialDefects: number;
  feedback: string;
}

/** True when the critique found nothing worth another round. */
export function converged(critique: Critique): boolean {
  if (critique.verdict === "accept") return true;
  return critique.materialDefects === 0 && critique.verdict !== "reject";
}

/**
 * Parse VERDICT / SCORE / MATERIAL_DEFECTS from a critic's output.
 * A missing score defaults to 1.0 on accept and 0.4 on revise, matching
 * the Python critic-judge loop so critics learn one output contract.
 */
export function parseCritique(text: string, acceptThreshold = 0.8): Critique {
  const body = text ?? "";
  const verdictHit = VERDICT_RE.exec(body);
  let verdict = (verdictHit?.[1]?.toLowerCase() ?? "revise") as Critique["verdict"];

  const scoreHit = SCORE_RE.exec(body);
  let score = scoreHit ? Number.parseFloat(scoreHit[1] ?? "") : NaN;
  if (!Number.isFinite(score)) score = verdict === "accept" ? 1.0 : 0.4;
  score = Math.max(0, Math.min(1, score));

  const materialHit = MATERIAL_RE.exec(body);
  let material = materialHit ? Number.parseInt(materialHit[1] ?? "", 10) : NaN;
  if (!Number.isFinite(material)) {
    // No explicit count: infer from the verdict, and let a high-scoring
    // "revise" still count as one so the feedback reaches the worker.
    material = verdict === "accept" ? 0 : 1;
  }

  if (verdict === "accept" && score < acceptThreshold) {
    // An "accept" the critic scored below the bar is not an accept.
    verdict = "revise";
    material = Math.max(material, 1);
  }

  return { verdict, score, materialDefects: material, feedback: body.trim() };
}

// ---------------------------------------------------------------------------
// Task builders
// ---------------------------------------------------------------------------

const MAX_QUOTED_CHARS = 4000;

function truncate(text: string, limit = MAX_QUOTED_CHARS): string {
  const body = (text ?? "").trim();
  if (body.length <= limit) return body;
  return body.slice(0, limit) + "\n\n[... truncated for the critique prompt ...]";
}

export function buildKeyTask(task: string, settings: GauntletSettings): string {
  const cap = Math.min(3 + roundsFor(settings), 10);
  return (
    "GAUNTLET :: acceptance answer key.\n\n" +
    "Before any work is judged, write the checkable acceptance criteria for " +
    "the task below. Derive them from the task itself and from the project's " +
    "stated requirements — do not invent a standard of your own, and do not " +
    "weaken, reinterpret, or drop any constraint the task states.\n\n" +
    "Emit exactly one block, nothing else:\n\n" +
    "ANSWER_KEY:\n" +
    "- <check_id>: <unambiguous pass condition, objectively decidable>\n" +
    "- <check_id>: <...>\n" +
    "END_ANSWER_KEY\n\n" +
    "Rules:\n" +
    "- Prefer binary, verifiable conditions over matters of taste.\n" +
    "- Weight correctness above style.\n" +
    "- Cover every explicit constraint and exclusion in the task.\n" +
    `- Aim for ${cap} checks or fewer; each must earn its place.\n` +
    "- If a decision is genuinely unresolved and would materially change the " +
    "result, add a check that names it rather than assuming an answer.\n\n" +
    `TASK:\n${task}`
  );
}

export function buildCritiqueTask(
  task: string,
  candidate: string,
  checks: readonly AnswerKeyCheck[],
  settings: GauntletSettings,
  round: number,
): string {
  return (
    `GAUNTLET :: adversarial critique, round ${round}/${roundsFor(settings)}.\n\n` +
    "Attack this work. Do not defend it, do not summarize it, and do not " +
    "praise it. Your job is to find what is wrong before anyone else does.\n\n" +
    "Judge it against the acceptance answer key below — that key, plus the " +
    "original task, is the source of truth. Do not substitute your own notion " +
    "of quality for it, and do not move the goalposts to make a weak " +
    "candidate pass.\n\n" +
    `ACCEPTANCE ANSWER KEY:\n${renderAnswerKey(checks)}\n\n` +
    `ORIGINAL TASK:\n${truncate(task)}\n\n` +
    `CANDIDATE:\n${truncate(candidate)}\n\n` +
    `Look through these lenses: ${lensesFor(settings).join(", ")}.\n\n` +
    "Check specifically for: failed or omitted acceptance checks; unsupported " +
    "claims; factual errors and invalid reasoning; hidden or conflicting " +
    "assumptions; ambiguity and contradictions; counterexamples and edge " +
    "cases; implementation gaps; unnecessary complexity; integration failures " +
    "between parts that each look fine alone; and overconfidence or false " +
    "claims of verification.\n\n" +
    "Classify every issue as material or non-material. A **material defect** " +
    "could change correctness, usefulness, compliance, interpretation, " +
    "feasibility, safety, or a reader's decision. Minor style preferences, " +
    "synonym choices, and cosmetic reordering are **not** material. Finding " +
    "nothing material is a valid outcome — say so rather than inventing a " +
    "complaint.\n\n" +
    "For each material defect, name the answer-key check it breaks.\n\n" +
    "End your response with exactly these three lines:\n" +
    "MATERIAL_DEFECTS: <integer>\n" +
    "VERDICT: accept   # or: revise\n" +
    "SCORE: <0..1>"
  );
}

export function buildReviseTask(
  task: string,
  critique: Critique,
  checks: readonly AnswerKeyCheck[],
  settings: GauntletSettings,
  round: number,
): string {
  return (
    `GAUNTLET :: revision, round ${round}/${roundsFor(settings)}.\n\n` +
    `An adversarial critic scored your previous response ` +
    `${critique.score.toFixed(2)}/1.0 and found ${critique.materialDefects} ` +
    "material defect(s):\n\n" +
    `${truncate(critique.feedback)}\n\n` +
    "Fix every material defect. Integrate the corrections into the work " +
    "itself — do not append caveats, disclaimers, or a changelog explaining " +
    "what you fixed. Keep what already works; rewrite only what was flagged. " +
    "Do not weaken the task's requirements to make the critique go away.\n\n" +
    "Re-check the ORIGINAL TASK below, not just the critique — a revision " +
    "that satisfies the critic while drifting from the original request has " +
    "failed.\n\n" +
    `ACCEPTANCE ANSWER KEY:\n${renderAnswerKey(checks)}\n\n` +
    `ORIGINAL TASK:\n${task}`
  );
}

export function buildVerifyTask(
  task: string,
  candidate: string,
  checks: readonly AnswerKeyCheck[],
): string {
  return (
    "GAUNTLET :: final verification.\n\n" +
    "This is the last gate before the work is accepted. Re-evaluate the " +
    "candidate against all of the following:\n\n" +
    "1. The original request, in full.\n" +
    "2. Every explicit constraint and exclusion it states.\n" +
    "3. Every check in the acceptance answer key.\n" +
    "4. Integration and regression: do the parts work together, and did the " +
    "revisions break anything that previously worked?\n" +
    "5. The evidence actually available to you.\n\n" +
    "Prefer deterministic evidence over opinion: run or read tests, inspect " +
    "the artifacts on disk, check the numbers. Do **not** claim you tested, " +
    "researched, or externally verified something you did not — an honest " +
    "'unverified' is worth more than a false 'verified'.\n\n" +
    `ACCEPTANCE ANSWER KEY:\n${renderAnswerKey(checks)}\n\n` +
    `ORIGINAL TASK:\n${truncate(task)}\n\n` +
    `CANDIDATE:\n${truncate(candidate)}\n\n` +
    "End your response with exactly these three lines:\n" +
    "MATERIAL_DEFECTS: <integer>\n" +
    "VERDICT: accept   # or: revise\n" +
    "SCORE: <0..1>"
  );
}

export function buildRepairTask(
  task: string,
  critique: Critique,
  checks: readonly AnswerKeyCheck[],
): string {
  return (
    "GAUNTLET :: final repair (one pass only).\n\n" +
    "Final verification found a remaining material defect. This is the only " +
    "repair round left — fix the defect and nothing else. Do not start new " +
    "work, do not refactor beyond the fix, and do not widen the scope.\n\n" +
    `${truncate(critique.feedback)}\n\n` +
    `ACCEPTANCE ANSWER KEY:\n${renderAnswerKey(checks)}\n\n` +
    `ORIGINAL TASK:\n${task}`
  );
}

// ---------------------------------------------------------------------------
// The loop
// ---------------------------------------------------------------------------

export interface GauntletOptions {
  /** The worker role the candidate came from; revisions go back to it. */
  agent: string;
  /** The original task, verbatim. */
  task: string;
  /** Candidate 0 — the output the normal dispatch path produced. */
  candidate: string;
  cwd: string;
  preferredModel?: string;
  signal?: AbortSignal;
  onUpdate?: (text: string) => void;
  /** Role used for critique / verification. Defaults to "critic". */
  critic?: string;
  /** Overrides the env-resolved settings (tests, per-call tuning). */
  settings?: GauntletSettings;
  /** Injectable spawn — tests pass a stub, same seam as consensus.ts. */
  spawn?: SpawnFn;
}

export interface GauntletRound {
  round: number;
  verdict: Critique["verdict"];
  score: number;
  materialDefects: number;
}

export interface GauntletResult {
  /** The best output the gauntlet arrived at. */
  output: string;
  /** Acceptance criteria the work was judged against. */
  checks: AnswerKeyCheck[];
  rounds: GauntletRound[];
  /** True when a critique found nothing material before the cap. */
  converged: boolean;
  /** Verification verdict, when the final pass ran. */
  verified?: Critique["verdict"];
  /** Total extra spawns the gauntlet cost. */
  spawns: number;
  /** Set when the loop was skipped (disabled, or nothing to critique). */
  skipped?: string;
}

/**
 * Put a candidate through the gauntlet and return the best output.
 *
 * Never throws and never loses the candidate: any failure inside the loop
 * returns the output that came in, so the gauntlet can only improve a
 * dispatch or leave it alone.
 */
export async function runGauntlet(opts: GauntletOptions): Promise<GauntletResult> {
  const settings = opts.settings ?? gauntletSettings();
  const spawn = opts.spawn ?? defaultSpawnSubagent;
  const critic = opts.critic ?? "critic";
  const cap = roundsFor(settings);

  const base: GauntletResult = {
    output: opts.candidate,
    checks: [],
    rounds: [],
    converged: false,
    spawns: 0,
  };

  if (!settings.enabled) return { ...base, skipped: "disabled" };
  if (!opts.candidate.trim()) return { ...base, skipped: "empty candidate" };
  if (budgetExhausted(settings.maxDispatches)) {
    return { ...base, skipped: "dispatch budget exhausted" };
  }

  // The gauntlet's own spawns must not be gauntleted again. The depth cap
  // is prompt-level, matching subagent.ts's no-grandchildren notice.
  const dispatch = async (agent: string, task: string): Promise<string> => {
    if (opts.signal?.aborted) return "";
    if (!spendBudget(settings.maxDispatches)) {
      // Session budget spent: stop wrapping rather than keep spending. The
      // caller falls back to the best candidate it already has.
      base.skipped = "dispatch budget exhausted";
      return "";
    }
    try {
      const { output } = await spawn({
        agent,
        task,
        cwd: opts.cwd,
        ...(opts.preferredModel !== undefined ? { preferredModel: opts.preferredModel } : {}),
        ...(opts.signal !== undefined ? { signal: opts.signal } : {}),
        ...(opts.onUpdate !== undefined ? { onUpdate: opts.onUpdate } : {}),
      });
      base.spawns += 1;
      return output ?? "";
    } catch {
      // A failed critique is not a failed dispatch — fall back to the
      // candidate rather than losing the work.
      return "";
    }
  };

  // -- stage 1: the answer key ---------------------------------------------
  let checks = parseAnswerKey(opts.candidate);
  if (checks.length === 0 && settings.answerKey) {
    checks = parseAnswerKey(await dispatch(critic, buildKeyTask(opts.task, settings)));
  }
  base.checks = checks;

  // -- stages 2-4: critique / revise ---------------------------------------
  let current = opts.candidate;
  for (let round = 1; round <= cap; round += 1) {
    if (opts.signal?.aborted) break;
    const critiqueText = await dispatch(
      critic,
      buildCritiqueTask(opts.task, current, checks, settings, round),
    );
    if (!critiqueText.trim()) break; // no usable critique — do not revise blindly

    const critique = parseCritique(critiqueText, settings.acceptThreshold);
    base.rounds.push({
      round,
      verdict: critique.verdict,
      score: critique.score,
      materialDefects: critique.materialDefects,
    });
    opts.onUpdate?.(
      `gauntlet round ${round}/${cap}: ${critique.verdict} ` +
        `score=${critique.score.toFixed(2)} material=${critique.materialDefects}`,
    );

    if (converged(critique)) {
      base.converged = true;
      break;
    }

    const revised = await dispatch(
      opts.agent,
      buildReviseTask(opts.task, critique, checks, settings, round),
    );
    if (!revised.trim()) break; // keep the last good candidate
    current = revised;
  }

  // -- stage 5: final verification + one bounded repair ---------------------
  if (settings.finalVerification && !opts.signal?.aborted) {
    const verifyText = await dispatch(critic, buildVerifyTask(opts.task, current, checks));
    if (verifyText.trim()) {
      const verdict = parseCritique(verifyText, settings.acceptThreshold);
      base.verified = verdict.verdict;
      if (!converged(verdict)) {
        const repaired = await dispatch(
          opts.agent,
          buildRepairTask(opts.task, verdict, checks),
        );
        if (repaired.trim()) current = repaired;
      }
    }
  }

  base.output = current;
  return base;
}

/** One-line summary for tool output. Mirrors quality.ts summarise(). */
export function summariseGauntlet(result: GauntletResult): string {
  if (result.skipped) return `gauntlet skipped (${result.skipped})`;
  const last = result.rounds[result.rounds.length - 1];
  const parts = [
    `${result.rounds.length} critique round(s)`,
    `${result.checks.length} acceptance check(s)`,
    `+${result.spawns} spawn(s)`,
  ];
  if (last) parts.push(`last verdict=${last.verdict} score=${last.score.toFixed(2)}`);
  if (result.verified) parts.push(`verified=${result.verified}`);
  if (result.converged) parts.push("converged");
  return `gauntlet: ${parts.join(", ")}`;
}
