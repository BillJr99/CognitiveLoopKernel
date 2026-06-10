// Detect when a workspace is busy but producing no new events, and
// automatically cancel + restart the stalled task.
import { useCallback, useEffect, useRef, useState } from "react";
import { apiPost } from "../api/client";

const STUCK_MS = 90_000;    // fire after 90 s of silence while busy
const COOLDOWN_MS = 90_000; // don't re-fire within 90 s of last nudge

export function useStuckWatchdog(
  wsId: string | null | undefined,
  busy: boolean,
  connected: boolean,
  lastSeq: number | undefined,
  onNudged?: (newTaskId: string) => void,
) {
  const lastSeqRef = useRef<number | undefined>(lastSeq);
  const lastMovedRef = useRef<number>(Date.now());
  const lastNudgeRef = useRef<number>(0);
  // setInterval returns a number in browsers; use the interval-specific overload.
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const [healing, setHealing] = useState(false);

  // Reset timestamps when the workspace changes so the new workspace doesn't
  // inherit a stale lastMovedRef and get nudged prematurely on its first busy tick.
  useEffect(() => {
    lastSeqRef.current = undefined;
    lastMovedRef.current = Date.now();
    lastNudgeRef.current = 0;
  }, [wsId]);

  // Advance the "last activity" clock whenever the event sequence moves forward.
  useEffect(() => {
    if (lastSeq !== lastSeqRef.current) {
      lastSeqRef.current = lastSeq;
      lastMovedRef.current = Date.now();
    }
  }, [lastSeq]);

  const maybeNudge = useCallback(async () => {
    // Only nudge when the SSE stream is live; a disconnected stream naturally
    // stops advancing lastSeq, which would otherwise look identical to a stuck
    // agent and cause us to cancel a healthy long-running task.
    if (!wsId || !busy || !connected) return;
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
      // Best-effort; if the nudge fails let the user notice naturally.
    } finally {
      setHealing(false);
    }
  }, [wsId, busy, connected, onNudged]);

  // Poll every 10 s only while the workspace is busy AND the stream is connected.
  useEffect(() => {
    if (!busy || !wsId || !connected) {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
      return;
    }
    timerRef.current = setInterval(() => void maybeNudge(), 10_000);
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [busy, wsId, connected, maybeNudge]);

  return { healing };
}
