// Detect when a workspace is busy but producing no new events, and
// automatically cancel + restart the stalled task.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost } from "../api/client";

const STUCK_MS = 90_000;   // fire after 90 s of silence while busy
const COOLDOWN_MS = 90_000; // don't re-fire within 90 s of last nudge

export function useStuckWatchdog(
  wsId: string | null | undefined,
  busy: boolean,
  lastSeq: number | undefined,
  onNudged?: (newTaskId: string) => void,
) {
  const lastSeqRef = useRef<number | undefined>(lastSeq);
  const lastMovedRef = useRef<number>(Date.now());
  const lastNudgeRef = useRef<number>(0);
  const timerRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const [healing, setHealing] = useState(false);

  // Update move timestamp whenever the event sequence advances.
  useEffect(() => {
    if (lastSeq !== lastSeqRef.current) {
      lastSeqRef.current = lastSeq;
      lastMovedRef.current = Date.now();
    }
  }, [lastSeq]);

  const maybeNudge = useCallback(async () => {
    if (!wsId || !busy) return;
    const now = Date.now();
    if (now - lastMovedRef.current < STUCK_MS) return;
    if (now - lastNudgeRef.current < COOLDOWN_MS) return;

    lastNudgeRef.current = now;
    setHealing(true);
    try {
      const res = await apiPost<{ action: string; task_id?: string }>(
        `/api/workspaces/${wsId}/nudge`,
        {},
      );
      if (res.action === "restarted" && res.task_id) {
        onNudged?.(res.task_id);
      }
    } catch {
      // Best-effort; if the nudge fails just let the user notice naturally.
    } finally {
      setHealing(false);
    }
  }, [wsId, busy, onNudged]);

  // Poll every 10 s while the workspace is busy.
  useEffect(() => {
    if (!busy || !wsId) {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      return;
    }
    timerRef.current = setInterval(() => void maybeNudge(), 10_000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [busy, wsId, maybeNudge]);

  return { healing };
}
