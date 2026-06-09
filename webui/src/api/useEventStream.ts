// SSE hook that follows a workspace's activity stream, replaying history
// then following live, with automatic reconnect and seq de-duplication.
import { useEffect, useRef, useState } from "react";
import type { ActivityEvent } from "./types";

export interface StreamState {
  events: ActivityEvent[];
  connected: boolean;
}

const MAX_EVENTS = 2000;

export function useActivityStream(workspaceId: string | null): StreamState {
  const [events, setEvents] = useState<ActivityEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const seenSeq = useRef<Set<number>>(new Set());
  const retry = useRef(0);

  useEffect(() => {
    if (!workspaceId) {
      setEvents([]);
      return;
    }
    seenSeq.current = new Set();
    setEvents([]);
    let es: EventSource | null = null;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | undefined;

    const connect = (from: "start" | "end") => {
      if (stopped) return;
      es = new EventSource(`/api/workspaces/${workspaceId}/activity/stream?from=${from}`);
      es.onopen = () => {
        setConnected(true);
        retry.current = 0;
      };
      es.onmessage = (ev) => {
        if (!ev.data || ev.data.startsWith(":")) return;
        try {
          const parsed: ActivityEvent = JSON.parse(ev.data);
          // The stream restarts seq at 0 per connection; key on
          // (seq, ts, kind) to avoid double-rendering replayed events.
          const key = `${parsed.seq}|${parsed.ts}|${parsed.kind}|${parsed.agent}`;
          const hash = hashKey(key);
          if (seenSeq.current.has(hash)) return;
          seenSeq.current.add(hash);
          setEvents((prev) => {
            const next = [...prev, parsed];
            return next.length > MAX_EVENTS ? next.slice(next.length - MAX_EVENTS) : next;
          });
        } catch {
          /* ignore malformed frame */
        }
      };
      es.onerror = () => {
        setConnected(false);
        es?.close();
        if (stopped) return;
        retry.current = Math.min(retry.current + 1, 6);
        const delay = Math.min(1000 * 2 ** retry.current, 15_000);
        // On reconnect we follow only new events to avoid a full replay storm.
        timer = setTimeout(() => connect("end"), delay);
      };
    };

    connect("start");
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      es?.close();
      setConnected(false);
    };
  }, [workspaceId]);

  return { events, connected };
}

function hashKey(s: string): number {
  let h = 0;
  for (let i = 0; i < s.length; i++) {
    h = (h << 5) - h + s.charCodeAt(i);
    h |= 0;
  }
  return h;
}
