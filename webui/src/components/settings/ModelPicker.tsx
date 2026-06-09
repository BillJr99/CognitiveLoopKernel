import { useEffect, useRef, useState } from "react";
import { RefreshCw, Wifi, WifiOff, Pencil } from "lucide-react";
import { useProbeModels } from "../../api/hooks";
import type { ProbeResponse } from "../../api/types";
import { Spinner } from "../common/ui";

const MASK = "••••••••";

// Shared model field: for HTTP providers (ollama / openwebui) it auto-probes
// the endpoint and offers a dropdown of installed models; otherwise (or when
// unreachable) it falls back to a plain text box. Used by both the Providers
// form and the .env editor.
export function ModelPicker({
  ptype,
  endpoint,
  apiKey,
  value,
  onChange,
  placeholder,
}: {
  ptype: string;
  endpoint?: string;
  apiKey?: string;
  value: string;
  onChange: (v: string) => void;
  placeholder?: string;
}) {
  const probe = useProbeModels();
  const [result, setResult] = useState<ProbeResponse | null>(null);
  const [manual, setManual] = useState(false);
  const probedFor = useRef<string | null>(null);

  const httpProvider = ptype === "ollama" || ptype === "openwebui";
  const endpointStr = (endpoint || "").trim();

  async function doProbe() {
    const key = apiKey && apiKey !== MASK ? apiKey : undefined;
    const r = await probe.mutateAsync({ type: ptype, endpoint: endpointStr || undefined, api_key: key });
    setResult(r);
    setManual(false);
  }

  // Auto-probe once per (type, endpoint); re-probe when the endpoint changes.
  useEffect(() => {
    if (!httpProvider) return;
    const key = `${ptype}|${endpointStr}`;
    if (probedFor.current === key) return;
    probedFor.current = key;
    doProbe();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [httpProvider, ptype, endpointStr]);

  const inputCls =
    "min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 text-sm outline-none focus:border-[var(--color-brand)]";
  const btnCls =
    "flex shrink-0 items-center gap-1 rounded-lg border border-[var(--color-line)] px-2 py-2 text-xs text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-frost)]";

  const showDropdown = httpProvider && !!result?.supported && (result?.models.length ?? 0) > 0 && !manual;

  return (
    <div className="flex flex-col gap-1">
      {showDropdown ? (
        <div className="flex gap-1.5">
          <select value={value} onChange={(e) => onChange(e.target.value)} className={inputCls}>
            {value && !result!.models.includes(value) && <option value={value}>{value} (current)</option>}
            {result!.models.map((m) => (
              <option key={m} value={m}>{m}</option>
            ))}
          </select>
          <button type="button" onClick={() => setManual(true)} className={btnCls} title="Type a model id manually">
            <Pencil size={13} />
          </button>
          <button type="button" onClick={doProbe} className={btnCls} title="Refresh model list">
            <RefreshCw size={13} className={probe.isPending ? "animate-spin" : ""} />
          </button>
        </div>
      ) : (
        <div className="flex gap-1.5">
          <input
            value={value}
            onChange={(e) => onChange(e.target.value)}
            placeholder={placeholder || (ptype === "ollama" ? "e.g. llama3.1" : "model id")}
            className={inputCls}
          />
          {httpProvider && (
            <button type="button" onClick={doProbe} disabled={probe.isPending} className={btnCls} title="Fetch models from the endpoint">
              {probe.isPending ? <Spinner size={12} /> : <RefreshCw size={13} />} models
            </button>
          )}
        </div>
      )}
      {httpProvider && result && (
        <div className="flex items-center gap-1 text-[11px]">
          {result.reachable ? (
            <span className="flex items-center gap-1 text-[var(--color-good)]">
              <Wifi size={11} /> reachable — {result.models.length} model{result.models.length === 1 ? "" : "s"}
            </span>
          ) : (
            <span className="flex items-center gap-1 text-[var(--color-warn)]">
              <WifiOff size={11} /> endpoint unreachable — type the model id manually
            </span>
          )}
        </div>
      )}
    </div>
  );
}
