/**
 * Validation gate — code-enforced shell checks for clk_merge and
 * clk_done, the extension's equivalent of per-stage `validation:`
 * commands in the Python harness's workflow YAML. A failing command
 * blocks the merge / completion instead of relying on the chief to
 * remember it ran the tests.
 */

import { execFile } from "node:child_process";

export interface ValidationResult {
  ok: boolean;
  command: string;
  /** Combined stdout+stderr tail, capped for tool-result readability. */
  output: string;
  exitCode: number | null;
}

const OUTPUT_CAP = 4000;
const DEFAULT_TIMEOUT_MS = 5 * 60 * 1000;

export function runValidation(
  cwd: string,
  command: string,
  opts: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<ValidationResult> {
  return new Promise((resolve) => {
    const child = execFile(
      "/bin/bash",
      ["-c", command],
      {
        cwd,
        timeout: opts.timeoutMs ?? DEFAULT_TIMEOUT_MS,
        maxBuffer: 10 * 1024 * 1024,
        signal: opts.signal,
      },
      (err, stdout, stderr) => {
        const combined = `${stdout ?? ""}${stderr ? `\n${stderr}` : ""}`.trim();
        const output =
          combined.length > OUTPUT_CAP ? `…${combined.slice(-OUTPUT_CAP)}` : combined;
        if (err) {
          const code =
            typeof (err as NodeJS.ErrnoException & { code?: unknown }).code === "number"
              ? ((err as { code: number }).code)
              : child.exitCode;
          resolve({ ok: false, command, output: output || String(err.message), exitCode: code ?? null });
        } else {
          resolve({ ok: true, command, output, exitCode: 0 });
        }
      },
    );
  });
}

/** Run several validation commands in order; stop at the first failure. */
export async function runValidations(
  cwd: string,
  commands: string[],
  opts: { signal?: AbortSignal; timeoutMs?: number } = {},
): Promise<{ ok: boolean; results: ValidationResult[] }> {
  const results: ValidationResult[] = [];
  for (const command of commands) {
    const r = await runValidation(cwd, command, opts);
    results.push(r);
    if (!r.ok) return { ok: false, results };
  }
  return { ok: true, results };
}
