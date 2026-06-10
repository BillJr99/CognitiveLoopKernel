// Step 2: pick a model from the provider's installed list (HTTP providers
// only — CLI providers skip this step and use their own default).
import { useState } from "react";
import { ArrowRight, Boxes, Check, RefreshCw } from "lucide-react";
import { useProbeModels } from "../../../api/hooks";
import type { DiscoveredProvider } from "../../../api/types";
import { StepShell } from "../StepShell";

export function ModelStep({
  provider,
  apiKey,
  initial,
  onPick,
  onBack,
}: {
  provider: DiscoveredProvider;
  apiKey: string;
  initial: string;
  onPick: (model: string) => void;
  onBack: () => void;
}) {
  const [models, setModels] = useState<string[]>(provider.models);
  const [selected, setSelected] = useState<string>(initial || provider.models[0] || "");
  const probe = useProbeModels();

  async function refresh() {
    const res = await probe.mutateAsync({
      type: provider.type,
      endpoint: provider.endpoint ?? undefined,
      api_key: apiKey || undefined,
    });
    if (res.models?.length) {
      setModels(res.models);
      if (!res.models.includes(selected)) setSelected(res.models[0]);
    }
  }

  return (
    <StepShell
      step="model"
      title="Pick a model"
      subtitle={`These models are installed on your ${provider.label} server. Bigger models are usually smarter but slower.`}
      onBack={onBack}
    >
      <div className="flex items-center justify-center">
        <button onClick={refresh} disabled={probe.isPending} className="btn btn-ghost !py-1.5 text-xs">
          <RefreshCw size={13} className={probe.isPending ? "animate-spin" : ""} /> Refresh list
        </button>
      </div>

      <div className="flex flex-col gap-2">
        {models.length === 0 ? (
          <div className="card p-5 text-center text-sm text-[var(--color-mist)]">
            No models installed yet. Run{" "}
            <code className="font-mono text-[var(--color-frost)]">ollama pull llama3.1</code> in a terminal,
            then refresh.
          </div>
        ) : (
          models.map((m) => {
            const active = selected === m;
            return (
              <button
                key={m}
                onClick={() => setSelected(m)}
                className={`card flex items-center gap-3 p-4 text-left transition-all ${
                  active
                    ? "border-[var(--color-brand)]/60 shadow-[0_0_0_1px_rgba(122,162,255,0.35),0_12px_32px_-14px_rgba(122,162,255,0.5)]"
                    : "card-hover"
                }`}
              >
                <div
                  className={`grid h-9 w-9 shrink-0 place-items-center rounded-lg ${
                    active
                      ? "bg-gradient-to-br from-[var(--color-brand)] to-[var(--color-iris)] text-[var(--color-ink-950)]"
                      : "bg-[var(--color-ink-700)] text-[var(--color-mist)]"
                  }`}
                >
                  <Boxes size={18} />
                </div>
                <span className={`flex-1 font-mono text-sm ${active ? "text-[var(--color-frost)]" : "text-[var(--color-mist)]"}`}>
                  {m}
                </span>
                {active && <Check size={16} className="text-[var(--color-brand-bright)]" />}
              </button>
            );
          })
        )}
      </div>

      <div className="flex justify-center">
        <button onClick={() => onPick(selected)} disabled={!selected} className="btn btn-primary !px-7">
          Continue <ArrowRight size={16} />
        </button>
      </div>
    </StepShell>
  );
}
