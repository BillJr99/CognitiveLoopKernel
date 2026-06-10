import { useEffect, useMemo, useState } from "react";
import { Save, RotateCcw, Info } from "lucide-react";
import { useEnv, useSaveEnv } from "../../api/hooks";
import type { EnvVar } from "../../api/types";
import { Spinner, Toggle } from "../common/ui";
import { SecretField } from "./SecretField";
import { ModelPicker } from "./ModelPicker";

// .env model keys that get a live model dropdown, mapped to the provider type
// and the sibling endpoint / api-key keys used to probe.
const MODEL_KEYS: Record<string, { ptype: string; endpointKey: string; apiKeyKey?: string }> = {
  CLK_OLLAMA_MODEL: { ptype: "ollama", endpointKey: "CLK_OLLAMA_ENDPOINT" },
  CLK_OPENWEBUI_MODEL: { ptype: "openwebui", endpointKey: "CLK_OPENWEBUI_ENDPOINT", apiKeyKey: "CLK_OPENWEBUI_API_KEY" },
};

const MASK = "••••••••";

// Pending edits keyed by env key. For secrets, `null` means "untouched"
// (submit sentinel); a string means new plaintext. For non-secrets the
// string is the literal value.
type Edits = Record<string, string | null>;

export function EnvForm() {
  const { data, isLoading } = useEnv();
  const save = useSaveEnv();
  const [edits, setEdits] = useState<Edits>({});

  useEffect(() => {
    setEdits({});
  }, [data?.path]);

  const byGroup = useMemo(() => {
    const map = new Map<string, EnvVar[]>();
    (data?.vars ?? []).forEach((v) => {
      if (!map.has(v.group)) map.set(v.group, []);
      map.get(v.group)!.push(v);
    });
    return map;
  }, [data]);

  const dirty = Object.keys(edits).length > 0;

  function set(key: string, value: string | null) {
    setEdits((e) => ({ ...e, [key]: value }));
  }

  function current(v: EnvVar): string | null {
    if (v.key in edits) return edits[v.key];
    return v.is_secret ? null : v.value;
  }

  // Resolve the current string value of any env key (edited or stored) so a
  // model field can read its sibling endpoint/api-key.
  function valueOf(key: string): string {
    if (key in edits) return edits[key] ?? "";
    const v = (data?.vars ?? []).find((x) => x.key === key);
    return v ? v.value : "";
  }

  const activeProvider = valueOf("CLK_PROVIDER");

  async function onSave() {
    const values: Record<string, string | null> = {};
    for (const [key, val] of Object.entries(edits)) {
      // Secret untouched (null) -> send sentinel to preserve stored value.
      values[key] = val === null ? MASK : val;
    }
    await save.mutateAsync(values);
    setEdits({});
  }

  if (isLoading) return <div className="p-6"><Spinner /></div>;
  if (!data) return <div className="p-6 text-[var(--color-mist)]">Could not load .env.</div>;

  const groupOrder = data.groups;

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center gap-3">
        <div className="text-sm text-[var(--color-mist)]">
          Editing <code className="text-[var(--color-brand-bright)]">{data.path}</code>. Secrets are masked;
          changes apply to the next run.
        </div>
        <div className="ml-auto flex gap-2">
          {dirty && (
            <button
              onClick={() => setEdits({})}
              className="flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] px-3 py-1.5 text-sm text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
            >
              <RotateCcw size={14} /> Reset
            </button>
          )}
          <button
            onClick={onSave}
            disabled={!dirty || save.isPending}
            className="btn btn-primary !py-1.5"
          >
            {save.isPending ? <Spinner size={14} /> : <Save size={14} />} Save
          </button>
        </div>
      </div>

      {save.isError && (
        <div className="rounded-lg bg-[var(--color-bad)]/10 p-2 text-sm text-[var(--color-bad)]">
          {(save.error as Error).message}
        </div>
      )}

      <div
        className={`flex items-start gap-2 rounded-lg border p-3 text-sm ${
          !activeProvider || activeProvider === "shell"
            ? "border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10"
            : "border-[var(--color-line)] bg-[var(--color-ink-900)]/40"
        }`}
      >
        <Info size={16} className="mt-0.5 shrink-0 text-[var(--color-brand)]" />
        <div className="text-[var(--color-mist)]">
          <span className="font-semibold text-[var(--color-frost)]">Set your provider here.</span>{" "}
          <code>CLK_PROVIDER</code> selects which backend runs your agents
          {activeProvider ? (
            <> (currently <code className="text-[var(--color-brand-bright)]">{activeProvider}</code>)</>
          ) : (
            <> (currently <span className="text-[var(--color-warn)]">unset → falls back to <code>shell</code></span>)</>
          )}
          .{" "}
          {(!activeProvider || activeProvider === "shell") && (
            <span className="text-[var(--color-warn)]">
              <code>shell</code> only echoes prompts and never calls an LLM — set it to a real
              provider (e.g. <code>ollama</code>) or workflows won't do anything.
            </span>
          )}{" "}
          For <code>ollama</code>/<code>openwebui</code>, also set the endpoint and model below.
        </div>
      </div>

      {groupOrder.map((group) => {
        const vars = byGroup.get(group) ?? [];
        if (vars.length === 0) return null;
        return (
          <section key={group} className="card p-4">
            <h3 className="mb-3 text-sm font-semibold text-[var(--color-brand-bright)]">{group}</h3>
            <div className="grid grid-cols-1 gap-x-6 gap-y-3 md:grid-cols-2">
              {vars.map((v) => (
                <div key={v.key} className="flex flex-col gap-1">
                  <label className="flex items-center justify-between gap-2 text-sm">
                    <span title={v.help}>{v.label}</span>
                    <code className="text-[10px] text-[var(--color-mist)]">{v.key}</code>
                  </label>
                  <EnvInput v={v} value={current(v)} edited={v.key in edits} onChange={(val) => set(v.key, val)} valueOf={valueOf} />
                  {v.help && <span className="text-[11px] text-[var(--color-mist)]">{v.help}</span>}
                </div>
              ))}
            </div>
          </section>
        );
      })}
    </div>
  );
}

