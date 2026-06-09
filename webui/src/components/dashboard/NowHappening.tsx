import { useEffect, useState } from "react";
import { Sparkles, Activity } from "lucide-react";
import type { ActivityEvent, Snapshot } from "../../api/types";

// A prominent, glanceable "what's happening right now" banner. It pulses
// with the latest activity event so the user always sees the live state of
// the system without reading the full timeline.
export function NowHappening({ snap, latest }: { snap?: Snapshot; latest?: ActivityEvent }) {
  const [bump, setBump] = useState(0);

  useEffect(() => {
    if (latest) setBump((b) => b + 1);
  }, [latest?.seq, latest?.ts]);

  const busy = snap?.busy;
  const workingAgent = snap ? Object.values(snap.agents).find((a) => a.status === "working") : undefined;

  const headline = busy
    ? workingAgent
      ? `${workingAgent.name} is working…`
      : "Agents are working…"
    : snap && snap.event_count > 0
      ? "Cycle complete — idle"
      : "Ready when you are";

  return (
    <div className="card relative overflow-hidden p-4">
      {busy && <div className="activity-sweep absolute inset-x-0 top-0 h-0.5" />}
      <div className="flex items-center gap-3">
        <div
          className={`grid h-10 w-10 place-items-center rounded-xl ${
            busy
              ? "bg-gradient-to-br from-[var(--color-brand)] to-[var(--color-iris)] working-ring"
              : "bg-[var(--color-ink-700)]"
          }`}
        >
          {busy ? (
            <Activity size={20} className="text-[var(--color-ink-950)]" />
          ) : (
            <Sparkles size={20} className="text-[var(--color-brand-bright)]" />
          )}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-sm font-semibold">{headline}</div>
          <div key={bump} className="slide-in truncate text-xs text-[var(--color-mist)]">
            {latest ? (
              <>
                <span className="font-medium text-[var(--color-frost)]">{latest.agent || latest.kind}</span>
                {" — "}
                {latest.summary}
              </>
            ) : (
              "Set an idea on the Run tab and start a workflow to watch the team go."
            )}
          </div>
        </div>
        {workingAgent?.last_thought && (
          <div className="hidden max-w-xs shrink-0 items-start gap-1.5 rounded-lg bg-[var(--color-ink-900)]/60 p-2 text-[11px] text-[var(--color-mist)] lg:flex">
            <Sparkles size={12} className="mt-0.5 shrink-0 text-[var(--color-iris)]" />
            <span className="line-clamp-2">{workingAgent.last_thought}</span>
          </div>
        )}
      </div>
    </div>
  );
}
