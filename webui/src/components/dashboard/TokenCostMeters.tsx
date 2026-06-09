import { Bar, BarChart, Cell, ResponsiveContainer, Tooltip, XAxis } from "recharts";
import type { Snapshot } from "../../api/types";
import { fmtTokens, fmtUsd } from "../../lib/format";

const COLORS = ["#7aa2ff", "#4ade80", "#fbbf24", "#fb7185", "#a9c2ff"];

export function TokenCostMeters({ snap }: { snap: Snapshot }) {
  const perAgent = Object.values(snap.agents)
    .map((a) => ({ name: a.name, tokens: a.tokens_total, usd: a.usd }))
    .filter((a) => a.tokens > 0)
    .sort((a, b) => b.tokens - a.tokens);

  const perProvider = Object.entries(snap.totals.cost_per_provider).map(([name, usd]) => ({ name, usd }));

  return (
    <div className="card flex flex-col gap-3 p-4">
      <div className="flex items-center justify-between">
        <span className="text-sm font-semibold">Token & cost breakdown</span>
        <span className="text-[11px] text-[var(--color-mist)]">
          {fmtTokens(snap.totals.total_tokens)} tokens · {fmtUsd(snap.totals.total_usd)}
        </span>
      </div>

      {perAgent.length === 0 ? (
        <div className="grid h-28 place-items-center text-sm text-[var(--color-mist)]">
          No token usage recorded yet.
        </div>
      ) : (
        <div className="h-36 w-full">
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={perAgent} margin={{ top: 6, right: 6, bottom: 0, left: 6 }}>
              <XAxis dataKey="name" tick={{ fill: "#9fb0d9", fontSize: 11 }} axisLine={false} tickLine={false} />
              <Tooltip
                cursor={{ fill: "rgba(122,162,255,0.08)" }}
                contentStyle={{ background: "#0b1020", border: "1px solid #263056", borderRadius: 10, fontSize: 12 }}
                formatter={(v: number, k) => (k === "tokens" ? [fmtTokens(v), "tokens"] : [fmtUsd(v), "cost"])}
              />
              <Bar dataKey="tokens" radius={[6, 6, 0, 0]}>
                {perAgent.map((_, i) => (
                  <Cell key={i} fill={COLORS[i % COLORS.length]} />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        </div>
      )}

      {perProvider.length > 0 && (
        <div className="flex flex-wrap gap-2">
          {perProvider.map((p, i) => (
            <span
              key={p.name}
              className="flex items-center gap-1.5 rounded-full bg-[var(--color-ink-900)]/60 px-2.5 py-1 text-[11px]"
            >
              <span className="h-2 w-2 rounded-full" style={{ background: COLORS[i % COLORS.length] }} />
              {p.name}: {fmtUsd(p.usd)}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}
