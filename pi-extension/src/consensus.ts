/**
 * Stochastic consensus + quality re-dispatch for clk_subagent.
 *
 * Two related primitives in one module:
 *
 *   * dispatchWithQuality — wraps a single spawnSubagent call with the
 *     quality re-dispatch loop (port of agent.py
 *     _dispatch_with_quality_loop). Scores the output via quality.ts;
 *     when the verdict is recoverable, re-runs with a repair preamble
 *     up to `maxRetries` extra attempts.
 *
 *   * runConsensus — fan-out N parallel tmux subagent samples for the
 *     same task, score each via quality.ts, return all samples plus the
 *     best (highest score, ok=true preferred). Port of
 *     agent.py _dispatch_auto_consensus, minus the chief-coalescing
 *     pass (the caller can choose to feed all samples back to the chief
 *     if it wants a synthesised answer; the typical case is "pick the
 *     winner and continue").
 *
 * Both helpers are exposed as new clk tools in tools.ts so the chief
 * can dispatch through them instead of raw clk_subagent. The chief
 * prompt nudges it that way for any non-trivial work.
 */

import { spawnSubagent as defaultSpawnSubagent, type SpawnOptions } from "./subagent.js";
import {
  scoreResponse,
  repairHint,
  isRecoverable,
  summarise,
  type ResponseQuality,
  type ScoreOpts,
} from "./quality.js";
import {
  gauntletSettings,
  runGauntlet,
  type GauntletResult,
  type GauntletSettings,
} from "./gauntlet.js";

/**
 * The signature of the function that actually spawns a subagent.
 * Defaults to the real tmux-based implementation in subagent.ts;
 * the tests inject a synchronous in-memory stub so they can run
 * without tmux / pi available.
 */
export type SpawnFn = (opts: SpawnOptions) => Promise<{ output: string; sessionId: string }>;

/** Cap on the distilled result returned from a delegated subtask. */
export const MAX_DELEGATE_RESULT_CHARS = 8000;

/**
 * Options every dispatch path accepts for the gauntlet (layer 12).
 *
 * Omit `gauntlet` to use the environment-resolved settings (on by default);
 * pass `false` to skip the loop for this one call, or a settings object to
 * override it. Passing `false` is what the gauntlet's own internal spawns
 * do, so the loop can never re-enter itself.
 */
export interface GauntletAware {
  gauntlet?: GauntletSettings | false;
  /** Role used for critique / verification. Defaults to "critic". */
  critic?: string;
}

/**
 * Run the gauntlet over one candidate, or return it unchanged.
 *
 * Never throws: a failure inside the loop leaves the candidate as-is, so
 * wrapping a dispatch can only improve it.
 */
async function applyGauntlet(
  opts: GauntletAware & {
    agent: string;
    task: string;
    cwd: string;
    preferredModel?: string;
    signal?: AbortSignal;
    onUpdate?: (text: string) => void;
    spawn?: SpawnFn;
  },
  candidate: string,
): Promise<{ output: string; gauntlet?: GauntletResult }> {
  if (opts.gauntlet === false) return { output: candidate };
  const settings = opts.gauntlet ?? gauntletSettings();
  if (!settings.enabled) return { output: candidate };
  try {
    const result = await runGauntlet({
      agent: opts.agent,
      task: opts.task,
      candidate,
      cwd: opts.cwd,
      settings,
      ...(opts.critic !== undefined ? { critic: opts.critic } : {}),
      ...(opts.preferredModel !== undefined ? { preferredModel: opts.preferredModel } : {}),
      ...(opts.signal !== undefined ? { signal: opts.signal } : {}),
      ...(opts.onUpdate !== undefined ? { onUpdate: opts.onUpdate } : {}),
      ...(opts.spawn !== undefined ? { spawn: opts.spawn } : {}),
    });
    return { output: result.output, gauntlet: result };
  } catch {
    return { output: candidate };
  }
}

export interface DelegateOptions extends GauntletAware {
  /** Target role label for the child. */
  agent: string;
  /** The bounded subtask. */
  task: string;
  /** Optional one-line context handed to the child. */
  context?: string;
  preferredModel?: string;
  cwd: string;
  signal?: AbortSignal;
  onUpdate?: (text: string) => void;
  /** Injectable spawn (tests pass a stub); defaults to the tmux impl. */
  spawn?: SpawnFn;
  /** Override the result cap (mainly for tests). */
  maxResultChars?: number;
}

export interface DelegateResult {
  output: string;
  sessionId: string;
  /** Present when the gauntlet ran over the child's work. */
  gauntlet?: GauntletResult;
}

