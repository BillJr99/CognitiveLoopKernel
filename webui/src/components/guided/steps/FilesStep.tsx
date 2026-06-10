// Step 5: present what the team produced, with download and follow-up CTAs.
import { useEffect, useMemo, useState } from "react";
import { Download, FileText, MessageSquarePlus, PanelsTopLeft } from "lucide-react";
import { useFileContent, useWorkspaceFiles } from "../../../api/hooks";
import { Spinner } from "../../common/ui";
import { StepShell } from "../StepShell";

export function FilesStep({
  wsId,
  onFollowUp,
  onAdvanced,
}: {
  wsId: string;
  onFollowUp: () => void;
  onAdvanced: () => void;
}) {
  const { data, isLoading } = useWorkspaceFiles(wsId);
  const files = useMemo(() => data?.files ?? [], [data]);
  const [selected, setSelected] = useState<string | null>(null);
  const { data: content, isLoading: contentLoading } = useFileContent(wsId, selected);

  // Lead with the README when there is one — it's the human-readable summary.
  useEffect(() => {
    if (selected || files.length === 0) return;
    const readme = files.find((f) => /^readme\.md$/i.test(f.path));
    setSelected((readme ?? files[0]).path);
  }, [files, selected]);

  return (
    <StepShell
      step="files"
      title="Here's what your team made"
      subtitle={
        files.length > 0
          ? `${files.length} file${files.length === 1 ? "" : "s"} were created in your project. Click any of them to take a look.`
          : "The run finished. If you expected files, ask the team to go further below."
      }
      wide
    >
      <div className="card-lux flex min-h-[320px] max-h-[52vh] overflow-hidden">
        {/* File list */}
        <div className="flex w-56 shrink-0 flex-col border-r border-[var(--color-line)]">
          <div className="border-b border-[var(--color-line)] px-3 py-2 text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
            Your files
          </div>
          <div className="min-h-0 flex-1 overflow-auto p-1.5">
            {isLoading ? (
              <div className="p-2"><Spinner size={14} /></div>
            ) : files.length === 0 ? (
              <div className="p-3 text-xs text-[var(--color-mist)]">Nothing here yet.</div>
            ) : (
              files.map((f) => (
                <button
                  key={f.path}
                  onClick={() => setSelected(f.path)}
                  className={`flex w-full items-center gap-2 truncate rounded-lg px-2.5 py-1.5 text-left text-xs transition-colors ${
                    selected === f.path
                      ? "bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)]"
                      : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-frost)]"
                  }`}
                  title={f.path}
                >
                  <FileText size={12} className="shrink-0" />
                  <span className="truncate">{f.path}</span>
                </button>
              ))
            )}
          </div>
        </div>

        {/* Preview */}
        <div className="min-w-0 flex-1 overflow-auto p-4">
          {contentLoading ? (
            <Spinner size={16} />
          ) : !selected ? (
            <div className="text-sm text-[var(--color-mist)]">Pick a file on the left to preview it.</div>
          ) : content?.binary ? (
            <div className="text-sm text-[var(--color-mist)]">This is a binary file — download the project to open it.</div>
          ) : (
            <pre className="whitespace-pre-wrap break-words font-mono text-[12px] leading-relaxed text-[var(--color-frost)]/90">
              {content?.content ?? ""}
            </pre>
          )}
        </div>
      </div>

      <div className="flex flex-wrap items-center justify-center gap-3">
        <a
          href={`/api/workspaces/${wsId}/download`}
          download
          aria-disabled={files.length === 0}
          onClick={(e) => files.length === 0 && e.preventDefault()}
          className={`btn btn-ghost ${files.length === 0 ? "pointer-events-none opacity-40" : ""}`}
        >
          <Download size={15} /> Download everything (.zip)
        </a>
        <button onClick={onFollowUp} className="btn btn-primary !px-6">
          <MessageSquarePlus size={16} /> Ask for changes
        </button>
      </div>

      <div className="text-center">
        <button
          onClick={onAdvanced}
          className="inline-flex items-center gap-1.5 text-xs text-[var(--color-mist)] underline-offset-4 transition-colors hover:text-[var(--color-frost)] hover:underline"
        >
          <PanelsTopLeft size={13} /> I'm happy — take me to the full console
        </button>
      </div>
    </StepShell>
  );
}
