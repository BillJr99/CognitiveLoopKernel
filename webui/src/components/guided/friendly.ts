// Beginner-facing copy for guided mode: provider menu metadata, plain-English
// activity descriptions, and stage headlines. Keeping jargon translation in
// one module makes it easy to test and tune.
import type { ActivityEvent, DiscoveredProvider } from "../../api/types";

export interface ProviderMeta {
  tagline: string;
  /** Shown when the provider is unavailable: what to do about it. */
  fixHint: string;
  /** Short note shown when no model menu applies (CLI providers). */
  modelNote?: string;
  /** Accent used for the card icon chip. */
  hue: string;
}

export const PROVIDER_META: Record<string, ProviderMeta> = {
  ollama: {
    tagline: "Free, private AI models running on your own machine",
    fixHint:
      "Ollama isn't reachable. Install it from ollama.com, run `ollama serve`, then scan again.",
    hue: "from-emerald-400 to-teal-500",
  },
  openwebui: {
    tagline: "Models served by your OpenWebUI server",
    fixHint:
      "No OpenWebUI server found at the usual address. Start it (default port 8080), then scan again.",
    hue: "from-sky-400 to-blue-500",
  },
  claude: {
    tagline: "Anthropic's Claude — excellent at coding and reasoning",
    fixHint:
      "Install the Claude CLI (`npm install -g @anthropic-ai/claude-code`) or paste an Anthropic API key below.",
    modelNote: "Uses your Claude setup's default model",
    hue: "from-orange-400 to-amber-500",
  },
  codex: {
    tagline: "OpenAI's models via the Codex CLI",
    fixHint: "Install the Codex CLI or paste an OpenAI API key below.",
    modelNote: "Uses your Codex setup's default model",
    hue: "from-violet-400 to-purple-500",
  },
  gemini: {
    tagline: "Google's Gemini models",
    fixHint: "Install the Gemini CLI or paste a Gemini API key below.",
    modelNote: "Uses your Gemini setup's default model",
    hue: "from-blue-400 to-indigo-500",
  },
  pi: {
    tagline: "The Pi terminal harness (advanced, multi-route)",
    fixHint: "The Pi CLI isn't installed. Most people can pick another option.",
    modelNote: "Uses your Pi configuration",
    hue: "from-pink-400 to-rose-500",
  },
};

export function providerMeta(p: DiscoveredProvider): ProviderMeta {
  return (
    PROVIDER_META[p.name] ?? {
      tagline: "A configured AI provider",
      fixHint: "This provider isn't available right now.",
      hue: "from-slate-400 to-slate-500",
    }
  );
}

function cap(s: string): string {
  return s ? s.charAt(0).toUpperCase() + s.slice(1) : s;
}

/**
 * Translate a raw activity event into one plain-English sentence, or null
 * when the event is internal noise a beginner shouldn't see.
 */
export function friendlyEvent(ev: ActivityEvent): string | null {
  const agent = cap(ev.agent || "An agent");
  const file = ev.payload?.path || ev.payload?.file || "";
  switch (ev.kind) {
    case "agent_dispatch":
      return `${agent} is starting on a task…`;
    case "prompt_sent":
      return `${agent} is thinking…`;
    case "agent_response":
      return `${agent} finished a step`;
    case "action_applied": {
      const action = String(ev.payload?.action ?? ev.payload?.type ?? "").toLowerCase();
      if (action === "write" && file) return `${agent} wrote ${file}`;
      if (action === "edit" && file) return `${agent} updated ${file}`;
      if (action === "append" && file) return `${agent} added to ${file}`;
      if (action === "delete" && file) return `${agent} removed ${file}`;
      if (action === "run") return `${agent} ran a command`;
      return file ? `${agent} changed ${file}` : `${agent} made a change`;
    }
    case "git_commit":
      return "Progress saved (checkpoint created)";
    case "provider_retry":
    case "agent_quality_retry":
    case "workflow_stage_retry":
      return `${agent} hit a snag — trying again`;
    case "consensus_started":
      return "The team is comparing ideas…";
    case "workflow_round_complete":
      return "Finished a full round of work";
    case "workflow_aborted":
      return "The run stopped early";
    case "default_agent_created":
    case "role_minted":
      return `${agent} joined the team`;
    case "workflow_written":
      return "The team drew up its plan of attack";
    case "blackboard_post":
      return `${agent} shared notes with the team`;
    default:
      return null; // internal noise (subprocess_*, provider_attempt, …)
  }
}

/** Plain-word description of an agent's role for the avatar chips. */
export function friendlyRole(name: string, role: string): string {
  const n = name.toLowerCase();
  if (n === "chief") return "Team lead";
  if (n === "qa") return "Quality checker";
  if (n === "ralph") return "Improver";
  if (n.includes("engineer") || n.includes("dev")) return "Builder";
  if (n.includes("research")) return "Researcher";
  if (n.includes("analyst")) return "Analyst";
  if (n.includes("design")) return "Designer";
  if (n.includes("writer") || n.includes("doc")) return "Writer";
  return role ? cap(role.split(",")[0].trim()).slice(0, 28) : "Team member";
}

export type GuidedPipeline = "cast" | "build" | null;

export interface StageInfo {
  index: 0 | 1 | 2;
  headline: string;
  detail: string;
}

/** Map the two-task pipeline + snapshot hints onto a 3-stage tracker. */
export function stageFor(pipeline: GuidedPipeline, agentCount: number): StageInfo {
  if (pipeline === "cast") {
    if (agentCount > 3) {
      return {
        index: 1,
        headline: "Assembling your team",
        detail: "The lead is picking specialists for your idea",
      };
    }
    return {
      index: 0,
      headline: "Understanding your idea",
      detail: "The team lead is reading what you asked for",
    };
  }
  return {
    index: 2,
    headline: "Building it",
    detail: "The team is writing files, testing, and saving progress",
  };
}

export const STAGES = ["Understanding your idea", "Assembling your team", "Building it"] as const;

/** Derive a tidy workspace name from the user's question. */
export function workspaceNameFrom(question: string): string {
  const cleaned = question
    .replace(/\s+/g, " ")
    .trim()
    .slice(0, 40)
    .replace(/[^\w\s-]/g, "")
    .trim();
  return cleaned || `Project ${new Date().toLocaleDateString()}`;
}

export const EXAMPLE_IDEAS = [
  "Build a simple website for my dog-walking business with a booking form",
  "Make a Python script that organizes my photos into folders by date",
  "Create a study-plan generator that turns any topic into a 4-week schedule",
];
