// Guided mode: a full-screen step-by-step wizard for people new to agents.
// Orchestrates discovery -> model choice -> idea -> the idea/run task
// pipeline -> results -> follow-up loop. All choices are persisted through
// the same APIs the Advanced console uses, so switching modes mid-flight
// shows the same workspace live on the Dashboard.
import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { Brain, PanelsTopLeft } from "lucide-react";
import { apiGet, apiPost, apiPut } from "../../api/client";
import { useTaskStatus } from "../../api/hooks";
import type { DiscoveredProvider, ProvidersConfig } from "../../api/types";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { useUiMode } from "../../state/uiMode";
import { workspaceNameFrom } from "./friendly";
import type { GuidedPipeline } from "./friendly";
import { WelcomeStep } from "./steps/WelcomeStep";
import { ProviderStep } from "./steps/ProviderStep";
import { ModelStep } from "./steps/ModelStep";
import { IdeaStep } from "./steps/IdeaStep";
import { WorkingStep } from "./steps/WorkingStep";
import { FilesStep } from "./steps/FilesStep";
import { FollowUpStep } from "./steps/FollowUpStep";

type GuidedStep = "welcome" | "provider" | "model" | "idea" | "working" | "files" | "followup";

interface GuidedState {
  step: GuidedStep;
  provider: DiscoveredProvider | null;
  model: string;
  wsId: string | null;
  question: string;
  stopWhen: string;
  pipeline: GuidedPipeline;
  taskId: string | null;
  rounds: string[];
  error: string | null;
}

type Action =
  | { type: "GO"; step: GuidedStep }
  | { type: "BACK" }
  | { type: "PICK_PROVIDER"; provider: DiscoveredProvider }
  | { type: "PICK_MODEL"; model: string }
  | { type: "SET_STOP_WHEN"; stopWhen: string }
  | { type: "LAUNCHED"; wsId: string; taskId: string; pipeline: Exclude<GuidedPipeline, null>; question: string }
  | { type: "TASK_STARTED"; taskId: string; pipeline: Exclude<GuidedPipeline, null> }
  | { type: "TASK_DONE" }
  | { type: "FAIL"; message: string }
  | { type: "FOLLOWUP_LAUNCHED"; taskId: string; question: string };

const INITIAL: GuidedState = {
  step: "welcome",
  provider: null,
  model: "",
  wsId: null,
  question: "",
  stopWhen: "",
  pipeline: null,
  taskId: null,
  rounds: [],
  error: null,
};

const BACK_FROM: Partial<Record<GuidedStep, GuidedStep>> = {
  provider: "welcome",
  model: "provider",
  idea: "provider", // model step may have been skipped; provider is always safe
  followup: "files",
};

function reducer(state: GuidedState, action: Action): GuidedState {
  switch (action.type) {
    case "GO":
      return { ...state, step: action.step, error: null };
    case "BACK": {
      const prev = BACK_FROM[state.step];
      if (!prev) return state;
      // Going back from idea to a provider with a model menu re-enters via
      // model; if the model step was skipped (no models), skip it again.
      if (state.step === "idea" && state.provider?.kind === "http" && state.provider.models.length > 0) {
        return { ...state, step: "model", error: null };
      }
      return { ...state, step: prev, error: null };
    }
    case "PICK_PROVIDER": {
      const p = action.provider;
      const next: GuidedStep = p.kind === "http" && p.models.length > 0 ? "model" : "idea";
      return { ...state, provider: p, model: "", step: next, error: null };
    }
    case "PICK_MODEL":
      return { ...state, model: action.model, step: "idea", error: null };
    case "SET_STOP_WHEN":
      return { ...state, stopWhen: action.stopWhen };
    case "LAUNCHED":
      return {
        ...state,
        wsId: action.wsId,
        taskId: action.taskId,
        pipeline: action.pipeline,
        question: action.question,
        rounds: [action.question],
        step: "working",
        error: null,
      };
    case "TASK_STARTED":
      return { ...state, taskId: action.taskId, pipeline: action.pipeline, step: "working", error: null };
    case "TASK_DONE":
      return { ...state, taskId: null, pipeline: null, step: "files", error: null };
    case "FAIL":
      return { ...state, error: action.message };
    case "FOLLOWUP_LAUNCHED":
      return {
        ...state,
        taskId: action.taskId,
        pipeline: "build",
        rounds: [...state.rounds, action.question],
        step: "working",
        error: null,
      };
    default:
      return state;
  }
}

