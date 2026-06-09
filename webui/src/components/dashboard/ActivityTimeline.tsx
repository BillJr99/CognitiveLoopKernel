import { useEffect, useMemo, useRef, useState } from "react";
import {
  AlertTriangle, CheckCircle2, FileEdit, GitCommitHorizontal, MessageSquare,
  RefreshCw, Send, Users, Wand2, XCircle, Circle, Radio,
} from "lucide-react";
import type { ActivityEvent } from "../../api/types";
import { shortTime } from "../../lib/format";

const CATEGORY_ICON: Record<string, typeof Send> = {
  dispatch: Send,
  provider: Radio,
  recovery: RefreshCw,
  coordination: Users,
  action: FileEdit,
  git: GitCommitHorizontal,
  workflow: Wand2,
  roster: Users,
  event: Circle,
};

const SEVERITY_COLOR: Record<string, string> = {
  success: "text-[var(--color-good)]",
  warn: "text-[var(--color-warn)]",
  error: "text-[var(--color-bad)]",
  info: "text-[var(--color-brand)]",
  muted: "text-[var(--color-mist)]",
};

function iconFor(ev: ActivityEvent) {
  if (ev.kind === "agent_response") return ev.severity === "error" ? XCircle : CheckCircle2;
  if (ev.kind === "prompt_sent") return MessageSquare;
  if (ev.severity === "error") return AlertTriangle;
  return CATEGORY_ICON[ev.category] ?? Circle;
}

export function ActivityTimeline({
  events,
  connected,
  onInspect,
}: {
  events: ActivityEvent[];
  connected: boolean;
  onInspect: (ev: ActivityEvent) => void;
}) {
  const [filter, setFilter] = useState<string>("all");
  const scroller = useRef<HTMLDivElement>(null);
  const pinned = useRef(true);

  const categories = useMemo(() => {
    const set = new Set<string>();
    events.forEach((e) => set.add(e.category));
    return ["all", ...Array.from(set).sort()];
  }, [events]);

  const shown = filter === "all" ? events : events.filter((e) => e.category === filter);

  useEffect(() => {
    if (pinned.current && scroller.current) {
      scroller.current.scrollTop = scroller.current.scrollHeight;
    }
  }, [shown.length]);

  return (
    <div className="card flex h-full min-h-0 flex-col">
      <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-4 py-2.5">
        <span
          className={`h-2.5 w-2.5 rounded-full ${
            connected ? "bg-[var(--color-good)] live-dot" : "bg-[var(--color-mist)]"
          }`}
        />
        <span className="text-sm font-semibold">Activity</span>
        <span className="text-[11px] text-[var(--color-mist)]">{connected ? "live feed" : "reconnecting…"}</span>
        <div className="ml-auto flex flex-wrap gap-1">
          {categories.map((c) => (
            <button
              key={c}
              onClick={() => setFilter(c)}
              className={`rounded-full px-2 py-0.5 text-[11px] capitalize transition-colors ${
                filter === c
                  ? "bg-[var(--color-brand)]/20 text-[var(--color-brand-bright)]"
                  : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
              }`}
            >
              {c}
            </button>
          ))}
        </div>
      </div>

      <div
        ref={scroller}
        onScroll={(e) => {
          const el = e.currentTarget;
          pinned.current = el.scrollHeight - el.scrollTop - el.clientHeight < 40;
        }}
        className="min-h-0 flex-1 overflow-auto px-2 py-2"
      >
        {shown.length === 0 && (
          <div className="grid h-full place-items-center text-sm text-[var(--color-mist)]">
            Waiting for agent activity…
          </div>
        )}
        <ul className="flex flex-col">
          {shown.map((ev) => {
            const Icon = iconFor(ev);
            return (
              <li key={`${ev.seq}-${ev.ts}-${ev.kind}`} className="slide-in">
                <button
                  onClick={() => onInspect(ev)}
                  className="group flex w-full items-start gap-2.5 rounded-lg px-2 py-1.5 text-left transition-colors hover:bg-[var(--color-ink-800)]"
                >
                  <Icon size={15} className={`mt-0.5 shrink-0 ${SEVERITY_COLOR[ev.severity] ?? ""}`} />
                  <div className="min-w-0 flex-1">
                    <div className="flex items-baseline gap-2">
                      {ev.agent && <span className="text-xs font-medium text-[var(--color-frost)]">{ev.agent}</span>}
                      <span className="truncate text-xs text-[var(--color-mist)]">{ev.summary}</span>
                    </div>
                  </div>
                  <span className="shrink-0 font-mono text-[10px] text-[var(--color-mist)]">{shortTime(ev.ts)}</span>
                </button>
              </li>
            );
          })}
        </ul>
      </div>
    </div>
  );
}
