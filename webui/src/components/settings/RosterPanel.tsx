import { useEffect, useState } from "react";
import { Plus, Save, Trash2 } from "lucide-react";
import { useAgents, useSaveAgents } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { Spinner } from "../common/ui";

const BASELINE = new Set(["chief", "qa", "ralph"]);

export function RosterPanel() {
  const { activeId } = useActiveWorkspace();
  const { data, isLoading } = useAgents(activeId);
  const save = useSaveAgents(activeId);
  const [agents, setAgents] = useState<Record<string, any>>({});
  const [newName, setNewName] = useState("");

  useEffect(() => {
    if (data?.agents) setAgents(data.agents);
  }, [data?.agents]);

  if (isLoading) return <div className="p-6"><Spinner /></div>;

  function setRole(name: string, role: string) {
    setAgents((a) => ({ ...a, [name]: { ...a[name], role } }));
  }
  function addAgent() {
    const n = newName.trim().toLowerCase().replace(/\s+/g, "_");
    if (!n || agents[n]) return;
    setAgents((a) => ({ ...a, [n]: { prompt: `${n}.md`, provider: null, role: "" } }));
    setNewName("");
  }
  function remove(name: string) {
    setAgents((a) => {
      const c = { ...a };
      delete c[name];
      return c;
    });
  }

  return (
    <div className="flex flex-col gap-4">
      <div className="flex items-center">
        <div className="text-sm text-[var(--color-mist)]">
          The roster (<code className="text-[var(--color-brand-bright)]">agents.json</code>). The chief also
          mints roles dynamically as work demands.
        </div>
        <button
          onClick={() => save.mutate(agents)}
          disabled={save.isPending}
          className="btn btn-primary ml-auto !py-1.5"
        >
          {save.isPending ? <Spinner size={14} /> : <Save size={14} />} Save
        </button>
      </div>

      <div className="card divide-y divide-[var(--color-line)]">
        {Object.entries(agents).map(([name, cfg]) => (
          <div key={name} className="flex items-center gap-3 p-3">
            <div className="w-24 shrink-0 font-medium">{name}</div>
            <input
              value={(cfg as any).role ?? ""}
              onChange={(e) => setRole(name, e.target.value)}
              placeholder="one-line role description"
              className="flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-1.5 text-sm outline-none focus:border-[var(--color-brand)]"
            />
            {BASELINE.has(name) ? (
              <span className="w-8 text-center text-[10px] uppercase text-[var(--color-mist)]">core</span>
            ) : (
              <button onClick={() => remove(name)} className="p-1 text-[var(--color-mist)] hover:text-[var(--color-bad)]">
                <Trash2 size={15} />
              </button>
            )}
          </div>
        ))}
      </div>

      <div className="flex gap-2">
        <input
          value={newName}
          onChange={(e) => setNewName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && addAgent()}
          placeholder="new_role_name"
          className="flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-2 text-sm outline-none focus:border-[var(--color-brand)]"
        />
        <button
          onClick={addAgent}
          className="flex items-center gap-1.5 rounded-lg border border-[var(--color-line)] px-3 py-2 text-sm text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
        >
          <Plus size={15} /> Add role
        </button>
      </div>
    </div>
  );
}
