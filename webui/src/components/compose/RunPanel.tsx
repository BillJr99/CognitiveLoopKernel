import { useEffect, useRef, useState } from "react";
import { Play, Square, Lightbulb, Repeat, Wand2, Terminal, AlertTriangle } from "lucide-react";
import { useCancelTask, useCreateWorkspace, useDoctor, useSetStopWhen, useStartTask, useStopWhen, useTaskStatus, useWorkflows } from "../../api/hooks";
import { apiGet } from "../../api/client";
import { useActiveWorkspace } from "../../state/activeWorkspace";
import { Badge, Spinner } from "../common/ui";

type Mode = "run" | "loop" | "idea" | "plan";

// Banner shown when the active provider is the shell stub: it echoes prompts
// and never calls an LLM, so we block real workflows until a provider is set.
export function ShellGuardBanner() {
  return (
    <div className="card flex items-start gap-3 border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 p-4">
      <AlertTriangle size={18} className="mt-0.5 shrink-0 text-[var(--color-warn)]" />
      <div className="text-sm">
        <div className="font-semibold text-[var(--color-warn)]">
          Active provider is <code>shell</code> — a stub that never calls an LLM.
        </div>
        <div className="mt-1 text-[var(--color-mist)]">
          Workflows are disabled because <code>shell</code> only echoes prompts (no real
          model is invoked). Open <span className="font-semibold text-[var(--color-frost)]">Configure → Providers</span>,
          pick a real provider (e.g. <code>ollama</code>), set its endpoint/model, and click
          <span className="font-semibold text-[var(--color-frost)]"> make active</span>.
        </div>
      </div>
    </div>
  );
}

