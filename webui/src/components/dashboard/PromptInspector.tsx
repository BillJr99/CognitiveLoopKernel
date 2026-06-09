import { X } from "lucide-react";
import type { ActivityEvent } from "../../api/types";
import { shortTime } from "../../lib/format";

// Modal that surfaces the full payload of an activity event — most
// usefully the prompt (embedded in prompt_sent) and the response text /
// token usage (embedded in agent_response).
export function PromptInspector({ event, onClose }: { event: ActivityEvent; onClose: () => void }) {
  const p = event.payload || {};
  const prompt = p.prompt as string | undefined;
  const response = p.response_text as string | undefined;

  return (
    <div className="fixed inset-0 z-50 grid place-items-center bg-black/60 p-6" onClick={onClose}>
      <div
        className="glass flex max-h-[80vh] w-full max-w-3xl flex-col overflow-hidden rounded-2xl"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-4 py-3">
          <span className="font-mono text-xs text-[var(--color-brand-bright)]">{event.kind}</span>
          {event.agent && <span className="text-sm font-semibold">{event.agent}</span>}
          <span className="text-[11px] text-[var(--color-mist)]">{shortTime(event.ts)}</span>
          <button onClick={onClose} className="ml-auto rounded-lg p-1 hover:bg-[var(--color-ink-800)]">
            <X size={18} />
          </button>
        </div>

        <div className="min-h-0 flex-1 space-y-4 overflow-auto p-4 text-sm">
          <div className="text-[var(--color-mist)]">{event.summary}</div>

          {prompt && <Section title="Prompt" body={prompt} />}
          {response && <Section title="Response" body={response} />}

          <details className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)]/60">
            <summary className="cursor-pointer px-3 py-2 text-xs text-[var(--color-mist)]">Raw event payload</summary>
            <pre className="overflow-auto px-3 pb-3 text-[11px] text-[var(--color-mist)]">
              {JSON.stringify(p, null, 2)}
            </pre>
          </details>
        </div>
      </div>
    </div>
  );
}

function Section({ title, body }: { title: string; body: string }) {
  return (
    <div>
      <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">{title}</div>
      <pre className="max-h-64 overflow-auto whitespace-pre-wrap rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-950)] p-3 text-[12px] leading-relaxed">
        {body}
      </pre>
    </div>
  );
}
