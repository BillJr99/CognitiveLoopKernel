import { useEffect, useMemo, useRef, useState } from "react";
import {
  Brain,
  Send,
  MessageSquare,
  Rocket,
  ChevronRight,
  Maximize2,
  Pause,
  Play,
} from "lucide-react";
import { useSharedActivity } from "../../state/activity";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import type { ActivityEvent } from "../../api/types";
import { shortTime } from "../../lib/format";
import { Badge, EmptyState } from "../common/ui";
import { PromptInspector } from "../dashboard/PromptInspector";

// Which event kinds count as "thinking & dispatching" — dispatches, the
// prompts we send, and the responses (which carry the agent's reasoning).
const PROMPT_KINDS = new Set(["prompt_sent"]);
const RESPONSE_KINDS = new Set(["agent_response"]);
const DISPATCH_KINDS = new Set(["agent_dispatch"]);

type Filter = "all" | "dispatch" | "prompt" | "response";

const FILTERS: { id: Filter; label: string; icon: typeof Rocket }[] = [
  { id: "all", label: "All", icon: Brain },
  { id: "dispatch", label: "Dispatches", icon: Rocket },
  { id: "prompt", label: "Prompts", icon: Send },
  { id: "response", label: "Responses", icon: MessageSquare },
];

function bucket(ev: ActivityEvent): Filter | null {
  if (DISPATCH_KINDS.has(ev.kind)) return "dispatch";
  if (PROMPT_KINDS.has(ev.kind)) return "prompt";
  if (RESPONSE_KINDS.has(ev.kind)) return "response";
  return null;
}

export function ThinkStream() {
  const { activeId } = useActiveWorkspace();
  const { events, connected } = useSharedActivity();
  const [filter, setFilter] = useState<Filter>("all");
  const [expanded, setExpanded] = useState<Set<number>>(new Set());
  const [inspect, setInspect] = useState<ActivityEvent | null>(null);
  const [follow, setFollow] = useState(true);
  const bottomRef = useRef<HTMLDivElement>(null);

  const rows = useMemo(() => {
    return events
      .map((ev, i) => ({ ev, key: i }))
      .filter(({ ev }) => {
        const b = bucket(ev);
        if (!b) return false;
        return filter === "all" || b === filter;
      });
  }, [events, filter]);

  useEffect(() => {
    if (follow && bottomRef.current) bottomRef.current.scrollIntoView({ block: "end" });
  }, [rows.length, follow]);

  if (!activeId) {
    return <EmptyState icon={<Brain size={22} />} title="No workspace selected" hint="Pick a workspace to watch the agents think." />;
  }

  function toggle(key: number) {
    setExpanded((prev) => {
      const next = new Set(prev);
      next.has(key) ? next.delete(key) : next.add(key);
      return next;
    });
  }

  return (
    <div className="mx-auto flex h-full max-w-4xl flex-col gap-3">
      <div className="card flex items-center gap-2 px-4 py-3">
        <Brain size={18} className="text-[var(--color-brand)]" />
        <div className="mr-2">
          <div className="text-sm font-semibold">Thinking &amp; dispatching</div>
          <div className="text-[11px] text-[var(--color-mist)]">
            Live dispatches, prompts, and responses — newest at the bottom.
          </div>
        </div>
        <div className="ml-auto flex flex-wrap items-center gap-1.5">
          {FILTERS.map((f) => {
            const Icon = f.icon;
            const active = filter === f.id;
            return (
              <button
                key={f.id}
                onClick={() => setFilter(f.id)}
                className={`flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-xs transition-colors ${
                  active
                    ? "border-[var(--color-brand)] bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)]"
                    : "border-[var(--color-line)] text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
                }`}
              >
                <Icon size={13} /> {f.label}
              </button>
            );
          })}
          <button
            onClick={() => setFollow((v) => !v)}
            title={follow ? "Pause auto-scroll" : "Resume auto-scroll"}
            className="ml-1 flex items-center gap-1 rounded-lg border border-[var(--color-line)] px-2 py-1 text-xs text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
          >
            {follow ? <Pause size={13} /> : <Play size={13} />}
          </button>
        </div>
      </div>

      <div className="card min-h-0 flex-1 overflow-auto p-2">
        {rows.length === 0 ? (
          <div className="grid h-full place-items-center text-sm text-[var(--color-mist)]">
            {connected ? "Waiting for the agents to think…" : "Connecting to the activity stream…"}
          </div>
        ) : (
          <ul className="flex flex-col gap-1.5">
            {rows.map(({ ev, key }) => (
              <ThinkRow
                key={`${key}-${ev.seq}`}
                ev={ev}
                open={expanded.has(key)}
                onToggle={() => toggle(key)}
                onInspect={() => setInspect(ev)}
              />
            ))}
            <div ref={bottomRef} />
          </ul>
        )}
      </div>

      {inspect && <PromptInspector event={inspect} onClose={() => setInspect(null)} />}
    </div>
  );
}

function ThinkRow({
  ev,
  open,
  onToggle,
  onInspect,
}: {
  ev: ActivityEvent;
  open: boolean;
  onToggle: () => void;
  onInspect: () => void;
}) {
  const b = bucket(ev);
  const tone = b === "dispatch" ? "brand" : b === "response" ? (ev.severity === "error" ? "bad" : "good") : "muted";
  const Icon = b === "dispatch" ? Rocket : b === "prompt" ? Send : MessageSquare;
  const p = ev.payload || {};
  const body =
    b === "prompt"
      ? (p.prompt as string | undefined)
      : b === "response"
        ? (p.response_text as string | undefined)
        : (p.objective as string | undefined);

  return (
    <li className="rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-900)]/50">
      <button onClick={onToggle} className="flex w-full items-center gap-2.5 px-3 py-2 text-left">
        <ChevronRight
          size={14}
          className={`shrink-0 text-[var(--color-mist)] transition-transform ${open ? "rotate-90" : ""}`}
        />
        <Icon size={14} className="shrink-0 text-[var(--color-brand)]" />
        <span className="font-mono text-[11px] tabular-nums text-[var(--color-mist)]">{shortTime(ev.ts)}</span>
        {ev.agent && <span className="shrink-0 text-sm font-semibold">{ev.agent}</span>}
        <Badge tone={tone as any}>{ev.kind}</Badge>
        <span className="min-w-0 flex-1 truncate text-xs text-[var(--color-mist)]">{ev.summary}</span>
      </button>
      {open && (
        <div className="border-t border-[var(--color-line)] px-3 py-2">
          {body ? (
            <pre className="max-h-80 overflow-auto whitespace-pre-wrap rounded-lg bg-[var(--color-ink-950)] p-3 text-[12px] leading-relaxed">
              {body}
            </pre>
          ) : (
            <div className="text-xs text-[var(--color-mist)]">No inline text for this event.</div>
          )}
          <div className="mt-2 flex items-center gap-3 text-[11px] text-[var(--color-mist)]">
            {b === "prompt" && p.prompt_chars != null && <span>{p.prompt_chars} chars</span>}
            {b === "response" && p.tokens_total != null && <span>{p.tokens_total} tokens</span>}
            {p.run_id && <span className="font-mono">run {String(p.run_id).slice(0, 8)}</span>}
            <button
              onClick={onInspect}
              className="ml-auto flex items-center gap-1 rounded-lg border border-[var(--color-line)] px-2 py-1 hover:bg-[var(--color-ink-800)] hover:text-[var(--color-frost)]"
            >
              <Maximize2 size={12} /> Full inspector
            </button>
          </div>
        </div>
      )}
    </li>
  );
}
