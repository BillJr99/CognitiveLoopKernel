import { useEffect, useMemo, useRef, useState } from "react";
import {
  FileText,
  Save,
  RefreshCw,
  Send,
  Bot,
  User,
  Square,
  FolderOpen,
  Loader2,
} from "lucide-react";
import {
  useWorkspaceFiles,
  useFileContent,
  useSaveFile,
  useSaveIdea,
  useStartTask,
  useCancelTask,
  useWorkflows,
  useDoctor,
} from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import type { FileEntry } from "../../api/types";
import { Badge, EmptyState, Spinner } from "../common/ui";

function fmtSize(n: number): string {
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  return `${(n / (1024 * 1024)).toFixed(1)} MB`;
}

interface ChatTurn {
  id: string;
  role: "user" | "agent";
  text: string;
  taskId?: string;
  status?: "running" | "done" | "failed" | "cancelled";
}

export function FilesView() {
  const { activeId } = useActiveWorkspace();
  const { data, isLoading, refetch, isRefetching } = useWorkspaceFiles(activeId);
  const [selected, setSelected] = useState<string | null>(null);

  const files = useMemo(() => data?.files ?? [], [data]);

  if (!activeId) {
    return <EmptyState icon={<FolderOpen size={22} />} title="No workspace selected" hint="Pick a workspace to browse the files your agents create." />;
  }

  return (
    <div className="flex h-full min-h-0 gap-3">
      {/* File list */}
      <div className="card flex w-64 shrink-0 flex-col">
        <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2.5">
          <FileText size={15} className="text-[var(--color-brand)]" />
          <span className="text-sm font-semibold">Files</span>
          <span className="text-[11px] text-[var(--color-mist)]">{files.length}</span>
          <button
            onClick={() => refetch()}
            title="Refresh"
            className="ml-auto rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
          >
            <RefreshCw size={14} className={isRefetching ? "animate-spin" : ""} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-1.5">
          {isLoading ? (
            <div className="px-2 py-2 text-sm text-[var(--color-mist)]"><Spinner size={14} /></div>
          ) : files.length === 0 ? (
            <div className="px-2 py-2 text-xs text-[var(--color-mist)]">
              No files yet. Ask the agents to build something on the Run tab.
            </div>
          ) : (
            <ul className="flex flex-col">
              {files.map((f) => (
                <FileRow key={f.path} file={f} active={f.path === selected} onClick={() => setSelected(f.path)} />
              ))}
            </ul>
          )}
        </div>
        {data?.truncated && (
          <div className="border-t border-[var(--color-line)] px-3 py-1.5 text-[11px] text-[var(--color-warn)]">
            Showing first {files.length} files.
          </div>
        )}
      </div>

      {/* Editor + chat */}
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        <FileEditor ws={activeId} path={selected} />
        <AgentChat ws={activeId} selectedPath={selected} />
      </div>
    </div>
  );
}

function FileRow({ file, active, onClick }: { file: FileEntry; active: boolean; onClick: () => void }) {
  return (
    <li>
      <button
        onClick={onClick}
        className={`flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-left text-sm transition-colors ${
          active ? "bg-[var(--color-ink-800)] ring-1 ring-[var(--color-brand)]/40 text-[var(--color-frost)]" : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
        }`}
      >
        <FileText size={13} className="shrink-0 opacity-70" />
        <span className="min-w-0 flex-1 truncate" title={file.path}>{file.path}</span>
        <span className="shrink-0 text-[10px] tabular-nums opacity-60">{fmtSize(file.size)}</span>
      </button>
    </li>
  );
}

