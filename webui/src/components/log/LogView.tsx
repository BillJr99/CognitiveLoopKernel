import { useState } from "react";
import { useSharedActivity } from "../../state/activity";
import { ActivityTimeline } from "../dashboard/ActivityTimeline";
import { PromptInspector } from "../dashboard/PromptInspector";
import type { ActivityEvent } from "../../api/types";

export function LogView() {
  const { events, connected } = useSharedActivity();
  const [inspect, setInspect] = useState<ActivityEvent | null>(null);

  return (
    <div className="flex h-full min-h-0 flex-col">
      <ActivityTimeline events={events} connected={connected} onInspect={setInspect} />
      {inspect && <PromptInspector event={inspect} onClose={() => setInspect(null)} />}
    </div>
  );
}
