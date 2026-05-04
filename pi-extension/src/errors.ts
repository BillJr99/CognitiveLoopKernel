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
  // OpenRouter returns 404 with these messages when routing is temporarily unavailable
  /no\s+(?:available\s+)?endpoints?\s+(?:available|found)/i,
  /no\s+provider\s+(?:has\s+taken|available)/i,
  /quota.?exceeded/i,
  /resource.?exhausted/i,
  /capacity/i,
  /try again/i,
  /throttl/i,
  /temporarily.*rate/i,
  /rate.*upstream/i,
  /upstream.*rate/i,
  /please\s+retry.*rate/i,
  /provider returned.*rate.?limit/i,
];

const MODEL_ERROR_PATTERNS: RegExp[] = [
  /model.*(not found|unavailable|does not exist|is not available)/i,
  /no such model/i,
  /invalid.?model/i,
  /endpoint.*(not found|unavailable)/i,
  /the model.*cannot be used/i,
  /free.*tier.*not.*support/i,
  /no endpoint\s+for\b/i,
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

  // Some HTTP clients surface the status code as a property rather than
  // embedding it in the message text; check both routes.  Normalize to a
  // number so string values like "429" or "404" compare correctly.
  const rawStatus =
    (err as { status?: unknown }).status ??
    (err as { statusCode?: unknown }).statusCode ??
    (err as { response?: { status?: unknown } }).response?.status;
  const httpStatus = rawStatus !== undefined && rawStatus !== null ? Number(rawStatus) : NaN;

  if (httpStatus === 429 || RATE_LIMIT_PATTERNS.some((p) => p.test(msg))) return "rate_limit";
  if (httpStatus === 404 || MODEL_ERROR_PATTERNS.some((p) => p.test(msg))) return "model_error";
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
  /** Maximum attempts for non-rate-limit retryable errors (network blips etc.). Rate-limit
   *  errors are retried indefinitely until the abort signal fires. */
  maxAttempts?: number;
  baseDelayMs?: number;
  /** Hard ceiling on any single inter-retry delay. Prevents the exponential from
   *  growing beyond a practical bound; defaults to 5 minutes. */
  maxDelayMs?: number;
  signal?: AbortSignal;
  onRetry?: (err: unknown, attempt: number, delayMs: number) => void;
}

/**
 * Retry `fn` with exponential backoff whenever a retryable error is thrown.
 *
 * Rate-limit errors (429 / upstream throttling) are retried indefinitely —
 * the delay grows exponentially up to `maxDelayMs` (default 5 min) and then
 * stays there until the call succeeds or the abort signal fires.
 *
 * All other retryable errors (network blips) give up after `maxAttempts`
 * (default 4).  Non-retryable errors propagate immediately.
 */
export async function withRetry<T>(fn: () => Promise<T>, opts: RetryOptions = {}): Promise<T> {
  const {
    maxAttempts = 4,
    baseDelayMs = 2000,
    maxDelayMs = 5 * 60 * 1000,
    signal,
    onRetry,
  } = opts;

  // Track rate-limit and network retries with separate counters so that
  // many rate-limit retries don't silently exhaust the network-blip budget.
  let rateLimitAttempt = 0;
  let networkAttempt = 0;

  for (;;) {
    if (signal?.aborted) throw new Error("Aborted");
    try {
      return await fn();
    } catch (err) {
      if (!isRetryable(err)) throw err;

      const isRateLimit = classifyError(err) === "rate_limit";

      // Non-rate-limit retryable errors (network blips) honour maxAttempts.
      if (!isRateLimit && networkAttempt >= maxAttempts - 1) throw err;

      const attempt = isRateLimit ? rateLimitAttempt : networkAttempt;
      const delayMs = Math.min(baseDelayMs * 2 ** attempt, maxDelayMs);
      if (isRateLimit) rateLimitAttempt++; else networkAttempt++;
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
