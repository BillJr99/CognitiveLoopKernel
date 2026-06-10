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
  Download,
  History,
  GitCommitHorizontal,
  ArrowLeft,
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
  useGitLog,
  useCommitDetail,
  useFileAtCommit,
  useGitStatus,
  useGitWorkingDiff,
} from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import type { FileEntry, GitCommit } from "../../api/types";
import { Badge, EmptyState, Spinner } from "../common/ui";
import { timeAgo } from "../../lib/format";

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

// Sentinel "commit" id for the uncommitted working tree in the History pane.
const WORKING_TREE = "__working_tree__";

export function FilesView() {
  const { activeId } = useActiveWorkspace();
  const { data, isLoading, refetch, isRefetching } = useWorkspaceFiles(activeId);
  const [selected, setSelected] = useState<string | null>(null);
  const [pane, setPane] = useState<"files" | "history">("files");
  const [selectedCommit, setSelectedCommit] = useState<string | null>(null);
  const gitLog = useGitLog(pane === "history" ? activeId : null);
  const { data: gitStatus } = useGitStatus(activeId);

  const files = useMemo(() => data?.files ?? [], [data]);
  const commits = gitLog.data?.commits ?? [];
  const dirtyPaths = useMemo(
    () => new Set((gitStatus?.files ?? []).map((f) => f.path)),
    [gitStatus],
  );

  if (!activeId) {
    return <EmptyState icon={<FolderOpen size={22} />} title="No workspace selected" hint="Pick a workspace to browse the files your agents create." />;
  }

  return (
    <div className="flex h-full min-h-0 gap-3">
      {/* File list / commit list */}
      <div className="card flex w-64 shrink-0 flex-col">
        <div className="flex items-center gap-1 border-b border-[var(--color-line)] px-2 py-2">
          <PaneTab
            active={pane === "files"}
            onClick={() => setPane("files")}
            icon={<FileText size={13} />}
            label="Files"
            count={files.length}
          />
          <PaneTab
            active={pane === "history"}
            onClick={() => setPane("history")}
            icon={<History size={13} />}
            label="History"
            count={pane === "history" ? commits.length : undefined}
          />
          <a
            href={`/api/workspaces/${activeId}/download`}
            download
            aria-disabled={files.length === 0}
            tabIndex={files.length === 0 ? -1 : 0}
            onClick={(e) => {
              if (files.length === 0) e.preventDefault();
            }}
            title="Download the workspace as a .zip"
            className={`ml-auto rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)] ${
              files.length === 0 ? "pointer-events-none opacity-40" : ""
            }`}
          >
            <Download size={14} />
          </a>
          <button
            onClick={() => (pane === "files" ? refetch() : gitLog.refetch())}
            title="Refresh"
            className="rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
          >
            <RefreshCw size={14} className={isRefetching || gitLog.isRefetching ? "animate-spin" : ""} />
          </button>
        </div>
        <div className="min-h-0 flex-1 overflow-auto p-1.5">
          {pane === "files" ? (
            isLoading ? (
              <div className="px-2 py-2 text-sm text-[var(--color-mist)]"><Spinner size={14} /></div>
            ) : files.length === 0 ? (
              <div className="px-2 py-2 text-xs text-[var(--color-mist)]">
                No files yet. Ask the agents to build something on the Run tab.
              </div>
            ) : (
              <ul className="flex flex-col">
                {files.map((f) => (
                  <FileRow
                    key={f.path}
                    file={f}
                    active={f.path === selected}
                    dirty={dirtyPaths.has(f.path)}
                    onClick={() => setSelected(f.path)}
                  />
                ))}
              </ul>
            )
          ) : gitLog.isLoading ? (
            <div className="px-2 py-2 text-sm text-[var(--color-mist)]"><Spinner size={14} /></div>
          ) : commits.length === 0 && !gitStatus?.dirty ? (
            <div className="px-2 py-2 text-xs text-[var(--color-mist)]">
              No commits yet. Each time the agents finish a unit of work, the
              harness commits it and it shows up here.
            </div>
          ) : (
            <ul className="flex flex-col gap-0.5">
              {gitStatus?.dirty && (
                <WorkingTreeRow
                  count={gitStatus.count}
                  active={selectedCommit === WORKING_TREE}
                  onClick={() =>
                    setSelectedCommit(selectedCommit === WORKING_TREE ? null : WORKING_TREE)
                  }
                />
              )}
              {commits.map((c) => (
                <CommitRow
                  key={c.sha}
                  commit={c}
                  active={c.sha === selectedCommit}
                  onClick={() => setSelectedCommit(c.sha === selectedCommit ? null : c.sha)}
                />
              ))}
            </ul>
          )}
        </div>
        {pane === "files" && data?.truncated && (
          <div className="border-t border-[var(--color-line)] px-3 py-1.5 text-[11px] text-[var(--color-warn)]">
            Showing first {files.length} files.
          </div>
        )}
      </div>

      {/* Editor or commit detail + chat */}
      <div className="flex min-w-0 flex-1 flex-col gap-3">
        {pane === "history" && selectedCommit === WORKING_TREE ? (
          <WorkingTreePanel ws={activeId} onClose={() => setSelectedCommit(null)} />
        ) : pane === "history" && selectedCommit ? (
          <CommitDetailPanel ws={activeId} sha={selectedCommit} onClose={() => setSelectedCommit(null)} />
        ) : (
          <FileEditor ws={activeId} path={selected} />
        )}
        <AgentChat ws={activeId} selectedPath={selected} />
      </div>
    </div>
  );
}