const SESSION_KEY = "clk.guided";

function loadSession(): GuidedState {
  try {
    const raw = sessionStorage.getItem(SESSION_KEY);
    if (!raw) return INITIAL;
    const saved = JSON.parse(raw) as Partial<GuidedState>;
    return { ...INITIAL, ...saved, error: null };
  } catch {
    return INITIAL;
  }
}

export function GuidedMode() {
  const [state, dispatch] = useReducer(reducer, undefined, loadSession);
  const { setActiveId } = useActiveWorkspace();
  const { setMode } = useUiMode();
  const [busy, setBusy] = useState(false);
  const { data: taskStatus } = useTaskStatus(state.taskId);
  // Guards the cast->build hop so a re-render can't launch the build twice.
  const advancedFromTask = useRef<string | null>(null);

  // Persist enough to survive a reload / a round-trip through Advanced mode.
  // The API key is deliberately not persisted; once launched it lives in .env.
  useEffect(() => {
    try {
      const { error: _e, ...rest } = state;
      sessionStorage.setItem(SESSION_KEY, JSON.stringify(rest));
    } catch {
      /* ignore */
    }
  }, [state]);

  const apiKeyRef = useRef("");

  const launch = useCallback(
    async (question: string) => {
      const provider = state.provider;
      if (!provider || busy) return;
      setBusy(true);
      try {
        // 1. A workspace named after the idea.
        const ws = await apiPost<{ workspace_id: string }>("/api/workspaces", {
          name: workspaceNameFrom(question),
        });
        const wsId = ws.workspace_id;
        setActiveId(wsId);

        // 2. Persist the chosen provider block (merge keeps the others).
        const cfg = await apiGet<ProvidersConfig>(`/api/workspaces/${wsId}/config/providers`);
        const base = (cfg.providers?.[provider.name] as Record<string, unknown>) ?? { type: provider.type };
        const block: Record<string, unknown> = { ...base };
        if (provider.kind === "http") {
          if (provider.endpoint) block.endpoint = provider.endpoint;
          if (state.model) block.model = state.model;
          if (apiKeyRef.current) block.api_key = apiKeyRef.current;
        } else if (apiKeyRef.current) {
          block.mode = "api";
          block.api_key = apiKeyRef.current;
        } else if (provider.mode) {
          block.mode = provider.mode;
        }
        await apiPut(`/api/workspaces/${wsId}/config/providers`, {
          providers: { [provider.name]: block },
          active: provider.name,
        });

        // 3. Global .env: CLK_PROVIDER overrides the workspace 'active' at
        // runtime, so a stale value here would silently defeat the wizard.
        const envValues: Record<string, string> = { CLK_PROVIDER: provider.name };
        if (apiKeyRef.current && provider.api_key_env) envValues[provider.api_key_env] = apiKeyRef.current;
        if (provider.name === "ollama") {
          if (provider.endpoint) envValues.CLK_OLLAMA_ENDPOINT = provider.endpoint;
          if (state.model) envValues.CLK_OLLAMA_MODEL = state.model;
        }
        if (provider.name === "openwebui") {
          // openwebui.py prefers these env vars over the config block, so a
          // stale .env would silently override the wizard's selections.
          if (provider.endpoint) envValues.CLK_OPENWEBUI_ENDPOINT = provider.endpoint;
          if (state.model) envValues.CLK_OPENWEBUI_MODEL = state.model;
          if (apiKeyRef.current) envValues.CLK_OPENWEBUI_API_KEY = apiKeyRef.current;
        }
        await apiPut("/api/env", { values: envValues });

        // 4. Capture the idea — the chief reads it and casts the team.
        // then_run makes the server auto-chain the build, so the pipeline
        // keeps moving even if the user switches modes or reloads mid-cast.
        const res = await apiPost<{ task_id: string }>("/api/research", {
          command: "idea",
          args: [question],
          workspace_id: wsId,
          then_run: "engineering",
          stop_when: state.stopWhen || undefined,
        });
        dispatch({ type: "LAUNCHED", wsId, taskId: res.task_id, pipeline: "cast", question });
      } catch (e) {
        dispatch({ type: "FAIL", message: (e as Error).message || "Setup failed — please try again." });
      } finally {
        setBusy(false);
      }
    },
    [state.provider, state.model, state.stopWhen, busy, setActiveId],
  );

  const startBuild = useCallback(async () => {
    if (!state.wsId) return;
    try {
      const res = await apiPost<{ task_id: string }>("/api/research", {
        command: "run",
        workspace_id: state.wsId,
        workflow: "engineering",
      });
      dispatch({ type: "TASK_STARTED", taskId: res.task_id, pipeline: "build" });
    } catch (e) {
      dispatch({ type: "FAIL", message: (e as Error).message || "Couldn't start the build." });
    }
  }, [state.wsId]);

  // Retry after a failure without re-running workspace/provider setup.
  const retry = useCallback(async () => {
    if (!state.wsId) return;
    try {
      if (state.pipeline === "cast") {
        const res = await apiPost<{ task_id: string }>("/api/research", {
          command: "idea",
          args: [state.question],
          workspace_id: state.wsId,
          then_run: "engineering",
        });
        dispatch({ type: "TASK_STARTED", taskId: res.task_id, pipeline: "cast" });
      } else {
        await startBuild();
      }
    } catch (e) {
      dispatch({ type: "FAIL", message: (e as Error).message || "Retry failed." });
    }
  }, [state.wsId, state.pipeline, state.question, startBuild]);

  const followUp = useCallback(
    async (request: string, stopWhen?: string) => {
      if (!state.wsId || busy) return;
      setBusy(true);
      try {
        await apiPut(`/api/workspaces/${state.wsId}/idea`, { statement: request });
        const res = await apiPost<{ task_id: string }>("/api/research", {
          command: "run",
          workspace_id: state.wsId,
          workflow: "engineering",
          stop_when: stopWhen || undefined,
        });
        dispatch({ type: "FOLLOWUP_LAUNCHED", taskId: res.task_id, question: request });
      } catch (e) {
        dispatch({ type: "FAIL", message: (e as Error).message || "Couldn't send the follow-up." });
      } finally {
        setBusy(false);
      }
    },
    [state.wsId, busy],
  );

  // Drive the two-task pipeline: when casting finishes, kick off the build;
  // when the build finishes, show the files. Failures surface a retry.
  useEffect(() => {
    if (!taskStatus || taskStatus.task_id !== state.taskId || state.step !== "working") return;
    if (taskStatus.status === "done") {
      if (state.pipeline === "cast" && advancedFromTask.current !== taskStatus.task_id) {
        advancedFromTask.current = taskStatus.task_id;
        // The server chains the build itself (then_run); just start tracking
        // the chained task. Fall back to a client-side launch for tasks
        // created before chaining existed.
        if (taskStatus.chained_task_id) {
          dispatch({ type: "TASK_STARTED", taskId: taskStatus.chained_task_id, pipeline: "build" });
        } else {
          void startBuild();
        }
      } else if (state.pipeline === "build") {
        dispatch({ type: "TASK_DONE" });
      }
    } else if (taskStatus.status === "failed") {
      dispatch({
        type: "FAIL",
        message:
          "The agents hit an error they couldn't recover from. This is usually the AI being unreachable — check it's still running, then try again.",
      });
    } else if (taskStatus.status === "cancelled") {
      dispatch({ type: "TASK_DONE" });
    }
  }, [taskStatus, state.taskId, state.pipeline, state.step, startBuild]);

  const goAdvanced = () => setMode("advanced");

  return (
    <div className="flex h-full flex-col">
      {/* Minimal top strip */}
      <header className="glass z-10 flex items-center gap-2.5 border-b border-[var(--color-line)] px-5 py-3">
        <div className="grid h-8 w-8 place-items-center rounded-lg bg-gradient-to-br from-[var(--color-brand)] via-[var(--color-iris)] to-[var(--color-good)]">
          <Brain size={17} className="text-[var(--color-ink-950)]" />
        </div>
        <div className="leading-tight">
          <span className="font-display text-sm font-semibold gradient-text">Cognitive Loop Kernel</span>
          <span className="ml-2 text-[10px] uppercase tracking-widest text-[var(--color-mist)]">Guided</span>
        </div>
        <button onClick={goAdvanced} className="btn btn-ghost ml-auto !px-3 !py-1.5 text-xs">
          <PanelsTopLeft size={13} /> Advanced mode
        </button>
      </header>

      <main className="min-h-0 flex-1 overflow-auto">
        {state.step === "welcome" && (
          <WelcomeStep onStart={() => dispatch({ type: "GO", step: "provider" })} onAdvanced={goAdvanced} />
        )}
        {state.step === "provider" && (
          <ProviderStep
            onPick={(provider, apiKey) => {
              apiKeyRef.current = apiKey;
              dispatch({ type: "PICK_PROVIDER", provider });
            }}
            onBack={() => dispatch({ type: "BACK" })}
          />
        )}
        {state.step === "model" && state.provider && (
          <ModelStep
            provider={state.provider}
            apiKey={apiKeyRef.current}
            initial={state.model}
            onPick={(model) => dispatch({ type: "PICK_MODEL", model })}
            onBack={() => dispatch({ type: "BACK" })}
          />
        )}
        {state.step === "idea" && state.provider && (
          <IdeaStep
            provider={state.provider}
            model={state.model}
            launching={busy}
            error={state.error}
            stopWhen={state.stopWhen}
            onStopWhenChange={(v) => dispatch({ type: "SET_STOP_WHEN", stopWhen: v })}
            onLaunch={launch}
            onBack={() => dispatch({ type: "BACK" })}
          />
        )}
        {state.step === "working" && state.wsId && (
          <WorkingStep
            wsId={state.wsId}
            taskId={state.taskId}
            pipeline={state.pipeline}
            failedMessage={state.error}
            onRetry={() => void retry()}
            onBack={() => dispatch({ type: "GO", step: "idea" })}
            onNudged={(newId) =>
              dispatch({ type: "TASK_STARTED", taskId: newId, pipeline: state.pipeline ?? "build" })
            }
          />
        )}
        {state.step === "files" && state.wsId && (
          <FilesStep
            wsId={state.wsId}
            onFollowUp={() => dispatch({ type: "GO", step: "followup" })}
            onAdvanced={goAdvanced}
          />
        )}
        {state.step === "followup" && (
          <FollowUpStep
            rounds={state.rounds}
            sending={busy}
            error={state.error}
            onSend={followUp}
            onBack={() => dispatch({ type: "BACK" })}
          />
        )}
        {/* Defensive fallback: stale session pointing at a step that needs
            context we no longer have (e.g. provider cleared). */}
        {((state.step === "model" || state.step === "idea") && !state.provider) ||
        ((state.step === "working" || state.step === "files") && !state.wsId) ? (
          <WelcomeStep onStart={() => dispatch({ type: "GO", step: "provider" })} onAdvanced={goAdvanced} />
        ) : null}
      </main>
    </div>
  );
}