function FileEditor({ ws, path }: { ws: string; path: string | null }) {
  const { data, isLoading } = useFileContent(ws, path);
  const save = useSaveFile(ws);
  const [draft, setDraft] = useState("");
  const loadedFor = useRef<string | null>(null);

  // Reset the draft whenever a different file's content arrives.
  useEffect(() => {
    if (data && !data.binary && data.path === path && loadedFor.current !== path) {
      setDraft(data.content ?? "");
      loadedFor.current = path;
    }
    if (!path) loadedFor.current = null;
  }, [data, path]);

  if (!path) {
    return (
      <div className="card grid flex-1 place-items-center text-sm text-[var(--color-mist)]">
        Select a file to view and edit it.
      </div>
    );
  }

  // A truncated read only has the first chunk; saving it would clobber the
  // rest of the file, so it's view-only.
  const truncated = !!data?.truncated;
  const dirty = data && !data.binary && !truncated && draft !== (data.content ?? "");

  return (
    <div className="card flex min-h-0 flex-[2] flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2">
        <FileText size={14} className="text-[var(--color-brand)]" />
        <span className="truncate font-mono text-xs">{path}</span>
        {truncated && <Badge tone="warn">truncated · read-only</Badge>}
        {dirty && <Badge tone="brand">unsaved</Badge>}
        <button
          onClick={() => path && dirty && save.mutate({ path, content: draft })}
          disabled={!dirty || save.isPending}
          title={truncated ? "File too large to edit safely in-browser" : undefined}
          className="ml-auto flex items-center gap-1.5 rounded-lg bg-[var(--color-brand)] px-3 py-1 text-xs font-semibold text-[var(--color-ink-950)] disabled:opacity-40"
        >
          {save.isPending ? <Spinner size={12} /> : <Save size={13} />} Save
        </button>
      </div>
      <div className="min-h-0 flex-1">
        {isLoading ? (
          <div className="grid h-full place-items-center text-sm text-[var(--color-mist)]"><Spinner size={16} /></div>
        ) : data?.binary ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-[var(--color-mist)]">
            Binary file ({fmtSize(data.size)}) — not editable here.
          </div>
        ) : (
          <textarea
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            readOnly={truncated}
            spellCheck={false}
            className={`h-full min-h-[14rem] w-full resize-none bg-[var(--color-ink-950)] p-3 font-mono text-[12px] leading-relaxed outline-none ${truncated ? "opacity-70" : ""}`}
          />
        )}
      </div>
      {truncated && (
        <div className="border-t border-[var(--color-line)] px-3 py-1.5 text-xs text-[var(--color-warn)]">
          Showing the first {fmtSize(data!.size > 1_000_000 ? 1_000_000 : data!.size)} of {fmtSize(data!.size)} — too large to edit in-browser.
        </div>
      )}
      {save.isError && (
        <div className="border-t border-[var(--color-line)] px-3 py-1.5 text-xs text-[var(--color-bad)]">
          {(save.error as Error).message}
        </div>
      )}
    </div>
  );
}

