import { Cpu, DollarSign, FileCode2, GitCommitHorizontal, Hash, Zap } from "lucide-react";
import { useSnapshot, useWorkspaces } from "../api/hooks";
import { useActiveWorkspace } from "../state/activeWorkspace";
import { useSharedActivity } from "../state/activity";
import { Badge, Spinner } from "./common/ui";
import { AnimatedNumber } from "./common/AnimatedNumber";
import { fmtTokens, fmtUsd } from "../lib/format";

export function TopBar() {
  const { activeId } = useActiveWorkspace();
  const { data: ws } = useWorkspaces();
  const { data } = useSnapshot(activeId);
  const { connected } = useSharedActivity();
  const snap = data?.snapshot;
  const name = ws?.workspaces.find((w) => w.id === activeId)?.name ?? "—";
  const totals = snap?.totals;

  return (
    <header className="flex flex-wrap items-center gap-x-6 gap-y-2 border-b border-[var(--color-line)] bg-[var(--color-ink-900)]/60 px-5 py-3 backdrop-blur-md">
      <div className="flex items-center gap-3">
        <div className="text-base font-semibold tracking-tight">{name}</div>
        {activeId && (
          <span className="flex items-center gap-1.5 text-[11px] text-[var(--color-mist)]">
            <span className="relative flex h-2 w-2">
              {connected && (
                <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-[var(--color-good)] opacity-50" />
              )}
              <span
                className={`relative inline-flex h-2 w-2 rounded-full ${
                  connected ? "bg-[var(--color-good)]" : "bg-[var(--color-mist)]"
                }`}
              />
            </span>
            {connected ? "live" : "connecting"}
          </span>
        )}
        {snap?.provider && <Badge tone="brand"><Cpu size={12} /> {snap.provider}</Badge>}
        {snap?.phase && <Badge tone="muted">{snap.phase}</Badge>}
        {snap?.busy ? (
          <Badge tone="warn"><Spinner size={11} /> working</Badge>
        ) : (
          <Badge tone="good">idle</Badge>
        )}
      </div>

      <div className="ml-auto flex items-center divide-x divide-[var(--color-line)]/60 [&>*]:px-5 first:[&>*]:pl-0 last:[&>*]:pr-0">
        <MiniStat label="Tokens" icon={<Zap size={14} className="text-[var(--color-warn)]" />}>
          <AnimatedNumber value={totals?.total_tokens ?? 0} format={fmtTokens} />
        </MiniStat>
        <MiniStat label="Est. cost" icon={<DollarSign size={14} className="text-[var(--color-good)]" />}>
          <AnimatedNumber value={totals?.total_usd ?? 0} format={fmtUsd} />
        </MiniStat>
        <MiniStat label="Files" icon={<FileCode2 size={14} className="text-[var(--color-brand)]" />}>
          <AnimatedNumber value={totals?.total_files ?? 0} />
        </MiniStat>
        <MiniStat label="Commits" icon={<GitCommitHorizontal size={14} className="text-[var(--color-mist)]" />}>
          <AnimatedNumber value={totals?.commits ?? 0} />
        </MiniStat>
        <MiniStat label="Events" icon={<Hash size={14} className="text-[var(--color-mist)]" />}>
          <AnimatedNumber value={snap?.event_count ?? 0} />
        </MiniStat>
      </div>
    </header>
  );
}

function MiniStat({ label, icon, children }: { label: string; icon: React.ReactNode; children: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <span className="text-[10px] uppercase tracking-wider text-[var(--color-mist)]">{label}</span>
      <span className="flex items-center gap-1 text-lg font-semibold tabular-nums">
        {icon}
        {children}
      </span>
    </div>
  );
}
