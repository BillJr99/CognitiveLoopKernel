import { describe, expect, it } from "vitest";
import type { ActivityEvent } from "../../api/types";
import { friendlyEvent, friendlyRole, stageFor, workspaceNameFrom } from "./friendly";

function ev(kind: string, agent = "chief", payload: Record<string, any> = {}): ActivityEvent {
  return {
    seq: 1, ts: "2026-01-01T00:00:00Z", kind, agent, run_id: "r1",
    severity: "info", category: "event", summary: "", payload,
  };
}

describe("friendlyEvent", () => {
  it("translates dispatches and prompts into plain English", () => {
    expect(friendlyEvent(ev("agent_dispatch"))).toBe("Chief is starting on a task…");
    expect(friendlyEvent(ev("prompt_sent", "qa"))).toBe("Qa is thinking…");
  });

  it("describes file actions with the file name", () => {
    expect(friendlyEvent(ev("action_applied", "engineer", { action: "write", path: "todo.py" }))).toBe(
      "Engineer wrote todo.py",
    );
    expect(friendlyEvent(ev("action_applied", "engineer", { action: "run" }))).toBe("Engineer ran a command");
  });

  it("hides internal noise", () => {
    expect(friendlyEvent(ev("subprocess_start"))).toBeNull();
    expect(friendlyEvent(ev("provider_attempt"))).toBeNull();
  });

  it("describes commits without git jargon", () => {
    expect(friendlyEvent(ev("git_commit"))).toBe("Progress saved (checkpoint created)");
  });
});

describe("friendlyRole", () => {
  it("maps known roster names to plain words", () => {
    expect(friendlyRole("chief", "decompose objectives")).toBe("Team lead");
    expect(friendlyRole("qa", "test and audit")).toBe("Quality checker");
    expect(friendlyRole("engineer-1", "build features")).toBe("Builder");
  });
});

describe("stageFor", () => {
  it("starts at understanding, advances as the team grows, then builds", () => {
    expect(stageFor("cast", 0).index).toBe(0);
    expect(stageFor("cast", 5).index).toBe(1);
    expect(stageFor("build", 5).index).toBe(2);
  });
});

describe("workspaceNameFrom", () => {
  it("trims and cleans the question", () => {
    expect(workspaceNameFrom("Build a   to-do app! With tags?")).toBe("Build a to-do app With tags");
  });
  it("falls back when the question is all symbols", () => {
    expect(workspaceNameFrom("???")).toMatch(/^Project /);
  });
});
