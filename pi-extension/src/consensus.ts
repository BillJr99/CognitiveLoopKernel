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

/**
 * The signature of the function that actually spawns a subagent.
 * Defaults to the real tmux-based implementation in subagent.ts;
 * the tests inject a synchronous in-memory stub so they can run
 * without tmux / pi available.
 */
export type SpawnFn = (opts: SpawnOptions) => Promise<{ output: string; sessionId: string }>;

export interface QualityDispatchOptions extends SpawnOptions {
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
      return { output, sessionId, quality: lastQuality, attempts: attempt };
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

export interface ConsensusOptions extends Omit<SpawnOptions, "onUpdate"> {
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
  return { best: winner, all: collected, reason };
}