/**
 * Hand a bounded subtask to a context-isolated child agent and return
 * only its distilled result. The child runs as a fresh pi session
 * (spawnSubagent) — it does NOT inherit the caller's conversation or
 * blackboard — but MAY do real work (write/commit files) in the repo.
 * Distillation is by instruction: the child is told to reply with only a
 * concise summary, mirroring the Python harness's DELEGATE child
 * objective. Depth is capped structurally — spawnSubagent's preamble
 * forbids the child from spawning further subagents or calling clk_*.
 */
export async function runDelegate(opts: DelegateOptions): Promise<DelegateResult> {
  const spawn = opts.spawn ?? defaultSpawnSubagent;
  const preamble =
    "Delegated, context-isolated subtask. You do NOT share the caller's " +
    "conversation or blackboard — work only from the task below. You MAY do " +
    "real work (read/write/edit files, run bash, commit with git). When " +
    "finished, reply with ONLY a concise, self-contained summary of the " +
    "result the caller needs — that summary is all that is returned.";
  const composed =
    preamble +
    (opts.context ? `\n\nContext:\n${opts.context}` : "") +
    `\n\nTask:\n${opts.task}`;
  const { output, sessionId } = await spawn({
    agent: opts.agent,
    task: composed,
    preferredModel: opts.preferredModel,
    cwd: opts.cwd,
    signal: opts.signal,
    onUpdate: opts.onUpdate,
  });
  // Gauntlet the child's work before distilling: the caller only ever sees
  // the summary, so a defect not caught here is a defect that ships.
  const gauntleted = await applyGauntlet(
    { ...opts, task: composed, spawn },
    output,
  );

  const cap = opts.maxResultChars ?? MAX_DELEGATE_RESULT_CHARS;
  let distilled = gauntleted.output || "(delegate produced no output)";
  if (distilled.length > cap) {
    distilled = distilled.slice(0, cap) + `\n\n[result truncated at ${cap} chars]`;
  }
  return {
    output: distilled,
    sessionId,
    ...(gauntleted.gauntlet ? { gauntlet: gauntleted.gauntlet } : {}),
  };
}

export interface QualityDispatchOptions extends SpawnOptions, GauntletAware {
  /**
   * Extra spawn attempts after the initial one. Default 1 (so up to
   * two total dispatches per call). Set to 0 to disable the loop.
   */
  maxRetries?: number;
  /** Scoring options forwarded to quality.scoreResponse. */
  scoreOpts?: ScoreOpts;
  /** Called with a short status line on each retry. Optional. */
  onRetry?: (attempt: number, quality: ResponseQuality) => void;
  /**
   * Injectable spawn function — defaults to the real tmux-based
   * spawnSubagent. Tests pass a stub.
   */
  spawn?: SpawnFn;
}

export interface QualityDispatchResult {
  output: string;
  sessionId: string;
  quality: ResponseQuality;
  attempts: number;
  /** Present when the gauntlet ran over the accepted candidate. */
  gauntlet?: GauntletResult;
}

/**
 * Dispatch one subagent, score the output, re-dispatch with a repair
 * preamble on recoverable failures. Returns the *last* run (which is
 * either the first ok run, or the final attempt's run when retries
 * ran out — callers inspect `quality.ok` to decide).
 */
export async function dispatchWithQuality(
  opts: QualityDispatchOptions,
): Promise<QualityDispatchResult> {
  const maxRetries = Math.max(0, opts.maxRetries ?? 1);
  const scoreOpts = opts.scoreOpts ?? {};
  const spawn = opts.spawn ?? defaultSpawnSubagent;
  const baseTask = opts.task;
  let currentTask = baseTask;
  let attempt = 0;
  let lastQuality: ResponseQuality = scoreResponse("");
  let lastOutput = "";
  let lastSessionId = "";
  while (true) {
    attempt += 1;
    const { output, sessionId } = await spawn({
      ...opts,
      task: currentTask,
    });
    lastOutput = output;
    lastSessionId = sessionId;
    lastQuality = scoreResponse(output, scoreOpts);
    if (lastQuality.ok || !isRecoverable(lastQuality) || attempt > maxRetries) {
      // Quality gating is done; put the accepted candidate through the
      // gauntlet before handing it back. Scored again afterwards so the
      // caller's `quality` reflects what it actually receives.
      const g = await applyGauntlet({ ...opts, task: baseTask, spawn }, output);
      const finalOutput = g.output;
      return {
        output: finalOutput,
        sessionId,
        quality: finalOutput === output ? lastQuality : scoreResponse(finalOutput, scoreOpts),
        attempts: attempt,
        ...(g.gauntlet ? { gauntlet: g.gauntlet } : {}),
      };
    }
    opts.onRetry?.(attempt, lastQuality);
    currentTask = repairHint(lastQuality) + "\n\nOriginal task:\n" + baseTask;
  }
  // Unreachable.
  return {
    output: lastOutput,
    sessionId: lastSessionId,
    quality: lastQuality,
    attempts: attempt,
  };
}

