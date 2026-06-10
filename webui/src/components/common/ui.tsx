// Small shared presentational primitives.
import type { ReactNode } from "react";

export function Spinner({ size = 16 }: { size?: number }) {
  return (
    <span
      className="inline-block animate-spin rounded-full border-2 border-[var(--color-line)] border-t-[var(--color-brand)]"
      style={{ width: size, height: size }}
    />
  );
}

const TONES: Record<string, string> = {
  good: "bg-[var(--color-good)]/15 text-[var(--color-good)] border-[var(--color-good)]/30",
  warn: "bg-[var(--color-warn)]/15 text-[var(--color-warn)] border-[var(--color-warn)]/30",
  bad: "bg-[var(--color-bad)]/15 text-[var(--color-bad)] border-[var(--color-bad)]/30",
  brand: "bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)] border-[var(--color-brand)]/30",
  muted: "bg-[var(--color-ink-700)]/60 text-[var(--color-mist)] border-[var(--color-line)]",
};

export function Badge({
  tone = "muted",
  title,
  children,
}: {
  tone?: keyof typeof TONES;
  title?: string;
  children: ReactNode;
}) {
  return (
    <span
      title={title}
      className={`inline-flex items-center gap-1 rounded-full border px-2 py-0.5 text-[11px] font-medium ${TONES[tone]}`}
    >
      {children}
    </span>
  );
}

export function Stat({ label, value, sub }: { label: string; value: ReactNode; sub?: ReactNode }) {
  return (
    <div className="flex flex-col">
      <span className="text-[11px] uppercase tracking-wide text-[var(--color-mist)]">{label}</span>
      <span className="text-lg font-semibold tabular-nums">{value}</span>
      {sub && <span className="text-[11px] text-[var(--color-mist)]">{sub}</span>}
    </div>
  );
}

export function EmptyState({
  icon,
  title,
  hint,
  action,
}: {
  icon?: ReactNode;
  title: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="float-in flex flex-col items-center justify-center gap-3 rounded-2xl border border-dashed border-[var(--color-line)] bg-[var(--color-ink-900)]/30 p-12 text-center text-[var(--color-mist)]">
      {icon && (
        <div className="grid h-14 w-14 place-items-center rounded-2xl bg-[var(--color-ink-800)] ring-1 ring-[var(--color-line)]">
          {icon}
        </div>
      )}
      <div className="font-display text-lg font-semibold text-[var(--color-frost)]">{title}</div>
      {hint && <div className="max-w-md text-sm leading-relaxed">{hint}</div>}
      {action && <div className="mt-1">{action}</div>}
    </div>
  );
}

export function Toggle({ checked, onChange }: { checked: boolean; onChange: (v: boolean) => void }) {
  return (
    <button
      type="button"
      onClick={() => onChange(!checked)}
      className={`relative h-6 w-11 rounded-full transition-colors ${
        checked ? "bg-[var(--color-brand)]" : "bg-[var(--color-ink-700)]"
      }`}
      aria-pressed={checked}
    >
      <span
        className={`absolute top-0.5 h-5 w-5 rounded-full bg-white transition-all ${
          checked ? "left-[22px]" : "left-0.5"
        }`}
      />
    </button>
  );
}
