import { Bot, FileCode2, MessageSquareText } from "lucide-react";
import type { AgentCard as Card } from "../../api/types";
import { Badge } from "../common/ui";
import { AnimatedNumber } from "../common/AnimatedNumber";
import { fmtTokens, fmtUsd } from "../../lib/format";

const STATUS: Record<Card["status"], { tone: "good" | "warn" | "bad" | "brand" | "muted"; label: string }> = {
  idle: { tone: "muted", label: "idle" },
  working: { tone: "brand", label: "working" },
  recovering: { tone: "warn", label: "recovering" },
  done: { tone: "good", label: "done" },
  failed: { tone: "bad", label: "failed" },
  provider: { tone: "bad", label: "provider error" },
};

export function AgentCard({ card, peak }: { card: Card; peak: number }) {
  const s = STATUS[card.status] ?? STATUS.idle;
  const working = card.status === "working";
  const meter = peak > 0 ? Math.min(100, Math.round((card.last_run_tokens / peak) * 100)) : 0;

  return (
    <div className={`card card-hover float-in relative overflow-hidden p-4 ${working ? "working-ring" : ""}`}>
      {/* status accent bar */}
      <div
        className="absolute inset-x-0 top-0 h-0.5"
        style={{ background: accentFor(card.status) }}
      />
      <div className="flex items-start justify-between gap-2">
        <div className="flex items-center gap-2">
          <div className={`grid h-8 w-8 place-items-center rounded-lg bg-[var(--color-ink-700)] ${working ? "ring-1 ring-[var(--color-brand)]/60" : ""}`}>
            <Bot size={16} className="text-[var(--color-brand-bright)]" />
          </div>
          <div className="leading-tight">
            <div className="text-sm font-semibold">{card.name}</div>
            {card.role && <div className="max-w-[180px] truncate text-[11px] text-[var(--color-mist)]">{card.role}</div>}
          </div>
        </div>
        <Badge tone={s.tone}>{s.label}</Badge>
      </div>

      {/* Activity meter */}
      <div className="relative mt-3 h-1.5 overflow-hidden rounded-full bg-[var(--color-ink-700)]">
        {working ? (
          <div className="activity-sweep absolute inset-0" />
        ) : (
          <div
            className="h-full rounded-full bg-gradient-to-r from-[var(--color-brand)] to-[var(--color-good)] transition-all"
            style={{ width: `${meter}%` }}
          />
        )}
      </div>

      <div className="mt-3 grid grid-cols-3 gap-2 text-center">
        <Metric label="runs" value={<AnimatedNumber value={card.runs} />} />
        <Metric label="tokens" value={<AnimatedNumber value={card.tokens_total} format={fmtTokens} />} />
        <Metric label="cost" value={<AnimatedNumber value={card.usd} format={fmtUsd} />} />
      </div>

      {card.last_thought && (
        <div className="mt-3 flex items-start gap-1.5 rounded-lg bg-[var(--color-ink-900)]/60 p-2 text-[11px] text-[var(--color-mist)]">
          <MessageSquareText size={13} className="mt-0.5 shrink-0 text-[var(--color-brand)]" />
          <span className="line-clamp-2">{card.last_thought}</span>
        </div>
      )}

      {card.last_error && (
        <div className="mt-2 truncate rounded-lg bg-[var(--color-bad)]/10 p-2 text-[11px] text-[var(--color-bad)]" title={card.last_error}>
          {card.error_kind ? `[${card.error_kind}] ` : ""}{card.last_error}
        </div>
      )}

      {card.provider && (
        <div className="mt-2 flex items-center justify-between text-[11px] text-[var(--color-mist)]">
          <span>{card.provider}</span>
          {card.files.length > 0 && (
            <span className="flex items-center gap-1"><FileCode2 size={12} /> {card.files.length}</span>
          )}
        </div>
      )}
    </div>
  );
}

function accentFor(status: Card["status"]): string {
  switch (status) {
    case "working":
      return "linear-gradient(90deg, var(--color-brand), var(--color-iris))";
    case "done":
      return "var(--color-good)";
    case "recovering":
      return "var(--color-warn)";
    case "failed":
    case "provider":
      return "var(--color-bad)";
    default:
      return "transparent";
  }
}

function Metric({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="rounded-lg bg-[var(--color-ink-900)]/50 py-1.5">
      <div className="text-sm font-semibold tabular-nums">{value}</div>
      <div className="text-[10px] uppercase tracking-wide text-[var(--color-mist)]">{label}</div>
    </div>
  );
}
