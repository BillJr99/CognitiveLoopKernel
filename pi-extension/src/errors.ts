/**
 * Utilities for classifying and recovering from model-provider errors.
 *
 * Five failure categories we care about:
 *   rate_limit   — provider is throttling; backoff and retry.
 *   model_error  — endpoint doesn't exist or is unavailable; notify and skip.
 *   redaction    — privacy settings stripped a required value; retry without
 *                  the sensitive content.
 *   max_turns    — child pi process hit its turn cap; re-dispatch immediately.
 *   network      — transient connectivity; backoff and retry.
 *   other        — anything else; propagate but don't abort the run.
 */

const RATE_LIMIT_PATTERNS: RegExp[] = [
  /rate.?limit/i,
  /too many requests/i,
  /\b429\b/,
  /quota.?exceeded/i,
  /resource.?exhausted/i,
  /capacity/i,
  /try again/i,
  /throttl/i,
];

const MODEL_ERROR_PATTERNS: RegExp[] = [
  /model.*(not found|unavailable|does not exist|is not available)/i,
  /no such model/i,
  /invalid.?model/i,
  /\b404\b/,
  /endpoint.*(not found|unavailable)/i,
  /the model.*cannot be used/i,
  /free.*tier.*not.*support/i,
  /no endpoint/i,
];

const REDACTION_PATTERNS: RegExp[] = [
  /\[REDACTED\]/i,
  /redact/i,
  /privacy.?filter/i,
  /sensitive.?content/i,
  /content.?filter/i,
  /blocked.*by.*policy/i,
  /privacy.?setting/i,
];

const MAX_TURNS_PATTERNS: RegExp[] = [
  /max(?:imum)?\s*turns?\s*(?:reached|exceeded|exhausted|hit|limit)/i,
  /turn\s*(?:limit|cap|max)\s*(?:reached|exceeded|hit)/i,
  /reached\s*(?:the\s*)?max(?:imum)?\s*(?:number\s*of\s*)?turns?/i,
  /turn\s*(?:count|budget)\s*(?:exceeded|exhausted)/i,
  /agent.*stopped.*turns?/i,
  /no\s*more\s*turns?/i,
];

const NETWORK_PATTERNS: RegExp[] = [
  /ECONNRESET/,
  /ETIMEDOUT/,
  /ENOTFOUND/,
  /ECONNREFUSED/,
  /network.*error/i,
  /socket.*hang/i,
  /failed to fetch/i,
];

export type ErrorClass = "rate_limit" | "model_error" | "redaction" | "max_turns" | "network" | "other";

export function classifyError(err: unknown): ErrorClass {
  const msg =
    err instanceof Error
      ? `${err.message} ${(err as NodeJS.ErrnoException).code ?? ""}`
      : String(err);

  if (RATE_LIMIT_PATTERNS.some((p) => p.test(msg))) return "rate_limit";
  if (MODEL_ERROR_PATTERNS.some((p) => p.test(msg))) return "model_error";
  if (REDACTION_PATTERNS.some((p) => p.test(msg))) return "redaction";
  if (MAX_TURNS_PATTERNS.some((p) => p.test(msg))) return "max_turns";
  if (NETWORK_PATTERNS.some((p) => p.test(msg))) return "network";
  return "other";
}

export function isRetryable(err: unknown): boolean {
  const cls = classifyError(err);
  return cls === "rate_limit" || cls === "network";
}

/**
 * Returns true if a string value looks like it was redacted by a privacy filter.
 * Used to guard tool parameters before we try to use them.
 */
export function looksRedacted(value: string): boolean {
  return REDACTION_PATTERNS.some((p) => p.test(value)) || value.trim() === "";
}

/**
 * Returns true if text from a subagent tool result indicates the child pi
 * process hit its max-turns limit.  Used to decide whether to re-dispatch.
 */
export function isMaxTurnsResult(text: string): boolean {
  return MAX_TURNS_PATTERNS.some((p) => p.test(text));
}

export interface RetryOptions {
  maxAttempts?: number;
  baseDelayMs?: number;
  signal?: AbortSignal;
  onRetry?: (err: unknown, attempt: number, delayMs: number) => void;
}

/**
 * Retry `fn` with exponential backoff (2 s → 4 s → 8 s → 16 s by default)
 * whenever a retryable error is thrown. Non-retryable errors propagate
 * immediately.
 */
export async function withRetry<T>(fn: () => Promise<T>, opts: RetryOptions = {}): Promise<T> {
  const { maxAttempts = 4, baseDelayMs = 2000, signal, onRetry } = opts;
  let lastErr: unknown;

  for (let attempt = 0; attempt < maxAttempts; attempt++) {
    if (signal?.aborted) throw new Error("Aborted");
    try {
      return await fn();
    } catch (err) {
      lastErr = err;
      if (!isRetryable(err) || attempt === maxAttempts - 1) throw err;

      const delayMs = baseDelayMs * 2 ** attempt;
      onRetry?.(err, attempt + 1, delayMs);

      await new Promise<void>((resolve, reject) => {
        const timer = setTimeout(resolve, delayMs);
        if (signal) {
          signal.addEventListener(
            "abort",
            () => {
              clearTimeout(timer);
              reject(new Error("Aborted during retry delay"));
            },
            { once: true },
          );
        }
      });
    }
  }
  throw lastErr;
}

/**
 * Human-readable hint for each error class, returned to the chief as tool
 * result text so it knows how to react.
 */
export function recoveryHint(cls: ErrorClass): string {
  switch (cls) {
    case "rate_limit":
      return (
        "The model provider is rate-limiting requests. " +
        "Wait at least 30 seconds, then retry the same subagent call. " +
        "If it keeps failing, try a different (or smaller) model."
      );
    case "model_error":
      return (
        "The requested model endpoint does not exist or cannot be used right now. " +
        "Fall back to a built-in Pi agent (scout / worker / researcher / oracle) " +
        "or omit the preferredModel field and let Pi choose."
      );
    case "redaction":
      return (
        "A privacy filter redacted part of the request. " +
        "Retry without including the sensitive field, or write the sensitive " +
        "data to a file first and reference the file path instead."
      );
    case "max_turns":
      return (
        "The subagent exhausted its turn budget before finishing. " +
        "Re-dispatch the same agent with the identical task immediately — " +
        "do not modify the task or ask for confirmation. " +
        "If it exhausts turns again, split the task into two smaller subtasks " +
        "and dispatch them sequentially."
      );
    case "network":
      return "A transient network error occurred. Retry after a short wait.";
    case "other":
      return "An unexpected error occurred. Inspect the message and decide whether to retry or skip this step.";
  }
}
