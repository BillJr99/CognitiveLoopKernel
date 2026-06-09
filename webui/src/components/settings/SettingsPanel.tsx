import { useState } from "react";
import { Stethoscope } from "lucide-react";
import { useDoctor } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { Badge } from "../common/ui";
import { EnvForm } from "./EnvForm";
import { ClkConfigForm } from "./ClkConfigForm";
import { ProvidersForm } from "./ProvidersForm";
import { RosterPanel } from "./RosterPanel";

type Tab = "env" | "config" | "providers" | "roster";

const TABS: { id: Tab; label: string }[] = [
  { id: "env", label: ".env (global)" },
  { id: "providers", label: "Providers" },
  { id: "config", label: "Harness config" },
  { id: "roster", label: "Roster" },
];

export function SettingsPanel() {
  const [tab, setTab] = useState<Tab>("env");
  const { activeId } = useActiveWorkspace();
  const { data: doctor } = useDoctor(activeId);

  const fails = doctor?.findings.filter((f) => f.level === "fail") ?? [];
  const warns = doctor?.findings.filter((f) => f.level === "warn") ?? [];

  return (
    <div className="flex flex-col gap-4">
      {/* Doctor strip */}
      {doctor && (
        <div className="glass flex flex-wrap items-center gap-3 rounded-xl px-4 py-2.5">
          <Stethoscope size={16} className="text-[var(--color-brand)]" />
          <span className="text-sm font-medium">Health</span>
          <span className="text-[11px] text-[var(--color-mist)]">
            provider <b className="text-[var(--color-frost)]">{doctor.active_provider}</b> · auth {doctor.auth_mode}
          </span>
          {fails.length === 0 && warns.length === 0 && <Badge tone="good">all checks pass</Badge>}
          {fails.map((f) => (
            <Badge key={f.name} tone="bad" title={f.message}>{f.name}: {f.message}</Badge>
          ))}
          {warns.map((f) => (
            <Badge key={f.name} tone="warn" title={f.message}>{f.name}</Badge>
          ))}
        </div>
      )}

      {/* Tabs */}
      <div className="flex flex-wrap gap-1 border-b border-[var(--color-line)]">
        {TABS.map((t) => (
          <button
            key={t.id}
            onClick={() => setTab(t.id)}
            className={`-mb-px rounded-t-lg px-4 py-2 text-sm transition-colors ${
              tab === t.id
                ? "border-b-2 border-[var(--color-brand)] text-[var(--color-brand-bright)]"
                : "text-[var(--color-mist)] hover:text-[var(--color-frost)]"
            }`}
          >
            {t.label}
          </button>
        ))}
      </div>

      {tab === "env" && <EnvForm />}
      {tab === "providers" && <ProvidersForm />}
      {tab === "config" && <ClkConfigForm />}
      {tab === "roster" && <RosterPanel />}
    </div>
  );
}
