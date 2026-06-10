// Step 6: iterative refinement — ask for changes, loop back to the build.
import { useState } from "react";
import { CornerDownLeft, History, Send } from "lucide-react";
import { Spinner } from "../../common/ui";
import { StepShell } from "../StepShell";

export function FollowUpStep({
  rounds,
  sending,
  error,
  onSend,
  onBack,
}: {
  rounds: string[];
  sending: boolean;
  error: string | null;
  onSend: (request: string) => void;
  onBack: () => void;
}) {
  const [request, setRequest] = useState("");
  const [stopWhen, setStopWhen] = useState("");

  return (
    <StepShell
      step="followup"
      title="What should they change or add?"
      subtitle="The same team picks the project back up with your feedback — repeat as many times as you like."
      onBack={onBack}
    >
      <div className="card-lux p-5">
        <textarea
          autoFocus
          value={request}
          onChange={(e) => setRequest(e.target.value)}
          rows={4}
          placeholder="e.g. Make the homepage blue, add a FAQ section, and fix the broken link…"
          className="w-full resize-y rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-950)]/60 p-4 text-[15px] leading-relaxed outline-none transition-colors focus:border-[var(--color-brand)]"
          onKeyDown={(e) => {
            if (e.key === "Enter" && (e.metaKey || e.ctrlKey) && request.trim()) onSend(request.trim());
          }}
        />
        <div className="mt-1 flex items-center gap-1 text-[10px] text-[var(--color-mist)]">
          <CornerDownLeft size={11} /> Ctrl/Cmd + Enter to send
        </div>
        <div className="mt-3">
          <label className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
            Stop when <span className="font-normal normal-case opacity-60">(optional)</span>
          </label>
          <input
            type="text"
            value={stopWhen}
            onChange={(e) => setStopWhen(e.target.value)}
            placeholder="e.g. working prototype with README"
            className="input mt-1 w-full text-sm"
          />
        </div>
      </div>

      {error && (
        <div className="card border-[var(--color-bad)]/40 bg-[var(--color-bad)]/10 p-3 text-sm text-[var(--color-bad)]">
          {error}
        </div>
      )}

      <div className="flex justify-center">
        <button
          onClick={() => onSend(request.trim())}
          disabled={!request.trim() || sending}
          className="btn btn-primary !px-7"
        >
          {sending ? <Spinner size={16} /> : <Send size={16} />} Send it back to the team
        </button>
      </div>

      {rounds.length > 0 && (
        <div className="card p-4">
          <div className="mb-2 flex items-center gap-1.5 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
            <History size={12} /> What you've asked so far
          </div>
          <ol className="flex flex-col gap-1.5">
            {rounds.map((r, i) => (
              <li key={i} className="flex gap-2 text-xs text-[var(--color-mist)]">
                <span className="shrink-0 font-semibold text-[var(--color-brand-bright)]">{i + 1}.</span>
                <span className="leading-relaxed">{r}</span>
              </li>
            ))}
          </ol>
        </div>
      )}
    </StepShell>
  );
}
