import { useEffect, useRef, useState } from "react";
import { Activity, ScrollText } from "lucide-react";
import { useHarnessLogs } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { useSharedActivity } from "../../state/activity";
import { ActivityTimeline } from "../dashboard/ActivityTimeline";
import { PromptInspector } from "../dashboard/PromptInspector";
import type { ActivityEvent } from "../../api/types";

type LogTab = "activity" | "harness";

export function LogView() {
  const { activeId } = useActiveWorkspace();
  const { events, connected } = useSharedActivity();
  const [inspect, setInspect] = useState<ActivityEvent | null>(null);
  const [tab, setTab] = useState<LogTab>("activity");

  return (
    <div className="flex h-full min-h-0 flex-col gap-3">
      <div className="flex items-center gap-1.5">
        <TabButton active={tab === "activity"} onClick={() => setTab("activity")} icon={<Activity size={13} />}>
          Agent activity
        </TabButton>
        <TabButton active={tab === "harness"} onClick={() => setTab("harness")} icon={<ScrollText size={13} />}>
          Harness log
        </TabButton>
      </div>

      {tab === "activity" ? (
        <div className="flex min-h-0 flex-1 flex-col">
          <ActivityTimeline events={events} connected={connected} onInspect={setInspect} />
        </div>
      ) : (
        <HarnessLog wsId={activeId} />
      )}

      {inspect && <PromptInspector event={inspect} onClose={() => setInspect(null)} />}
    </div>
  );
}

function TabButton({
  active,
  onClick,
  icon,
  children,
}: {
  active: boolean;
  onClick: () => void;
  icon: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      className={`flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-xs transition-colors ${
        active
          ? "bg-[var(--color-brand)]/15 font-semibold text-[var(--color-brand-bright)]"
          : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-frost)]"
      }`}
    >
      {icon}
      {children}
    </button>
  );
}

// Raw session logs from .clk/logs/*.log: init progress, casting decisions,
// orchestration messages — everything the CLI writes outside activity.jsonl.
function HarnessLog({ wsId }: { wsId: string | null }) {
  const { data, isLoading } = useHarnessLogs(wsId);
  const ref = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);

  useEffect(() => {
    const el = ref.current;
    if (el && pinnedRef.current) el.scrollTop = el.scrollHeight;
  }, [data?.count]);

  if (!wsId) {
    return <div className="card p-4 text-sm text-[var(--color-mist)]">Pick a workspace to see its harness log.</div>;
  }

  return (
    <div
      ref={ref}
      onScroll={(e) => {
        const el = e.currentTarget;
        pinnedRef.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
      }}
      className="card min-h-0 flex-1 overflow-auto p-3 font-mono text-[11px] leading-relaxed text-[var(--color-mist)]"
    >
      {isLoading ? (
        "Loading…"
      ) : !data || data.lines.length === 0 ? (
        "No harness log lines yet — they appear once a run starts."
      ) : (
        data.lines.map((l, i) => (
          <div key={i} className="whitespace-pre-wrap">
            <span className="text-[var(--color-iris)]/70">[{l.file.replace(/\.log$/, "")}]</span> {l.line}
          </div>
        ))
      )}
    </div>
  );
}
