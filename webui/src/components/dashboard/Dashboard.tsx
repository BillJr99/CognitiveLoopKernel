import { useState } from "react";
import { FileCode2, Lightbulb, Users } from "lucide-react";
import { useSnapshot } from "../../api/hooks";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { useSharedActivity } from "../../state/activity";
import { useUiMode } from "../../state/uiMode";
import { useStuckWatchdog } from "../../hooks/useStuckWatchdog";
import type { ActivityEvent } from "../../api/types";
import { AgentCard } from "./AgentCard";
import { ActivityTimeline } from "./ActivityTimeline";
import { TokenCostMeters } from "./TokenCostMeters";
import { PromptInspector } from "./PromptInspector";
import { NowHappening } from "./NowHappening";
import { EmptyState } from "../common/ui";

export function Dashboard() {
  const { activeId } = useActiveWorkspace();
  const { data } = useSnapshot(activeId);
  const { events, connected } = useSharedActivity();
  const { setMode } = useUiMode();
  const [inspect, setInspect] = useState<ActivityEvent | null>(null);

  const snap = data?.snapshot;
  const agents = snap ? Object.values(snap.agents) : [];
  const peak = snap?.totals.peak_run_tokens ?? 0;
  const lastSeq = events.length > 0 ? events[events.length - 1].seq : undefined;
  const { healing } = useStuckWatchdog(activeId, snap?.busy ?? false, connected, lastSeq);

  return (
    <div className="grid h-full min-h-0 grid-cols-1 gap-4 xl:grid-cols-3">
      {/* Left/main column */}
      <div className="flex min-h-0 flex-col gap-4 xl:col-span-2">
        <NowHappening snap={snap} latest={events[events.length - 1]} healing={healing} />
        {snap?.idea && (
          <div className="card flex items-start gap-2 p-3">
            <Lightbulb size={16} className="mt-0.5 shrink-0 text-[var(--color-warn)]" />
            <div>
              <div className="text-[11px] uppercase tracking-wide text-[var(--color-mist)]">Current idea</div>
              <div className="text-sm">{snap.idea}</div>
            </div>
          </div>
        )}

        <div>
          <SectionHeader icon={<Users size={15} />} title={`Agents (${agents.length})`} />
          {agents.length === 0 ? (
            <EmptyState
              title="No agents have run yet"
              hint="Head to the Run tab, set an idea, and start a workflow to watch the team assemble."
              action={
                <button onClick={() => setMode("guided")} className="btn btn-ghost text-xs">
                  Or let Guided mode walk you through it
                </button>
              }
            />
          ) : (
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              {agents.map((a, i) => (
                <div key={a.name} style={{ animationDelay: `${Math.min(i * 60, 360)}ms` }} className="float-in">
                  <AgentCard card={a} peak={peak} />
                </div>
              ))}
            </div>
          )}
        </div>

        {snap && <TokenCostMeters snap={snap} />}

        {snap && snap.files_changed.length > 0 && (
          <div className="card p-4">
            <SectionHeader icon={<FileCode2 size={15} />} title={`Files changed (${snap.files_changed.length})`} />
            <div className="mt-2 flex flex-wrap gap-1.5">
              {snap.files_changed.map((f) => (
                <span key={f} className="rounded-md bg-[var(--color-ink-900)]/60 px-2 py-1 font-mono text-[11px] text-[var(--color-mist)]">
                  {f}
                </span>
              ))}
            </div>
          </div>
        )}
      </div>

      {/* Right column: live timeline */}
      <div className="flex min-h-0 flex-col xl:h-[calc(100vh-9rem)]">
        <ActivityTimeline events={events} connected={connected} onInspect={setInspect} />
      </div>

      {inspect && <PromptInspector event={inspect} onClose={() => setInspect(null)} />}
    </div>
  );
}

function SectionHeader({ icon, title }: { icon: React.ReactNode; title: string }) {
  return (
    <div className="mb-2 flex items-center gap-2 text-sm font-semibold text-[var(--color-frost)]">
      <span className="text-[var(--color-brand)]">{icon}</span>
      {title}
    </div>
  );
}
