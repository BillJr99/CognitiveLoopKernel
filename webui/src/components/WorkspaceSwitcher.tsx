import { useState } from "react";
import { FolderPlus, Trash2, Check, Pencil, X } from "lucide-react";
import { useCreateWorkspace, useDeleteWorkspace, useRenameWorkspace, useWorkspaces } from "../api/hooks";
import { useActiveWorkspace } from "../state/activeWorkspace";
import { Spinner } from "./common/ui";

export function WorkspaceSwitcher() {
  const { data, isLoading } = useWorkspaces();
  const { activeId, setActiveId } = useActiveWorkspace();
  const create = useCreateWorkspace();
  const del = useDeleteWorkspace();
  const rename = useRenameWorkspace();
  const [adding, setAdding] = useState(false);
  const [name, setName] = useState("");
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editName, setEditName] = useState("");

  const workspaces = data?.workspaces ?? [];

  function startEdit(id: string, current: string) {
    setEditingId(id);
    setEditName(current);
  }

  async function submitEdit() {
    const trimmed = editName.trim();
    if (trimmed && editingId) await rename.mutateAsync({ id: editingId, name: trimmed });
    setEditingId(null);
  }

  async function submit() {
    const trimmed = name.trim();
    if (!trimmed) return;
    const res = await create.mutateAsync(trimmed);
    setActiveId(res.workspace_id);
    setName("");
    setAdding(false);
  }

  return (
    <div className="flex flex-col gap-2">
      <div className="flex items-center justify-between px-1">
        <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
          Workspaces
        </span>
        <button
          onClick={() => setAdding((v) => !v)}
          className="rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
          title="New workspace"
        >
          <FolderPlus size={16} />
        </button>
      </div>

      {adding && (
        <div className="flex gap-1">
          <input
            autoFocus
            value={name}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
            placeholder="project name"
            className="min-w-0 flex-1 rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-2 py-1 text-sm outline-none focus:border-[var(--color-brand)]"
          />
          <button
            onClick={submit}
            disabled={create.isPending}
            className="rounded-lg bg-[var(--color-brand)] px-2 text-[var(--color-ink-950)]"
          >
            {create.isPending ? <Spinner size={14} /> : <Check size={16} />}
          </button>
        </div>
      )}

      <div className="flex flex-col gap-1">
        {isLoading && <div className="px-2 py-1 text-sm text-[var(--color-mist)]"><Spinner size={14} /></div>}
        {!isLoading && workspaces.length === 0 && (
          <div className="px-2 py-1 text-xs text-[var(--color-mist)]">No workspaces yet.</div>
        )}
        {workspaces.map((w) => {
          const active = w.id === activeId;
          return (
            <div
              key={w.id}
              className={`group flex items-center gap-2 rounded-lg px-2 py-1.5 text-sm transition-colors ${
                active ? "bg-[var(--color-ink-800)] ring-1 ring-[var(--color-brand)]/40" : "hover:bg-[var(--color-ink-800)]"
              }`}
            >
              {editingId === w.id ? (
                <>
                  <input
                    autoFocus
                    value={editName}
                    onChange={(e) => setEditName(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") submitEdit();
                      if (e.key === "Escape") setEditingId(null);
                    }}
                    className="min-w-0 flex-1 rounded border border-[var(--color-line)] bg-[var(--color-ink-900)] px-1.5 py-0.5 text-sm outline-none focus:border-[var(--color-brand)]"
                  />
                  <button onClick={submitEdit} className="text-[var(--color-good)] hover:opacity-80" title="Save name">
                    <Check size={14} />
                  </button>
                  <button onClick={() => setEditingId(null)} className="text-[var(--color-mist)] hover:text-[var(--color-frost)]" title="Cancel">
                    <X size={14} />
                  </button>
                </>
              ) : (
                <>
                  <button
                    onClick={() => setActiveId(w.id)}
                    onDoubleClick={() => startEdit(w.id, w.name)}
                    title="Click to switch · double-click to rename"
                    className="min-w-0 flex-1 truncate text-left"
                  >
                    <span className={active ? "text-[var(--color-frost)]" : "text-[var(--color-mist)]"}>{w.name}</span>
                  </button>
                  <button
                    onClick={() => startEdit(w.id, w.name)}
                    className="opacity-0 transition-opacity group-hover:opacity-100 text-[var(--color-mist)] hover:text-[var(--color-brand-bright)]"
                    title="Rename"
                  >
                    <Pencil size={13} />
                  </button>
                  <button
                    onClick={() => {
                      if (confirm(`Delete workspace "${w.name}"? This removes its files.`)) {
                        del.mutate(w.id);
                        if (active) setActiveId(null);
                      }
                    }}
                    className="opacity-0 transition-opacity group-hover:opacity-100 text-[var(--color-mist)] hover:text-[var(--color-bad)]"
                    title="Delete"
                  >
                    <Trash2 size={14} />
                  </button>
                </>
              )}
            </div>
          );
        })}
      </div>
    </div>
  );
}
