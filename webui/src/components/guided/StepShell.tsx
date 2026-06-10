// Shared chrome for wizard steps: progress rail, back button, and a heading.
import type { ReactNode } from "react";
import { ArrowLeft, Check } from "lucide-react";

const RAIL: { id: string; label: string }[] = [
  { id: "provider", label: "Choose your AI" },
  { id: "model", label: "Pick a model" },
  { id: "idea", label: "Your idea" },
  { id: "working", label: "Build" },
  { id: "files", label: "Results" },
];

// followup shares the "Results" dot so the rail stays a clean five steps.
const RAIL_ALIAS: Record<string, string> = { followup: "files" };

export function StepShell({
  step,
  title,
  subtitle,
  onBack,
  children,
  wide,
}: {
  step: string;
  title: string;
  subtitle?: string;
  onBack?: () => void;
  children: ReactNode;
  wide?: boolean;
}) {
  const current = RAIL_ALIAS[step] ?? step;
  const currentIdx = RAIL.findIndex((r) => r.id === current);

  return (
    <div key={step} className={`step-in mx-auto flex w-full ${wide ? "max-w-5xl" : "max-w-3xl"} flex-col gap-6 px-6 py-8`}>
      {/* Progress rail */}
      <div className="flex items-center justify-center gap-0">
        {RAIL.map((r, i) => {
          const done = currentIdx > i;
          const active = currentIdx === i;
          return (
            <div key={r.id} className="flex items-center">
              {i > 0 && (
                <div
                  className={`h-px w-8 sm:w-14 ${
                    done || active ? "bg-[var(--color-brand)]/60" : "bg-[var(--color-line)]"
                  }`}
                />
              )}
              <div className="flex flex-col items-center gap-1.5">
                <div
                  className={`grid h-7 w-7 place-items-center rounded-full border text-[11px] font-semibold transition-all ${
                    done
                      ? "border-[var(--color-brand)]/60 bg-[var(--color-brand)]/20 text-[var(--color-brand-bright)]"
                      : active
                        ? "progress-glow border-transparent bg-gradient-to-br from-[var(--color-brand)] to-[var(--color-iris)] text-[var(--color-ink-950)]"
                        : "border-[var(--color-line)] bg-[var(--color-ink-900)] text-[var(--color-mist)]"
                  }`}
                >
                  {done ? <Check size={13} /> : i + 1}
                </div>
                <span
                  className={`hidden text-[10px] sm:block ${
                    active ? "font-semibold text-[var(--color-brand-bright)]" : "text-[var(--color-mist)]"
                  }`}
                >
                  {r.label}
                </span>
              </div>
            </div>
          );
        })}
      </div>

      {/* Heading */}
      <div className="relative text-center">
        {onBack && (
          <button
            onClick={onBack}
            className="btn btn-ghost absolute left-0 top-1/2 -translate-y-1/2 !px-2.5 !py-1.5 text-xs"
            aria-label="Go back"
          >
            <ArrowLeft size={14} /> Back
          </button>
        )}
        <h1 className="font-display text-2xl font-bold tracking-tight sm:text-3xl">{title}</h1>
        {subtitle && <p className="mx-auto mt-2 max-w-xl text-sm leading-relaxed text-[var(--color-mist)]">{subtitle}</p>}
      </div>

      {children}
    </div>
  );
}
