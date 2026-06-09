import { useEffect, useRef, useState } from "react";
import { Activity, Sparkles } from "lucide-react";
import type { ActivityEvent } from "../../api/types";

// A prominent, animated "what's happening right now" banner. It surfaces
// the latest activity event so the user always sees the agents working at
// a glance — the heart of the real-time experience.
export function NowHappening({ events, busy }: { events: ActivityEvent[]; busy: boolean }) {
  const latest = events[events.length - 1];
  const [key, setKey] = useState(0);
  const prev = useRef<string>("");

  useEffect(() => {
    if (latest && latest.summary !== prev.current) {
      prev.current = latest.summary;
      setKey((k) => k + 1);
    }
  }, [latest?.summary]);

  if (!latest) {
    return (
      <div className="card flex items-center gap-3 overflow-hidden p-4">
        <div className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-[var(--color-ink-700)]">
          <Sparkles size={18} className="text-[var(--color-iris)]" />
        </div>
        <div className="text-sm text-[var(--color-mist)]">
          Ready when you are — start a workflow from the <b className="text-[var(--color-frost)]">Run</b> tab and the agents will spring to life here.
        </div>
      </div>
    );
  }

  return (
    <div className="card relative flex items-center gap-3 overflow-hidden p-4">
      {busy && <div className="activity-sweep pointer-events-none absolute inset-0 opacity-40" />}
      <div
        className={`relative grid h-9 w-9 shrink-0 place-items-center rounded-lg ${
          busy ? "bg-gradient-to-br from-[var(--color-brand)] to-[var(--color-iris)]" : "bg-[var(--color-ink-700)]"
        }`}
      >
        <Activity size={18} className={busy ? "text-[var(--color-ink-950)]" : "text-[var(--color-brand)]"} />
      </div>
      <div className="relative min-w-0 flex-1">
        <div className="text-[10px] uppercase tracking-wider text-[var(--color-mist)]">
          {busy ? "Now happening" : "Latest activity"}
        </div>
        <div key={key} className="slide-in truncate text-sm font-medium text-[var(--color-frost)]">
          {latest.agent && <span className="text-[var(--color-brand-bright)]">{latest.agent} · </span>}
          {latest.summary}
        </div>
      </div>
      {busy && (
        <span className="relative flex items-center gap-1.5 rounded-full bg-[var(--color-brand)]/15 px-3 py-1 text-[11px] font-medium text-[var(--color-brand-bright)]">
          <span className="h-1.5 w-1.5 rounded-full bg-[var(--color-good)] live-dot" />
          live
        </span>
      )}
    </div>
  );
}
