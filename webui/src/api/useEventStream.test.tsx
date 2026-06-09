import { act, renderHook, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import { useActivityStream } from "./useEventStream";

// Minimal EventSource mock that lets the test push frames.
class MockEventSource {
  static instances: MockEventSource[] = [];
  url: string;
  onopen: (() => void) | null = null;
  onmessage: ((ev: { data: string }) => void) | null = null;
  onerror: (() => void) | null = null;
  closed = false;
  constructor(url: string) {
    this.url = url;
    MockEventSource.instances.push(this);
    queueMicrotask(() => this.onopen?.());
  }
  emit(data: string) {
    this.onmessage?.({ data });
  }
  close() {
    this.closed = true;
  }
}

afterEach(() => {
  MockEventSource.instances = [];
  vi.restoreAllMocks();
});

describe("useActivityStream", () => {
  it("collects events and de-duplicates replayed frames", async () => {
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
    const { result } = renderHook(() => useActivityStream("ws-1"));

    const es = MockEventSource.instances[0];
    expect(es.url).toContain("from=start");

    const frame = JSON.stringify({
      seq: 0, ts: "2026-06-09T10:00:00.000", kind: "agent_dispatch",
      agent: "engineer", run_id: "r1", severity: "info", category: "dispatch",
      summary: "engineer dispatched", payload: {},
    });

    act(() => {
      es.emit(frame);
      es.emit(frame); // duplicate — must be dropped
      es.emit(":keepalive"); // comment frame — ignored
    });

    await waitFor(() => expect(result.current.events.length).toBe(1));
    expect(result.current.events[0].agent).toBe("engineer");
  });

  it("clears events when the workspace changes", async () => {
    vi.stubGlobal("EventSource", MockEventSource as unknown as typeof EventSource);
    const { result, rerender } = renderHook(({ ws }) => useActivityStream(ws), {
      initialProps: { ws: "ws-1" },
    });
    act(() => {
      MockEventSource.instances[0].emit(
        JSON.stringify({ seq: 0, ts: "t", kind: "x", agent: "a", run_id: "", severity: "info", category: "event", summary: "s", payload: {} }),
      );
    });
    await waitFor(() => expect(result.current.events.length).toBe(1));
    rerender({ ws: "ws-2" });
    await waitFor(() => expect(result.current.events.length).toBe(0));
  });
});