export interface ConsensusSample {
  index: number;
  agent: string;
  output: string;
  sessionId: string;
  quality: ResponseQuality;
  /** Set when spawnSubagent threw before producing output. */
  error?: string;
}

export interface ConsensusOptions extends Omit<SpawnOptions, "onUpdate">, GauntletAware {
  /** Number of parallel samples. Clamped to 1..6. Default 3. */
  samples?: number;
  /** Max concurrent in-flight tmux sessions. Clamped to 1..samples. Default min(4, samples). */
  maxParallel?: number;
  scoreOpts?: ScoreOpts;
  /**
   * Called with each sample's progress update. The fan-out wraps the
   * tmux poll messages so the caller can stream them.
   */
  onSample?: (index: number, message: string) => void;
  /** Injectable spawn function — tests pass a stub. */
  spawn?: SpawnFn;
}

export interface ConsensusResult {
  best: ConsensusSample;
  all: ConsensusSample[];
  /** Short human-readable winning rationale. */
  reason: string;
  /** Present when the gauntlet ran over the winning sample. */
  gauntlet?: GauntletResult;
}

function pickBest(samples: ConsensusSample[]): { winner: ConsensusSample; reason: string } {
  if (samples.length === 0) {
    throw new Error("runConsensus: no samples to pick from");
  }
  // Prefer samples that came back with output, then highest quality
  // score, tie-break on shorter output (less filler).
  const sorted = [...samples].sort((a, b) => {
    const aHas = a.error ? 0 : 1;
    const bHas = b.error ? 0 : 1;
    if (aHas !== bHas) return bHas - aHas;
    if (a.quality.score !== b.quality.score) return b.quality.score - a.quality.score;
    return a.output.length - b.output.length;
  });
  const winner = sorted[0]!;
  let reason = `sample #${winner.index} won: ${summarise(winner.quality)}`;
  if (samples.length > 1) {
    const scores = samples.map((s) => `#${s.index}=${s.quality.score.toFixed(2)}`).join(" ");
    reason += ` (all: ${scores})`;
  }
  return { winner, reason };
}

/**
 * Spawn N parallel subagent samples for the same task; score each;
 * return them all plus the winner. Never throws — failed samples carry
 * their error in `sample.error` and contribute a 0-score quality.
 */
export async function runConsensus(opts: ConsensusOptions): Promise<ConsensusResult> {
  const samples = Math.max(1, Math.min(6, Math.floor(opts.samples ?? 3)));
  const maxParallel = Math.max(1, Math.min(samples, Math.floor(opts.maxParallel ?? Math.min(4, samples))));
  const scoreOpts = opts.scoreOpts ?? {};
  const spawn = opts.spawn ?? defaultSpawnSubagent;

  // Simple semaphore-style runner: launch up to `maxParallel` at a time.
  const indices = Array.from({ length: samples }, (_, i) => i + 1);
  const collected: ConsensusSample[] = [];

  const runOne = async (idx: number): Promise<ConsensusSample> => {
    try {
      const { output, sessionId } = await spawn({
        agent: opts.agent,
        task: opts.task,
        preferredModel: opts.preferredModel,
        cwd: opts.cwd,
        signal: opts.signal,
        onUpdate: (text) => opts.onSample?.(idx, text),
      });
      const quality = scoreResponse(output, scoreOpts);
      return { index: idx, agent: opts.agent, output, sessionId, quality };
    } catch (err) {
      return {
        index: idx,
        agent: opts.agent,
        output: "",
        sessionId: "",
        quality: scoreResponse(""),
        error: (err as Error).message,
      };
    }
  };

  // Pool: keep `maxParallel` in flight, drain as they complete.
  let next = 0;
  async function worker(): Promise<void> {
    while (next < indices.length) {
      const myIdx = indices[next++]!;
      const result = await runOne(myIdx);
      collected.push(result);
    }
  }
  const workers = Array.from({ length: maxParallel }, () => worker());
  await Promise.all(workers);

  // Stable order by sample index.
  collected.sort((a, b) => a.index - b.index);
  const { winner, reason } = pickBest(collected);

  // Gauntlet the winner only. Running it on every sample would multiply an
  // already-expensive fan-out by the round cap for no benefit: the losing
  // samples are discarded either way.
  const g = await applyGauntlet({ ...opts, spawn }, winner.output);
  const best: ConsensusSample =
    g.output === winner.output
      ? winner
      : { ...winner, output: g.output, quality: scoreResponse(g.output, scoreOpts) };

  return {
    best,
    all: collected,
    reason,
    ...(g.gauntlet ? { gauntlet: g.gauntlet } : {}),
  };
}
