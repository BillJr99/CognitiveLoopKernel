import { useEffect, useState } from "react";
import { CheckCircle2, Save, XCircle } from "lucide-react";
import { useProviders, useSaveProviders } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { Badge, Spinner } from "../common/ui";
import { SecretField } from "./SecretField";
import { ModelPicker } from "./ModelPicker";

const MASK = "••••••••";
const SECRET_FIELDS = ["api_key", "apikey", "token", "secret", "password"];
const isSecretField = (k: string) => SECRET_FIELDS.some((s) => k.toLowerCase().includes(s));

export function ProvidersForm() {
  const { activeId } = useActiveWorkspace();
  const { data, isLoading } = useProviders(activeId);
  const save = useSaveProviders(activeId);
  const [active, setActive] = useState<string>("");
  const [providers, setProviders] = useState<Record<string, Record<string, any>>>({});
  // Track which secret fields the user actively edited (key: "prov.field").
  const [touched, setTouched] = useState<Set<string>>(new Set());

  useEffect(() => {
    if (data) {
      setActive(data.active ?? "");
      setProviders(data.providers ?? {});
      setTouched(new Set());
    }
  }, [data]);

  if (isLoading || !data) return <div className="p-6"><Spinner /></div>;

  function setField(prov: string, field: string, value: any) {
    setProviders((p) => ({ ...p, [prov]: { ...p[prov], [field]: value } }));
  }

  function onSave() {
    // For untouched secret fields, send the mask sentinel so the backend
    // preserves the stored value rather than overwriting it.
    const payload: Record<string, Record<string, any>> = {};
    for (const [prov, block] of Object.entries(providers)) {
      const copy: Record<string, any> = { ...block };
      for (const field of Object.keys(copy)) {
        if (isSecretField(field) && !touched.has(`${prov}.${field}`) && copy[field]) {
          copy[field] = MASK;
        }
      }
      payload[prov] = copy;
    }
    save.mutate({ providers: payload, active });
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center">
        <div className="text-sm text-[var(--color-mist)]">
          Choose the active provider and edit per-provider settings.
        </div>
        <button
          onClick={onSave}
          disabled={save.isPending}
          className="ml-auto flex items-center gap-1.5 rounded-lg bg-[var(--color-brand)] px-4 py-1.5 text-sm font-semibold text-[var(--color-ink-950)] disabled:opacity-40"
        >
          {save.isPending ? <Spinner size={14} /> : <Save size={14} />} Save
        </button>
      </div>

      <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
        {Object.entries(providers).map(([name, block]) => {
          const available = data.available[name];
          const isActive = active === name;
          return (
            <div
              key={name}
              className={`card p-4 ${isActive ? "ring-1 ring-[var(--color-brand)]/50" : ""}`}
            >
              <div className="mb-3 flex items-center gap-2">
                <span className="text-sm font-semibold">{name}</span>
                {available ? (
                  <Badge tone="good"><CheckCircle2 size={11} /> available</Badge>
                ) : (
                  <Badge tone="muted"><XCircle size={11} /> unavailable</Badge>
                )}
                <button
                  onClick={() => setActive(name)}
                  disabled={isActive}
                  className={`ml-auto rounded-lg px-2.5 py-1 text-xs ${
                    isActive
                      ? "bg-[var(--color-brand)]/20 text-[var(--color-brand-bright)]"
                      : "border border-[var(--color-line)] text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
                  }`}
                >
                  {isActive ? "active" : "make active"}
                </button>
              </div>

              <div className="flex flex-col gap-2">
                {Object.entries(block as Record<string, any>)
                  .filter(([k]) => k !== "type" && k !== "description")
                  .map(([field, val]) => (
                    <div key={field} className="flex flex-col gap-1">
                      <label className="text-[11px] uppercase tracking-wide text-[var(--color-mist)]">{field}</label>
                      {field === "model" ? (
                        <ModelField
                          providerName={name}
                          block={block as Record<string, any>}
                          value={val}
                          onChange={(v) => setField(name, field, v)}
                        />
                      ) : isSecretField(field) ? (
                        <SecretField
                          stored={val === MASK || (!!val && !touched.has(`${name}.${field}`))}
                          value={touched.has(`${name}.${field}`) ? String(val ?? "") : null}
                          onChange={(v) => {
                            setTouched((t) => new Set(t).add(`${name}.${field}`));
                            setField(name, field, v ?? "");
                          }}
                        />
                      ) : typeof val === "boolean" ? (
                        <input
                          type="checkbox"
                          checked={val}
                          onChange={(e) => setField(name, field, e.target.checked)}
                          className="h-4 w-4"
                        />
                      ) : Array.isArray(val) ? (
                        <input
                          value={val.join(" ")}
                          onChange={(e) => setField(name, field, e.target.value.split(/\s+/).filter(Boolean))}
                          className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
                        />
                      ) : (
                        <input
                          value={val == null ? "" : String(val)}
                          onChange={(e) => setField(name, field, e.target.value)}
                          className="rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
                        />
                      )}
                    </div>
                  ))}
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

// Provider-card model field: thin wrapper over the shared ModelPicker.
function ModelField({
  providerName,
  block,
  value,
  onChange,
}: {
  providerName: string;
  block: Record<string, any>;
  value: any;
  onChange: (v: string) => void;
}) {
  return (
    <ModelPicker
      ptype={String(block.type || providerName)}
      endpoint={block.endpoint ? String(block.endpoint) : undefined}
      apiKey={block.api_key ? String(block.api_key) : undefined}
      value={value == null ? "" : String(value)}
      onChange={onChange}
    />
  );
}
