export function fmtUsd(amount: number): string {
  if (!amount || amount <= 0) return "$0.00";
  if (amount < 0.01) return `$${amount.toFixed(3)}`;
  return `$${amount.toFixed(2)}`;
}

export function fmtTokens(n: number): string {
  if (n >= 1_000_000) return `${(n / 1_000_000).toFixed(1)}M`;
  if (n >= 1_000) return `${(n / 1_000).toFixed(1)}k`;
  return `${n}`;
}

export function shortTime(ts: string): string {
  if (!ts) return "";
  // Activity timestamps are naive local ISO (no Z). Show just HH:MM:SS.
  const m = ts.match(/(\d{2}:\d{2}:\d{2})/);
  if (m) return m[1];
  try {
    return new Date(ts).toLocaleTimeString();
  } catch {
    return ts;
  }
}
