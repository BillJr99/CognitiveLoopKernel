// Step 4: friendly live progress while the agents work.
import { useEffect, useMemo, useRef, useState } from "react";
import { Check, ChevronDown, ChevronRight, FileText, RefreshCw, Square, Terminal } from "lucide-react";
import { useCancelTask, useSnapshot } from "../../../api/hooks";
import { useSharedActivity } from "../../../state/activity";
import { useStuckWatchdog } from "../../../hooks/useStuckWatchdog";
import { Spinner } from "../../common/ui";
import { StepShell } from "../StepShell";
import { STAGES, friendlyEvent, friendlyRole, stageFor } from "../friendly";
import type { GuidedPipeline } from "../friendly";

const AVATAR_HUES = [
  "from-sky-400 to-blue-500",
  "from-violet-400 to-purple-500",
  "from-emerald-400 to-teal-500",
  "from-amber-400 to-orange-500",
  "from-pink-400 to-rose-500",
  "from-cyan-400 to-sky-500",
];

function hueFor(name: string): string {
  let h = 0;
  for (const c of name) h = (h * 31 + c.charCodeAt(0)) % AVATAR_HUES.length;
  return AVATAR_HUES[h];
}

export function WorkingStep({
  wsId,
  taskId,
  pipeline,
  failedMessage,
  onRetry,
  onBack,
  onNudged,
}: {
  wsId: string;
  taskId: string | null;
  pipeline: GuidedPipeline;
  failedMessage: string | null;
  onRetry: () => void;
  onBack: () => void;
  onNudged?: (newTaskId: string) => void;
}) {
  const { data: snapData } = useSnapshot(wsId);
  const { events, connected } = useSharedActivity();
  const cancel = useCancelTask();
  const lastSeq = events.length > 0 ? events[events.length - 1].seq : undefined;
  const { healing } = useStuckWatchdog({
    wsId,
    busy: snapData?.snapshot?.busy ?? false,
    connected,
    lastSeq,
    onNudged,
  });
  const [showLog, setShowLog] = useState(false);

  const snapshot = snapData?.snapshot;
  const agents = useMemo(() => Object.values(snapshot?.agents ?? {}), [snapshot]);
  const stage = stageFor(pipeline, agents.length);
  const filesCount = snapshot?.files_changed?.length ?? 0;

  // Latest plain-English line (skip internal noise). Before any events
  // arrive the workspace is initializing (first run creates a virtualenv
  // and installs tools — ~30-60 s), so reassure rather than sit silent.
  const nowLine = useMemo(() => {
    for (let i = events.length - 1; i >= 0; i--) {
      const line = friendlyEvent(events[i]);
      if (line) return line;
    }
    return "Setting up your project — the first run installs a few tools and can take a minute…";
  }, [events]);

  return (
    <StepShell
      step="working"
      title={failedMessage ? "Something went wrong" : stage.headline}
      subtitle={failedMessage ?? stage.detail}
      wide
    >
      {failedMessage ? (
        <div className="flex justify-center gap-3">
          <button onClick={onBack} className="btn btn-ghost">Change my question</button>
          <button onClick={onRetry} className="btn btn-primary">Try again</button>
        </div>
      ) : (
        <>
          {/* Stage tracker */}
          <div className="card-lux flex flex-col gap-5 p-6">
            <div className="flex items-center justify-center gap-2 sm:gap-4">
              {STAGES.map((label, i) => {
                const done = stage.index > i;
                const active = stage.index === i;
                return (
                  <div key={label} className="flex items-center gap-2 sm:gap-4">
                    {i > 0 && <div className={`h-px w-6 sm:w-12 ${done || active ? "bg-[var(--color-brand)]/50" : "bg-[var(--color-line)]"}`} />}
                    <div className="flex items-center gap-2">
                      <div
                        className={`grid h-8 w-8 place-items-center rounded-full ${
                          done
                            ? "bg-[var(--color-good)]/20 text-[var(--color-good)]"
                            : active
                              ? "progress-glow bg-gradient-to-br from-[var(--color-brand)] to-[var(--color-iris)] text-[var(--color-ink-950)]"
                              : "bg-[var(--color-ink-800)] text-[var(--color-mist)]"
                        }`}
                      >
                        {done ? <Check size={15} /> : active ? <Spinner size={14} /> : <span className="text-xs">{i + 1}</span>}
                      </div>
                      <span
                        className={`hidden text-xs sm:block ${
                          active ? "font-semibold text-[var(--color-frost)]" : "text-[var(--color-mist)]"
                        }`}
                      >
                        {label}
                      </span>
                    </div>
                  </div>
                );
              })}
            </div>

            {/* Now happening */}
            <div className="relative overflow-hidden rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-950)]/50 px-4 py-3">
              <div className="activity-sweep pointer-events-none absolute inset-x-0 top-0 h-px" />
              <div className="flex items-center gap-2.5 text-sm">
                {healing ? (
                  <RefreshCw size={13} className="shrink-0 animate-spin text-[var(--color-warn)]" />
                ) : (
                  <span className="live-dot h-2 w-2 shrink-0 rounded-full bg-[var(--color-brand)]" />
                )}
                <span key={healing ? "healing" : nowLine} className="fade-up min-w-0 truncate text-[var(--color-frost)]">
                  {healing ? "Auto-healing stalled agent…" : nowLine}
                </span>
                {filesCount > 0 && (
                  <span className="ml-auto flex shrink-0 items-center gap-1 text-[11px] text-[var(--color-good)]">
                    <FileText size={12} /> {filesCount} file{filesCount === 1 ? "" : "s"} so far
                  </span>
                )}
              </div>
            </div>

            {/* Team chips */}
            {agents.length > 0 && (
              <div className="flex flex-wrap items-center justify-center gap-2">
                {agents.map((a) => (
                  <div
                    key={a.name}
                    className={`flex items-center gap-2 rounded-full border px-2.5 py-1 text-[11px] transition-all ${
                      a.status === "working"
                        ? "working-ring border-[var(--color-brand)]/40 bg-[var(--color-brand)]/10 text-[var(--color-frost)]"
                        : a.status === "failed"
                          ? "border-[var(--color-bad)]/40 bg-[var(--color-bad)]/10 text-[var(--color-mist)]"
                          : "border-[var(--color-line)] bg-[var(--color-ink-800)]/60 text-[var(--color-mist)]"
                    }`}
                    title={a.role}
                  >
                    <span
                      className={`grid h-5 w-5 place-items-center rounded-full bg-gradient-to-br text-[9px] font-bold text-[var(--color-ink-950)] ${hueFor(a.name)}`}
                    >
                      {a.name.slice(0, 2).toUpperCase()}
                    </span>
                    <span className="font-semibold">{a.name}</span>
                    <span className="opacity-70">· {friendlyRole(a.name, a.role)}</span>
                  </div>
                ))}
              </div>
            )}
          </div>

          {/* Technical log (collapsed by default) */}
          <div className="card overflow-hidden">
            <button
              onClick={() => setShowLog((s) => !s)}
              className="flex w-full items-center gap-2 px-4 py-2.5 text-left text-xs text-[var(--color-mist)] transition-colors hover:text-[var(--color-frost)]"
            >
              {showLog ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
              <Terminal size={13} />
              Show the technical log
            </button>
            {showLog && taskId && <TaskLog taskId={taskId} />}
          </div>

          <div className="flex justify-center">
            <button
              onClick={() => taskId && cancel.mutate(taskId)}
              disabled={!taskId || cancel.isPending}
              className="btn btn-danger !py-1.5 text-xs"
            >
              <Square size={13} /> Stop
            </button>
          </div>
        </>
      )}
    </StepShell>
  );
}

function TaskLog({ taskId }: { taskId: string }) {
  const [lines, setLines] = useState<string[]>([]);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    setLines([]);
    const es = new EventSource(`/api/research/${taskId}/stream`);
    es.onmessage = (ev) => {
      try {
        const obj = JSON.parse(ev.data);
        if (obj.line !== undefined) setLines((p) => [...p, obj.line]);
        if (obj.status) es.close();
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [taskId]);

  useEffect(() => {
    if (ref.current) ref.current.scrollTop = ref.current.scrollHeight;
  }, [lines.length]);

  return (
    <div ref={ref} className="max-h-64 overflow-auto border-t border-[var(--color-line)] p-3 font-mono text-[11px] leading-relaxed text-[var(--color-mist)]">
      {lines.length === 0 ? "Waiting for output…" : lines.map((l, i) => <div key={i} className="whitespace-pre-wrap">{l}</div>)}
    </div>
  );
}