function WorkingTreeRow({ count, active, onClick }: { count: number; active: boolean; onClick: () => void }) {
  return (
    <li>
      <button
        onClick={onClick}
        className={`flex w-full flex-col gap-0.5 rounded-lg px-2 py-1.5 text-left transition-colors ${
          active
            ? "bg-[var(--color-ink-800)] ring-1 ring-[var(--color-warn)]/40"
            : "hover:bg-[var(--color-ink-800)]"
        }`}
      >
        <span className="flex items-center gap-1.5">
          <span className="h-2 w-2 shrink-0 animate-pulse rounded-full bg-[var(--color-warn)]" />
          <span className="min-w-0 truncate text-xs font-semibold text-[var(--color-warn)]">
            Uncommitted changes
          </span>
        </span>
        <span className="pl-[14px] text-[10px] text-[var(--color-mist)]">
          {count} file{count === 1 ? "" : "s"} not yet in a commit
        </span>
      </button>
    </li>
  );
}

function WorkingTreePanel({ ws, onClose }: { ws: string; onClose: () => void }) {
  const { data: status } = useGitStatus(ws);
  const { data: diff, isLoading } = useGitWorkingDiff(ws);
  const files = status?.files ?? [];

  return (
    <div className="card flex min-h-0 flex-[2] flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2">
        <button
          onClick={onClose}
          title="Back to the file editor"
          className="rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
        >
          <ArrowLeft size={14} />
        </button>
        <span className="h-2 w-2 shrink-0 rounded-full bg-[var(--color-warn)]" />
        <span className="text-sm font-semibold">Uncommitted changes</span>
        <span className="ml-auto text-[11px] text-[var(--color-mist)]">
          committed automatically as the agents finish each batch of work
        </span>
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {files.length > 0 && (
          <div className="border-b border-[var(--color-line)] px-3 py-2">
            <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
              {files.length} file{files.length === 1 ? "" : "s"} changed since the last commit
            </div>
            <ul className="flex flex-col gap-0.5">
              {files.map((f) => (
                <li key={f.path} className="flex items-center gap-2 text-xs">
                  <FileText size={11} className="shrink-0 opacity-60" />
                  <span className="min-w-0 truncate font-mono" title={f.path}>{f.path}</span>
                  <span
                    className={`ml-auto shrink-0 rounded px-1 text-[10px] font-semibold ${
                      f.state === "new"
                        ? "bg-[var(--color-good)]/15 text-[var(--color-good)]"
                        : f.state === "deleted"
                          ? "bg-[var(--color-bad)]/15 text-[var(--color-bad)]"
                          : "bg-[var(--color-warn)]/15 text-[var(--color-warn)]"
                    }`}
                  >
                    {f.state}
                  </span>
                </li>
              ))}
            </ul>
          </div>
        )}
        {isLoading ? (
          <div className="grid place-items-center p-6 text-sm text-[var(--color-mist)]"><Spinner size={16} /></div>
        ) : (
          <>
            <DiffBlock patch={diff?.patch ?? ""} />
            {!diff?.patch?.trim() && files.some((f) => f.state === "new") && (
              <div className="px-3 pb-3 text-[11px] text-[var(--color-mist)]">
                New files don't show in the diff until their first commit — open them
                from the Files list to see their content.
              </div>
            )}
            {diff?.truncated && (
              <div className="px-3 py-1.5 text-[11px] text-[var(--color-warn)]">
                Diff truncated — too large to show in full.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function PaneTab({
  active, onClick, icon, label, count,
}: {
  active: boolean; onClick: () => void; icon: React.ReactNode; label: string; count?: number;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg px-2 py-1 text-xs font-semibold transition-colors ${
        active
          ? "bg-[var(--color-ink-800)] text-[var(--color-brand-bright)]"
          : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
      }`}
    >
      {icon} {label}
      {count !== undefined && <span className="text-[10px] font-normal opacity-70">{count}</span>}
    </button>
  );
}

/** "[engineer] Implement parser" → { agent: "engineer", title: "Implement parser" } */
function splitSubject(subject: string): { agent: string | null; title: string } {
  const m = subject.match(/^\[([\w-]+)\]\s*(.*)$/);
  return m ? { agent: m[1], title: m[2] || subject } : { agent: null, title: subject };
}

function CommitRow({ commit, active, onClick }: { commit: GitCommit; active: boolean; onClick: () => void }) {
  const { agent, title } = splitSubject(commit.subject);
  return (
    <li>
      <button
        onClick={onClick}
        className={`flex w-full flex-col gap-0.5 rounded-lg px-2 py-1.5 text-left transition-colors ${
          active
            ? "bg-[var(--color-ink-800)] ring-1 ring-[var(--color-brand)]/40"
            : "hover:bg-[var(--color-ink-800)]"
        }`}
      >
        <span className="flex items-center gap-1.5">
          <GitCommitHorizontal size={12} className="shrink-0 text-[var(--color-brand)]" />
          {agent && (
            <span className="shrink-0 rounded bg-[var(--color-iris)]/20 px-1 text-[10px] font-semibold text-[var(--color-iris)]">
              {agent}
            </span>
          )}
          <span className="min-w-0 truncate text-xs text-[var(--color-frost)]" title={title}>{title}</span>
        </span>
        <span className="flex items-center gap-2 pl-[18px] text-[10px] text-[var(--color-mist)]">
          <span>{timeAgo(commit.date)}</span>
          <span className="font-mono opacity-60">{commit.short}</span>
          {commit.insertions > 0 && <span className="text-[var(--color-good)]">+{commit.insertions}</span>}
          {commit.deletions > 0 && <span className="text-[var(--color-bad)]">−{commit.deletions}</span>}
        </span>
      </button>
    </li>
  );
}

function CommitDetailPanel({ ws, sha, onClose }: { ws: string; sha: string; onClose: () => void }) {
  const { data, isLoading } = useCommitDetail(ws, sha);
  const commit = data?.commit;
  const { agent, title } = splitSubject(commit?.subject ?? "");

  return (
    <div className="card flex min-h-0 flex-[2] flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2">
        <button
          onClick={onClose}
          title="Back to the file editor"
          className="rounded-lg p-1 text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
        >
          <ArrowLeft size={14} />
        </button>
        <GitCommitHorizontal size={14} className="text-[var(--color-brand)]" />
        {agent && (
          <span className="rounded bg-[var(--color-iris)]/20 px-1.5 py-0.5 text-[10px] font-semibold text-[var(--color-iris)]">
            {agent}
          </span>
        )}
        <span className="min-w-0 truncate text-sm font-semibold" title={title}>{title || sha.slice(0, 12)}</span>
        {commit && (
          <span className="ml-auto shrink-0 text-[11px] text-[var(--color-mist)]">
            {timeAgo(commit.date)} · <span className="font-mono">{commit.short}</span>
          </span>
        )}
      </div>
      <div className="min-h-0 flex-1 overflow-auto">
        {isLoading ? (
          <div className="grid h-full place-items-center text-sm text-[var(--color-mist)]"><Spinner size={16} /></div>
        ) : (
          <>
            {commit && commit.files.length > 0 && (
              <div className="border-b border-[var(--color-line)] px-3 py-2">
                <div className="mb-1 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
                  {commit.files.length} file{commit.files.length === 1 ? "" : "s"} changed
                </div>
                <ul className="flex flex-col gap-0.5">
                  {commit.files.map((f) => (
                    <li key={f.path} className="flex items-center gap-2 text-xs">
                      <FileText size={11} className="shrink-0 opacity-60" />
                      <span className="min-w-0 truncate font-mono" title={f.path}>{f.path}</span>
                      <span className="ml-auto shrink-0 tabular-nums">
                        {f.insertions > 0 && <span className="text-[var(--color-good)]">+{f.insertions}</span>}{" "}
                        {f.deletions > 0 && <span className="text-[var(--color-bad)]">−{f.deletions}</span>}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            )}
            <DiffBlock patch={data?.patch ?? ""} />
            {data?.patch_truncated && (
              <div className="px-3 py-1.5 text-[11px] text-[var(--color-warn)]">
                Diff truncated — too large to show in full.
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}

function DiffBlock({ patch }: { patch: string }) {
  if (!patch.trim()) {
    return <div className="px-3 py-3 text-xs text-[var(--color-mist)]">No visible changes in this commit.</div>;
  }
  return (
    <pre className="overflow-x-auto p-3 font-mono text-[11px] leading-relaxed">
      {patch.split("\n").map((line, i) => {
        let cls = "text-[var(--color-mist)]";
        if (line.startsWith("diff --git") || line.startsWith("index ") || line.startsWith("--- ") || line.startsWith("+++ ")) {
          cls = "text-[var(--color-frost)] font-semibold";
        } else if (line.startsWith("@@")) {
          cls = "text-[var(--color-iris)]";
        } else if (line.startsWith("+")) {
          cls = "text-[var(--color-good)] bg-[rgba(74,222,128,0.06)]";
        } else if (line.startsWith("-")) {
          cls = "text-[var(--color-bad)] bg-[rgba(248,113,113,0.06)]";
        }
        return (
          <div key={i} className={cls}>{line || " "}</div>
        );
      })}
    </pre>
  );
}

function FileRow({
  file, active, dirty, onClick,
}: {
  file: FileEntry; active: boolean; dirty?: boolean; onClick: () => void;
}) {
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
        {dirty && (
          <span
            title="Changed since the last commit"
            className="h-1.5 w-1.5 shrink-0 rounded-full bg-[var(--color-warn)]"
          />
        )}
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

  // Per-file time travel: pick a past commit to view that version read-only.
  const [showVersions, setShowVersions] = useState(false);
  const [versionSha, setVersionSha] = useState<string | null>(null);
  const fileLog = useGitLog(showVersions || versionSha ? ws : null, path);
  const versioned = useFileAtCommit(ws, versionSha, path);
  const versionMeta = useMemo(
    () => (fileLog.data?.commits ?? []).find((c) => c.sha === versionSha) ?? null,
    [fileLog.data, versionSha],
  );

  // Reset the draft whenever a different file's content arrives.
  useEffect(() => {
    if (data && !data.binary && data.path === path && loadedFor.current !== path) {
      setDraft(data.content ?? "");
      loadedFor.current = path;
    }
    if (!path) loadedFor.current = null;
  }, [data, path]);

  // Leave time travel when the user switches files.
  useEffect(() => {
    setVersionSha(null);
    setShowVersions(false);
  }, [path]);

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
  const viewingPast = !!versionSha;

  return (
    <div className="card flex min-h-0 flex-[2] flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-3 py-2">
        <FileText size={14} className="text-[var(--color-brand)]" />
        <span className="truncate font-mono text-xs">{path}</span>
        {viewingPast && versionMeta && (
          <Badge tone="warn">version from {timeAgo(versionMeta.date)}</Badge>
        )}
        {!viewingPast && truncated && <Badge tone="warn">truncated · read-only</Badge>}
        {!viewingPast && dirty && <Badge tone="brand">unsaved</Badge>}
        <button
          onClick={() => setShowVersions((v) => !v)}
          title="Browse this file's past versions"
          className={`ml-auto rounded-lg p-1 transition-colors ${
            showVersions || viewingPast
              ? "bg-[var(--color-ink-800)] text-[var(--color-brand-bright)]"
              : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-brand-bright)]"
          }`}
        >
          <History size={14} />
        </button>
        <button
          onClick={() => path && dirty && save.mutate({ path, content: draft })}
          disabled={!dirty || save.isPending || viewingPast}
          title={truncated ? "File too large to edit safely in-browser" : undefined}
          className="flex items-center gap-1.5 rounded-lg bg-[var(--color-brand)] px-3 py-1 text-xs font-semibold text-[var(--color-ink-950)] disabled:opacity-40"
        >
          {save.isPending ? <Spinner size={12} /> : <Save size={13} />} Save
        </button>
      </div>
      {showVersions && (
        <div className="max-h-40 overflow-auto border-b border-[var(--color-line)] px-2 py-1.5">
          {fileLog.isLoading ? (
            <div className="px-1 py-1 text-xs text-[var(--color-mist)]"><Spinner size={12} /></div>
          ) : (fileLog.data?.commits ?? []).length === 0 ? (
            <div className="px-1 py-1 text-xs text-[var(--color-mist)]">No committed versions of this file yet.</div>
          ) : (
            <ul className="flex flex-col gap-0.5">
              <li>
                <button
                  onClick={() => { setVersionSha(null); setShowVersions(false); }}
                  className={`flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left text-xs transition-colors ${
                    !versionSha ? "bg-[var(--color-ink-800)] text-[var(--color-brand-bright)]" : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
                  }`}
                >
                  <FileText size={11} /> Latest (editable)
                </button>
              </li>
              {(fileLog.data?.commits ?? []).map((c) => {
                const { agent, title } = splitSubject(c.subject);
                return (
                  <li key={c.sha}>
                    <button
                      onClick={() => { setVersionSha(c.sha); setShowVersions(false); }}
                      className={`flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left text-xs transition-colors ${
                        c.sha === versionSha ? "bg-[var(--color-ink-800)] text-[var(--color-brand-bright)]" : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
                      }`}
                    >
                      <GitCommitHorizontal size={11} className="shrink-0 text-[var(--color-brand)]" />
                      {agent && (
                        <span className="shrink-0 rounded bg-[var(--color-iris)]/20 px-1 text-[10px] font-semibold text-[var(--color-iris)]">{agent}</span>
                      )}
                      <span className="min-w-0 flex-1 truncate" title={title}>{title}</span>
                      <span className="shrink-0 text-[10px] opacity-70">{timeAgo(c.date)}</span>
                      <span className="shrink-0 font-mono text-[10px] opacity-50">{c.short}</span>
                    </button>
                  </li>
                );
              })}
            </ul>
          )}
        </div>
      )}
      {viewingPast && (
        <div className="flex items-center gap-2 border-b border-[var(--color-line)] bg-[var(--color-ink-900)] px-3 py-1.5 text-xs text-[var(--color-warn)]">
          <History size={12} />
          Read-only view of {versionMeta ? `the version committed ${timeAgo(versionMeta.date)}` : `commit ${versionSha?.slice(0, 12)}`}.
          <button
            onClick={() => setVersionSha(null)}
            className="ml-auto flex items-center gap-1 rounded-lg px-2 py-0.5 font-semibold text-[var(--color-brand-bright)] hover:bg-[var(--color-ink-800)]"
          >
            <ArrowLeft size={11} /> Back to latest
          </button>
        </div>
      )}
      <div className="min-h-0 flex-1">
        {viewingPast ? (
          versioned.isLoading ? (
            <div className="grid h-full place-items-center text-sm text-[var(--color-mist)]"><Spinner size={16} /></div>
          ) : versioned.data?.binary ? (
            <div className="grid h-full place-items-center px-6 text-center text-sm text-[var(--color-mist)]">
              Binary file ({fmtSize(versioned.data.size)}) at this version — not viewable here.
            </div>
          ) : versioned.isError ? (
            <div className="grid h-full place-items-center px-6 text-center text-sm text-[var(--color-bad)]">
              {(versioned.error as Error).message}
            </div>
          ) : (
            <textarea
              value={versioned.data?.content ?? ""}
              readOnly
              spellCheck={false}
              className="h-full min-h-[14rem] w-full resize-none bg-[var(--color-ink-950)] p-3 font-mono text-[12px] leading-relaxed opacity-80 outline-none"
            />
          )
        ) : isLoading ? (
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
      {!viewingPast && truncated && (
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
        {!doctorLoaded && (
          <div className="mb-1.5 flex items-center gap-1 text-[11px] text-[var(--color-mist)]">
            <Loader2 size={11} className="animate-spin" /> checking the active provider…
          </div>
        )}
        {doctorLoaded && isShell && (
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
            className="input min-w-0 flex-1 resize-y !p-2.5"
          />
          {running ? (
            <button
              onClick={stop}
              className="btn btn-danger !py-2.5"
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
              className="btn btn-primary !py-2.5"
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
