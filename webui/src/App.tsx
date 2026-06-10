import { useState } from "react";
import { Activity, Brain, FolderOpen, Rocket, ScrollText, Settings, Sparkles } from "lucide-react";
import { useWorkspaces, useWorkspaceFiles } from "./api/hooks";
import { useActiveWorkspace } from "./state/activeWorkspace";
import { useUiMode } from "./state/uiMode";
import { WorkspaceSwitcher } from "./components/WorkspaceSwitcher";
import { TopBar } from "./components/TopBar";
import { Dashboard } from "./components/dashboard/Dashboard";
import { SettingsPanel } from "./components/settings/SettingsPanel";
import { RunPanel } from "./components/compose/RunPanel";
import { ThinkStream } from "./components/think/ThinkStream";
import { FilesView } from "./components/files/FilesView";
import { LogView } from "./components/log/LogView";
import { Onboarding } from "./components/Onboarding";
import { GuidedMode } from "./components/guided/GuidedMode";

type View = "dashboard" | "run" | "think" | "files" | "log" | "configure";

const NAV: { id: View; label: string; icon: typeof Activity }[] = [
  { id: "dashboard", label: "Dashboard", icon: Activity },
  { id: "run", label: "Run", icon: Rocket },
  { id: "think", label: "Think", icon: Brain },
  { id: "files", label: "Files", icon: FolderOpen },
  { id: "log", label: "Log", icon: ScrollText },
  { id: "configure", label: "Configure", icon: Settings },
];

export default function App() {
  const { activeId } = useActiveWorkspace();
  const { mode, setMode } = useUiMode();
  const { data: wsData } = useWorkspaces();
  const [view, setView] = useState<View>("dashboard");
  // Keep the files cache warm at all times so the Files tab shows live data.
  useWorkspaceFiles(activeId);

  // No stored preference yet: newcomers (no workspaces) land in the guided
  // wizard; returning users go straight to the console they know.
  const effectiveMode =
    mode ?? (wsData === undefined ? null : wsData.workspaces.length === 0 ? "guided" : "advanced");

  if (effectiveMode === null) {
    return <div className="h-full" />; // one frame while the workspace list loads
  }

  if (effectiveMode === "guided") {
    return <GuidedMode />;
  }

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="flex w-60 shrink-0 flex-col gap-5 border-r border-[var(--color-line)] bg-[var(--color-ink-900)]/50 p-4 backdrop-blur-md">
        <div className="flex items-center gap-2.5 px-1">
          <div className="grid h-10 w-10 place-items-center rounded-xl bg-gradient-to-br from-[var(--color-brand)] via-[var(--color-iris)] to-[var(--color-good)] shadow-[0_8px_24px_-6px_rgba(122,162,255,0.7)]">
            <Brain size={21} className="text-[var(--color-ink-950)]" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold gradient-text">Cognitive Loop</div>
            <div className="text-[11px] tracking-wide text-[var(--color-mist)]">Kernel · web console</div>
          </div>
        </div>

        <WorkspaceSwitcher />

        <nav className="flex flex-col gap-1">
          {NAV.map((n) => {
            const Icon = n.icon;
            const active = view === n.id;
            return (
              <button
                key={n.id}
                onClick={() => setView(n.id)}
                className={`group relative flex items-center gap-3 rounded-xl px-3 py-2 text-sm transition-all ${
                  active
                    ? "bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)] shadow-[inset_0_0_0_1px_rgba(122,162,255,0.25),0_8px_24px_-12px_rgba(122,162,255,0.55)]"
                    : "text-[var(--color-mist)] hover:bg-[var(--color-ink-800)] hover:text-[var(--color-frost)]"
                }`}
              >
                {active && (
                  <span className="absolute left-0 top-1/2 h-5 w-1 -translate-y-1/2 rounded-r-full bg-gradient-to-b from-[var(--color-brand)] to-[var(--color-iris)]" />
                )}
                <Icon size={18} className="transition-transform group-hover:scale-110" />
                {n.label}
              </button>
            );
          })}
        </nav>

        <div className="mt-auto flex flex-col gap-2">
          <button
            onClick={() => setMode("guided")}
            title="A step-by-step wizard: pick an AI, describe your idea, get files back"
            className="flex items-center gap-2.5 rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-800)]/50 px-3 py-2.5 text-left text-sm text-[var(--color-mist)] transition-all hover:border-[var(--color-line-bright)] hover:text-[var(--color-frost)]"
          >
            <Sparkles size={16} className="shrink-0 text-[var(--color-iris)]" />
            <span>
              <span className="block font-semibold">Guided mode</span>
              <span className="block text-[10px] leading-tight opacity-80">Step-by-step, beginner friendly</span>
            </span>
          </button>
        </div>
      </aside>

      {/* Main */}
      <main className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <div className="min-h-0 flex-1 overflow-auto p-5">
          {/* Run is reachable without a workspace — hitting Start auto-creates
              a timestamped one. Other views need an active workspace first. */}
          <div key={view} className="fade-up flex h-full min-h-0 flex-col">
            {view === "run" ? (
              <RunPanel />
            ) : view === "log" ? (
              <LogView />
            ) : !activeId ? (
              <Onboarding />
            ) : view === "dashboard" ? (
              <Dashboard />
            ) : view === "think" ? (
              <ThinkStream />
            ) : view === "files" ? (
              <FilesView />
            ) : (
              <SettingsPanel />
            )}
          </div>
        </div>
      </main>
    </div>
  );
}