function EnvInput({
  v,
  value,
  edited,
  onChange,
  valueOf,
}: {
  v: EnvVar;
  value: string | null;
  edited: boolean;
  onChange: (val: string | null) => void;
  valueOf: (key: string) => string;
}) {
  if (v.is_secret || v.type === "secret") {
    return <SecretField stored={v.set && !edited} value={value} onChange={onChange} />;
  }
  const val = value ?? "";
  // Model fields get a live, probe-backed dropdown (fallback to text).
  const modelCfg = MODEL_KEYS[v.key];
  if (modelCfg) {
    return (
      <ModelPicker
        ptype={modelCfg.ptype}
        endpoint={valueOf(modelCfg.endpointKey)}
        apiKey={modelCfg.apiKeyKey ? valueOf(modelCfg.apiKeyKey) : undefined}
        value={val}
        onChange={(s) => onChange(s)}
        placeholder={v.default}
      />
    );
  }
  if (v.type === "bool") {
    return <Toggle checked={val === "true"} onChange={(c) => onChange(c ? "true" : "false")} />;
  }
  if (v.type === "enum") {
    return (
      <select
        value={val}
        onChange={(e) => onChange(e.target.value)}
        className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 text-sm outline-none focus:border-[var(--color-brand)]"
      >
        {!v.choices.includes(val) && <option value={val}>{val || "(unset)"}</option>}
        {v.choices.map((c) => (
          <option key={c} value={c}>{c}</option>
        ))}
      </select>
    );
  }
  return (
    <input
      type={v.type === "int" || v.type === "float" ? "number" : "text"}
      value={val}
      placeholder={v.default}
      onChange={(e) => onChange(e.target.value)}
      className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 text-sm outline-none focus:border-[var(--color-brand)]"
    />
  );
}