function AgentChat({ ws, selectedPath }: { ws: string; selectedPath: string | null }) {
  const { data: wfData } = useWorkflows();
  const { data: doctor } = useDoctor(ws);
  const doctorLoaded = doctor !== undefined;
  const isShell = doctor?.active_provider === "shell";
  const saveIdea = useSaveIdea(ws);
  const start = useStartTask();
  const cancel = useCancelTask();

  const [message, setMessage] = useState("");
  const [workflow, setWorkflow] = useState("engineering");
  const [turns, setTurns] = useState<ChatTurn[]>([]);
  const [activeStream, setActiveStream] = useState<{ taskId: string; turnId: string } | null>(null);
  const [sending, setSending] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  const workflows = wfData?.workflows ?? [];
  const running = !!activeStream;

  useEffect(() => {
    if (scrollRef.current) scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
  }, [turns]);

  // Stream the launched task's stdout into the matching agent turn.
  useEffect(() => {
    if (!activeStream) return;
    const { taskId, turnId } = activeStream;
    const es = new EventSource(`/api/research/${taskId}/stream`);
    es.onmessage = (ev) => {
      try {
        const obj = JSON.parse(ev.data);
        if (obj.line !== undefined) {
          setTurns((prev) =>
            prev.map((t) => (t.id === turnId ? { ...t, text: t.text ? `${t.text}\n${obj.line}` : obj.line } : t)),
          );
        }
        if (obj.status) {
          setTurns((prev) => prev.map((t) => (t.id === turnId ? { ...t, status: obj.status } : t)));
          es.close();
          setActiveStream(null);
        }
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => {
      es.close();
      setActiveStream(null);
      setTurns((prev) => prev.map((t) => (t.id === turnId ? { ...t, status: t.status ?? "failed" } : t)));
    };
    return () => es.close();
  }, [activeStream]);

  async function send() {
    const text = message.trim();
    if (!text || sending || running || isShell || !doctorLoaded) return;
    setSending(true);
    const userId = crypto.randomUUID();
    const agentId = crypto.randomUUID();
    setTurns((prev) => [
      ...prev,
      { id: userId, role: "user", text },
      { id: agentId, role: "agent", text: "", status: "running" },
    ]);
    setMessage("");

    const statement = selectedPath
      ? `${text}\n\n(Context: this request concerns the file \`${selectedPath}\` in the workspace.)`
      : text;

    try {
      await saveIdea.mutateAsync({ statement });
      const res = await start.mutateAsync({ command: "run", workspace_id: ws, workflow });
      setActiveStream({ taskId: res.task_id, turnId: agentId });
    } catch (e) {
      setTurns((prev) =>
        prev.map((t) => (t.id === agentId ? { ...t, status: "failed", text: (e as Error).message } : t)),
      );
    } finally {
      setSending(false);
    }
  }

  function stop() {
    if (activeStream) cancel.mutate(activeStream.taskId);
  }

  return (
    <div className="card flex min-h-0 flex-1 flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2">
        <Bot size={15} className="text-[var(--color-iris)]" />
        <span className="text-sm font-semibold">Follow up with the agents</span>
        {running && <Badge tone="warn"><Spinner size={10} /> working</Badge>}
        <select
          value={workflow}
          onChange={(e) => setWorkflow(e.target.value)}
          className="ml-auto rounded-lg border border-[var(--color-line)] bg-[var(--color-ink-900)] px-2 py-1 text-xs outline-none focus:border-[var(--color-brand)]"
        >
          {workflows.length === 0 && <option value="engineering">engineering</option>}
          {workflows.map((w) => (
            <option key={w.name} value={w.name}>{w.name}</option>
          ))}
        </select>
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 space-y-3 overflow-auto p-3">
        {turns.length === 0 ? (
          <div className="grid h-full place-items-center px-6 text-center text-sm text-[var(--color-mist)]">
            Ask the agents to change, extend, or explain {selectedPath ? <span className="font-mono">{selectedPath}</span> : "your files"}.
            Each message seeds a workflow run and streams the result here.
          </div>
        ) : (
          turns.map((t) => <ChatBubble key={t.id} turn={t} />)
        )}
      </div>

      <div className="border-t border-[var(--color-line)] p-2.5">
        {isShell && (
          <div className="mb-1.5 text-[11px] text-[var(--color-warn)]">
            Active provider is <code>shell</code> (echoes only) — pick a real provider in Configure → Providers to chat with the agents.
          </div>
        )}
        {selectedPath && (
          <div className="mb-1.5 flex items-center gap-1 text-[11px] text-[var(--color-mist)]">
            <FileText size={11} /> context: <span className="font-mono text-[var(--color-brand-bright)]">{selectedPath}</span>
          </div>
        )}
        <div className="flex items-end gap-2">
          <textarea
            value={message}
            onChange={(e) => setMessage(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) {
                e.preventDefault();
                send();
              }
            }}
            rows={2}
            placeholder="e.g. Add error handling to the parser and a test for empty input… (⌘/Ctrl+Enter to send)"
            className="min-w-0 flex-1 resize-y rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-900)] p-2.5 text-sm outline-none focus:border-[var(--color-brand)]"
          />
          {running ? (
            <button
              onClick={stop}
              className="flex items-center gap-1.5 rounded-xl bg-[var(--color-bad)]/90 px-4 py-2.5 text-sm font-semibold text-white hover:bg-[var(--color-bad)]"
            >
              <Square size={14} /> Stop
            </button>
          ) : (
            <button
              onClick={send}
              disabled={!message.trim() || sending || isShell || !doctorLoaded}
              title={
                !doctorLoaded
                  ? "Checking the active provider…"
                  : isShell
                    ? "Active provider is 'shell' — pick a real provider in Configure → Providers"
                    : undefined
              }
              className="flex items-center gap-1.5 rounded-xl bg-gradient-to-r from-[var(--color-brand)] to-[var(--color-iris)] px-4 py-2.5 text-sm font-semibold text-[var(--color-ink-950)] disabled:opacity-40"
            >
              {sending ? <Loader2 size={14} className="animate-spin" /> : <Send size={14} />} Send
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

function ChatBubble({ turn }: { turn: ChatTurn }) {
  const isUser = turn.role === "user";
  return (
    <div className={`flex gap-2.5 ${isUser ? "flex-row-reverse" : ""}`}>
      <div
        className={`grid h-7 w-7 shrink-0 place-items-center rounded-lg ${
          isUser ? "bg-[var(--color-brand)]/20 text-[var(--color-brand-bright)]" : "bg-[var(--color-iris)]/20 text-[var(--color-iris)]"
        }`}
      >
        {isUser ? <User size={14} /> : <Bot size={14} />}
      </div>
      <div className={`min-w-0 max-w-[85%] ${isUser ? "text-right" : ""}`}>
        {!isUser && turn.status && (
          <div className="mb-0.5 text-[11px] text-[var(--color-mist)]">
            {turn.status === "running" ? "agents working…" : `run ${turn.status}`}
          </div>
        )}
        <div
          className={`inline-block whitespace-pre-wrap rounded-xl px-3 py-2 text-left text-[12px] leading-relaxed ${
            isUser
              ? "bg-[var(--color-brand)]/15 text-[var(--color-frost)]"
              : "bg-[var(--color-ink-900)] font-mono text-[var(--color-mist)]"
          }`}
        >
          {turn.text || (turn.status === "running" ? "…" : "")}
        </div>
      </div>
    </div>
  );
}