export function RunPanel() {
  const { activeId, setActiveId } = useActiveWorkspace();
  const { data: wfData } = useWorkflows();
  const { data: doctor } = useDoctor(activeId);
  const doctorLoaded = doctor !== undefined;
  const isShell = doctor?.active_provider === "shell";
  const start = useStartTask();
  const cancel = useCancelTask();
  const createWorkspace = useCreateWorkspace();
  // Without an active workspace, Start creates one on the fly, so the provider
  // gate (which needs a workspace's doctor) doesn't apply yet.
  const noWorkspace = !activeId;

  const [idea, setIdea] = useState("");
  const [mode, setMode] = useState<Mode>("run");
  const [stopWhen, setStopWhen] = useState("");
  const [workflow, setWorkflow] = useState("engineering");
  const [loopMode, setLoopMode] = useState<"ralph" | "autoresearch">("ralph");
  const [iterations, setIterations] = useState(5);
  const [taskId, setTaskId] = useState<string | null>(null);
  const [lines, setLines] = useState<string[]>([]);

  const { data: status } = useTaskStatus(taskId);
  const { data: stopWhenData } = useStopWhen(activeId);
  const setStopWhenMutation = useSetStopWhen(activeId);
  const logRef = useRef<HTMLDivElement>(null);
  const running = status?.status === "pending" || status?.status === "running";

  const workflows = wfData?.workflows ?? [];

  // Stream raw stdout of the active task.
  useEffect(() => {
    if (!taskId) return;
    setLines([]);
    const es = new EventSource(`/api/research/${taskId}/stream`);
    es.onmessage = (ev) => {
      try {
        const obj = JSON.parse(ev.data);
        if (obj.line !== undefined) setLines((p) => [...p, obj.line]);
        if (obj.status) es.close();
      } catch {
        /* ignore */
      }
    };
    es.onerror = () => es.close();
    return () => es.close();
  }, [taskId]);

  useEffect(() => {
    if (logRef.current) logRef.current.scrollTop = logRef.current.scrollHeight;
  }, [lines.length]);

  useEffect(() => {
    if (stopWhenData?.condition != null) {
      setStopWhen(stopWhenData.condition);
    }
  }, [stopWhenData?.condition]);

  async function launch() {
    if (start.isPending || createWorkspace.isPending) return;
    if (mode === "idea" && !idea.trim()) return;
    // With a workspace, respect the provider gate; without one, we create a
    // fresh timestamped workspace and run in it.
    let wsId = activeId;
    if (!wsId) {
      const name = new Date().toISOString().slice(0, 19).replace("T", " ");
      const ws = await createWorkspace.mutateAsync(name);
      wsId = ws.workspace_id;
      setActiveId(wsId);
      // New workspaces default to the shell stub. Honor the same gate (the
      // doctor is .env CLK_PROVIDER-aware) -- if it's truly shell, keep the
      // workspace active so the ShellGuard banner explains it, and don't run.
      try {
        const doc = await apiGet<{ active_provider: string }>(`/api/workspaces/${wsId}/doctor`);
        if (doc.active_provider === "shell") return;
      } catch {
        /* if the doctor check fails, fall through and let the run report */
      }
    } else if (!doctorLoaded || isShell) {
      return;
    }
    const body: { command: string; args?: string[]; workspace_id: string; workflow?: string } = {
      command: mode,
      workspace_id: wsId,
    };
    if (mode === "idea") {
      if (!idea.trim()) return;
      body.args = [idea.trim()];
    } else if (mode === "run") {
      body.workflow = workflow;
    } else if (mode === "loop") {
      body.args = ["--mode", loopMode, "--max-iterations", String(iterations)];
    }
    if (activeId && stopWhen.trim() !== (stopWhenData?.condition ?? "")) {
      await setStopWhenMutation.mutateAsync(stopWhen.trim() || null);
    }
    const res = await start.mutateAsync(body);
    setTaskId(res.task_id);
  }

  const modes: { id: Mode; label: string; icon: typeof Play }[] = [
    { id: "idea", label: "Set idea", icon: Lightbulb },
    { id: "run", label: "Run workflow", icon: Play },
    { id: "loop", label: "Loop", icon: Repeat },
    { id: "plan", label: "Plan", icon: Wand2 },
  ];

  return (
    <div className="mx-auto flex max-w-4xl flex-col gap-4">
      {isShell && <ShellGuardBanner />}
      <div className="card p-5">
        <h2 className="font-display mb-1 text-lg font-semibold">Kick off the agents</h2>
        <p className="mb-4 text-sm text-[var(--color-mist)]">
          Capture an idea and the chief will cast a team, then run a workflow or an iterative loop —
          watch it all unfold on the Dashboard.
        </p>

        <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
          Idea / problem statement
        </label>
        <textarea
          value={idea}
          onChange={(e) => setIdea(e.target.value)}
          rows={4}
          placeholder="e.g. Build a CLI todo app with tags, due dates, and a JSON store…"
          className="input resize-y !p-3"
        />

        {/* Mode selector */}
        <div className="mt-4 flex flex-wrap gap-2">
          {modes.map((m) => {
            const Icon = m.icon;
            const active = mode === m.id;
            return (
              <button
                key={m.id}
                onClick={() => setMode(m.id)}
                className={`flex items-center gap-2 rounded-xl border px-3 py-2 text-sm transition-colors ${
                  active
                    ? "border-[var(--color-brand)] bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)]"
                    : "border-[var(--color-line)] text-[var(--color-mist)] hover:bg-[var(--color-ink-800)]"
                }`}
              >
                <Icon size={15} />
                {m.label}
              </button>
            );
          })}
        </div>

        {/* Mode-specific options */}
        <div className="mt-3 flex flex-wrap items-end gap-3">
          {mode === "run" && (
            <Field label="Workflow">
              <select
                value={workflow}
                onChange={(e) => setWorkflow(e.target.value)}
                className="select"
              >
                {workflows.length === 0 && <option value="engineering">engineering</option>}
                {workflows.map((w) => (
                  <option key={w.name} value={w.name}>
                    {w.name}
                    {w.description ? ` — ${w.description.slice(0, 40)}` : ""}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {mode === "loop" && (
            <>
              <Field label="Loop mode">
                <select
                  value={loopMode}
                  onChange={(e) => setLoopMode(e.target.value as "ralph" | "autoresearch")}
                  className="select"
                >
                  <option value="ralph">ralph (refine)</option>
                  <option value="autoresearch">autoresearch</option>
                </select>
              </Field>
              <Field label="Iterations">
                <input
                  type="number"
                  min={1}
                  max={50}
                  value={iterations}
                  onChange={(e) => setIterations(Number(e.target.value))}
                  className="input w-24"
                />
              </Field>
            </>
          )}

          <div className="ml-auto flex items-center gap-2">
            {!running && !noWorkspace && !doctorLoaded && (
              <span className="flex items-center gap-1 text-[11px] text-[var(--color-mist)]">
                <Spinner size={11} /> checking provider…
              </span>
            )}
            {running ? (
              <button
                onClick={() => taskId && cancel.mutate(taskId)}
                className="btn btn-danger"
              >
                <Square size={15} /> Stop
              </button>
            ) : (
              <button
                onClick={launch}
                disabled={
                  start.isPending ||
                  createWorkspace.isPending ||
                  (mode === "idea" && !idea.trim()) ||
                  (!noWorkspace && (!doctorLoaded || isShell))
                }
                title={
                  noWorkspace
                    ? "Starts a new timestamped workspace"
                    : !doctorLoaded
                      ? "Checking the active provider…"
                      : isShell
                        ? "Active provider is 'shell' — pick a real provider in Configure → Providers"
                        : undefined
                }
                className="btn btn-primary !px-5"
              >
                {start.isPending ? <Spinner size={15} /> : <Play size={15} />} Start
              </button>
            )}
          </div>
        </div>

        {/* Stop condition */}
        <div className="mt-3">
          <label className="mb-1 block text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
            Stop when <span className="font-normal normal-case opacity-60">(optional — leave blank to run until max cycles)</span>
          </label>
          <input
            type="text"
            value={stopWhen}
            onChange={(e) => setStopWhen(e.target.value)}
            placeholder="e.g. all tests pass and README is complete"
            className="input w-full"
          />
        </div>

        {start.isError && (
          <div className="mt-3 rounded-lg bg-[var(--color-bad)]/10 p-2 text-sm text-[var(--color-bad)]">
            {(start.error as Error).message}
          </div>
        )}
      </div>

      {/* Raw output */}
      {taskId && (
        <div className="card flex min-h-0 flex-col">
          <div className="flex items-center gap-2 border-b border-[var(--color-line)] px-4 py-2.5">
            <Terminal size={15} className="text-[var(--color-brand)]" />
            <span className="text-sm font-semibold">Raw output</span>
            {status && (
              <Badge tone={running ? "warn" : status.status === "done" ? "good" : "bad"}>
                {running && <Spinner size={11} />} {status.status}
                {status.exit_code != null && ` · exit ${status.exit_code}`}
              </Badge>
            )}
            <span className="ml-auto text-[11px] text-[var(--color-mist)]">
              Structured view lives on the Dashboard tab.
            </span>
          </div>
          <div ref={logRef} className="max-h-80 overflow-auto p-3 font-mono text-[12px] leading-relaxed text-[var(--color-mist)]">
            {lines.length === 0 ? (
              <span className="text-[var(--color-mist)]">Waiting for output…</span>
            ) : (
              lines.map((l, i) => <div key={i} className="whitespace-pre-wrap">{l}</div>)
            )}
          </div>
        </div>
      )}
    </div>
  );
}

function Field({ label, children }: { label: string; children: React.ReactNode }) {
  return (
    <div className="flex flex-col gap-1">
      <span className="text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">{label}</span>
      {children}
    </div>
  );
}
