import { useEffect, useState } from "react";
import { Save } from "lucide-react";
import { useClkConfig, useSaveClkConfig } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { Spinner, Toggle } from "../common/ui";

// Known enum choices so nested string knobs render as dropdowns.
const ENUMS: Record<string, string[]> = {
  auto_consensus: ["off", "on_careful", "always"],
  auto_refine: ["off", "careful_only", "all"],
  plateau_action: ["off", "escalate_only", "reframe_only", "escalate_then_reframe"],
  auth_mode: ["cli", "apikey"],
};

function setPath(obj: any, path: string[], value: any): any {
  if (path.length === 0) return value;
  const [head, ...rest] = path;
  return { ...obj, [head]: setPath(obj?.[head] ?? {}, rest, value) };
}

export function ClkConfigForm() {
  const { activeId } = useActiveWorkspace();
  const { data, isLoading } = useClkConfig(activeId);
  const save = useSaveClkConfig(activeId);
  const [draft, setDraft] = useState<Record<string, any> | null>(null);

  useEffect(() => {
    if (data?.config) setDraft(data.config);
  }, [data?.config]);

  if (isLoading || !draft) return <div className="p-6"><Spinner /></div>;

  const update = (path: string[], value: any) => setDraft((d) => setPath(d, path, value));

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center">
        <div className="text-sm text-[var(--color-mist)]">
          Per-workspace harness config (<code className="text-[var(--color-brand-bright)]">clk.config.json</code>).
        </div>
        <button
          onClick={() => draft && save.mutate(draft)}
          disabled={save.isPending}
          className="ml-auto flex items-center gap-1.5 rounded-lg bg-[var(--color-brand)] px-4 py-1.5 text-sm font-semibold text-[var(--color-ink-950)] disabled:opacity-40"
        >
          {save.isPending ? <Spinner size={14} /> : <Save size={14} />} Save
        </button>
      </div>

      <div className="card p-4">
        <ConfigTree value={draft} path={[]} onChange={update} />
      </div>
    </div>
  );
}

function ConfigTree({
  value,
  path,
  onChange,
}: {
  value: Record<string, any>;
  path: string[];
  onChange: (path: string[], v: any) => void;
}) {
  return (
    <div className="grid grid-cols-1 gap-x-6 gap-y-3 md:grid-cols-2">
      {Object.entries(value).map(([key, val]) => {
        const here = [...path, key];
        if (val !== null && typeof val === "object" && !Array.isArray(val)) {
          return (
            <fieldset key={key} className="col-span-full rounded-xl border border-[var(--color-line)] p-3">
              <legend className="px-1 text-xs font-semibold text-[var(--color-brand-bright)]">{key}</legend>
              <ConfigTree value={val} path={here} onChange={onChange} />
            </fieldset>
          );
        }
        return (
          <div key={key} className="flex items-center justify-between gap-3">
            <label className="text-sm">{key}</label>
            <Widget keyName={key} value={val} onChange={(v) => onChange(here, v)} />
          </div>
        );
      })}
    </div>
  );
}

function Widget({ keyName, value, onChange }: { keyName: string; value: any; onChange: (v: any) => void }) {
  if (typeof value === "boolean") {
    return <Toggle checked={value} onChange={onChange} />;
  }
  if (ENUMS[keyName]) {
    return (
      <select
        value={String(value ?? "")}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-2 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
      >
        {ENUMS[keyName].map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
    );
  }
  if (typeof value === "number") {
    return (
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(e.target.value === "" ? 0 : Number(e.target.value))}
        className="w-28 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-2 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
      />
    );
  }
  if (value === null) {
    return <span className="text-xs text-[var(--color-mist)]">null</span>;
  }
  return (
    <input
      value={String(value)}
      onChange={(e) => onChange(e.target.value)}
      className="w-44 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-2 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
    />
  );
}
