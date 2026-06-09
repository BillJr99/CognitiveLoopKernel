import { useState } from "react";
import { Activity, Brain, Settings, Rocket, FolderOpen } from "lucide-react";
import { useActiveWorkspace } from "./state/activeWorkspace";
import { WorkspaceSwitcher } from "./components/WorkspaceSwitcher";
import { TopBar } from "./components/TopBar";
import { Dashboard } from "./components/dashboard/Dashboard";
import { SettingsPanel } from "./components/settings/SettingsPanel";
import { RunPanel } from "./components/compose/RunPanel";
import { ThinkStream } from "./components/think/ThinkStream";
import { FilesView } from "./components/files/FilesView";
import { Onboarding } from "./components/Onboarding";

type View = "dashboard" | "run" | "think" | "files" | "configure";

const NAV: { id: View; label: string; icon: typeof Activity }[] = [
  { id: "dashboard", label: "Dashboard", icon: Activity },
  { id: "run", label: "Run", icon: Rocket },
  { id: "think", label: "Think", icon: Brain },
  { id: "files", label: "Files", icon: FolderOpen },
  { id: "configure", label: "Configure", icon: Settings },
];

export default function App() {
  const { activeId } = useActiveWorkspace();
  const [view, setView] = useState<View>("dashboard");

  return (
    <div className="flex h-full">
      {/* Sidebar */}
      <aside className="flex w-64 shrink-0 flex-col gap-5 border-r border-[var(--color-line)] bg-[var(--color-ink-900)]/50 p-4 backdrop-blur-md">
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
                    ? "bg-[var(--color-brand)]/15 text-[var(--color-brand-bright)] shadow-[inset_0_0_0_1px_rgba(122,162,255,0.25)]"
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

        <div className="mt-auto rounded-xl bg-[var(--color-ink-800)]/50 p-3 text-[11px] leading-relaxed text-[var(--color-mist)] ring-1 ring-[var(--color-line)]">
          Watch agents think, write, and commit in real time. Configure every
          feature and <code className="text-[var(--color-brand-bright)]">.env</code> setting right here.
        </div>
      </aside>

      {/* Main */}
      <main className="flex min-w-0 flex-1 flex-col">
        <TopBar />
        <div className="min-h-0 flex-1 overflow-auto p-5">
          {/* Run is reachable without a workspace — hitting Start auto-creates
              a timestamped one. Other views need an active workspace first. */}
          {view === "run" ? (
            <RunPanel />
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
      </main>
    </div>
  );
}
