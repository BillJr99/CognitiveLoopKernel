// Step 3: the user's idea, in plain language.
import { useState } from "react";
import { Lightbulb, Rocket } from "lucide-react";
import type { DiscoveredProvider } from "../../../api/types";
import { Spinner } from "../../common/ui";
import { StepShell } from "../StepShell";
import { EXAMPLE_IDEAS, providerMeta } from "../friendly";

export function IdeaStep({
  provider,
  model,
  launching,
  error,
  stopWhen,
  onStopWhenChange,
  onLaunch,
  onBack,
}: {
  provider: DiscoveredProvider;
  model: string;
  launching: boolean;
  error: string | null;
  stopWhen: string;
  onStopWhenChange: (v: string) => void;
  onLaunch: (question: string) => void;
  onBack: () => void;
}) {
  const [question, setQuestion] = useState("");
  const meta = providerMeta(provider);
  const usingNote = model ? `${provider.label} · ${model}` : `${provider.label}${meta.modelNote ? ` · ${meta.modelNote.toLowerCase()}` : ""}`;

  return (
    <StepShell
      step="idea"
      title="What would you like to build or figure out?"
      subtitle="Describe it like you would to a colleague. The more detail, the better — but a single sentence works too."
      onBack={onBack}
    >
      <div className="card-lux p-5">
        <textarea
          autoFocus
          value={question}
          onChange={(e) => setQuestion(e.target.value)}
          rows={5}
          placeholder="e.g. Build a simple website for my bakery with a menu page and a contact form…"
          className="w-full resize-y rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-950)]/60 p-4 text-[15px] leading-relaxed outline-none transition-colors focus:border-[var(--color-brand)]"
        />
        <div className="mt-3 flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1 text-[11px] text-[var(--color-mist)]">
            <Lightbulb size={12} /> Try:
          </span>
          {EXAMPLE_IDEAS.map((ex) => (
            <button
              key={ex}
              onClick={() => setQuestion(ex)}
              className="rounded-full border border-[var(--color-line)] bg-[var(--color-ink-800)]/60 px-3 py-1 text-[11px] text-[var(--color-mist)] transition-colors hover:border-[var(--color-line-bright)] hover:text-[var(--color-frost)]"
            >
              {ex.length > 52 ? `${ex.slice(0, 52)}…` : ex}
            </button>
          ))}
        </div>
      </div>

      <div className="mt-3">
        <label className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
          Stop when <span className="font-normal normal-case opacity-60">(optional)</span>
        </label>
        <input
          type="text"
          value={stopWhen}
          onChange={(e) => onStopWhenChange(e.target.value)}
          placeholder="e.g. working prototype with README"
          className="input mt-1 w-full text-sm"
        />
      </div>

      {error && (
        <div className="card border-[var(--color-bad)]/40 bg-[var(--color-bad)]/10 p-3 text-sm text-[var(--color-bad)]">
          {error}
        </div>
      )}

      <div className="flex flex-col items-center gap-2">
        <button
          onClick={() => onLaunch(question.trim())}
          disabled={!question.trim() || launching}
          className="btn btn-primary !px-8 !py-3 !text-base"
        >
          {launching ? <Spinner size={18} /> : <Rocket size={18} />}
          {launching ? "Setting things up…" : "Start building"}
        </button>
        <span className="text-[11px] text-[var(--color-mist)]">Using {usingNote}</span>
      </div>
    </StepShell>
  );
}
