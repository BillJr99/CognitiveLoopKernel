import { useState } from "react";
import { Eye, KeyRound, Pencil } from "lucide-react";

const MASK = "••••••••";

// Renders a secret input. When a value is already stored (masked), the
// field shows a "stored" affordance with a Replace button; only when the
// user actively edits do we send a new plaintext value. Untouched secrets
// are submitted as the mask sentinel so the backend preserves them.
export function SecretField({
  stored,
  value,
  onChange,
}: {
  stored: boolean; // a value is already persisted
  value: string | null; // null = untouched (-> sentinel), else the new plaintext
  onChange: (v: string | null) => void;
}) {
  const [editing, setEditing] = useState(!stored);

  if (stored && !editing) {
    return (
      <div className="flex items-center gap-2">
        <div className="flex flex-1 items-center gap-2 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 text-sm text-[var(--color-mist)]">
          <KeyRound size={14} className="text-[var(--color-good)]" />
          <span className="font-mono">{MASK}</span>
          <span className="text-[11px]">stored</span>
        </div>
        <button
          onClick={() => {
            setEditing(true);
            onChange("");
          }}
          className="flex items-center gap-1 rounded-lg border border-[var(--color-line)] px-2.5 py-2 text-xs text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
        >
          <Pencil size={13} /> Replace
        </button>
      </div>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <div className="relative flex-1">
        <input
          type="password"
          value={value ?? ""}
          onChange={(e) => onChange(e.target.value)}
          placeholder="paste secret value"
          className="w-full rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 pr-9 text-sm outline-none focus:border-[var(--color-brand)]"
        />
        <Eye size={15} className="pointer-events-none absolute right-3 top-2.5 text-[var(--color-mist)]" />
      </div>
      {stored && (
        <button
          onClick={() => {
            setEditing(false);
            onChange(null); // revert to untouched -> sentinel on save
          }}
          className="rounded-lg border border-[var(--color-line)] px-2.5 py-2 text-xs text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
        >
          Keep
        </button>
      )}
    </div>
  );
}
