// Shares a single activity SSE connection across the whole app so the
// TopBar's live indicator and the Dashboard timeline don't each open their
// own EventSource for the same workspace.
import { createContext, useContext } from "react";
import { useActivityStream } from "../api/useEventStream";
import type { StreamState } from "../api/useEventStream";
import { useActiveWorkspace } from "./activeWorkspace";

const ActivityContext = createContext<StreamState>({ events: [], connected: false });

export function ActivityStreamProvider({ children }: { children: React.ReactNode }) {
  const { activeId } = useActiveWorkspace();
  const stream = useActivityStream(activeId);
  return <ActivityContext.Provider value={stream}>{children}</ActivityContext.Provider>;
}

export function useSharedActivity(): StreamState {
  return useContext(ActivityContext);
}
