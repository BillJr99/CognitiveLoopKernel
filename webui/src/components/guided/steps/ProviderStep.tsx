// Step 1: scan for usable AI providers and present them as a simple menu.
import { useState } from "react";
import { Bot, Check, ChevronRight, Cpu, Globe, KeyRound, RefreshCw, Server, Sparkles, Terminal } from "lucide-react";
import { useDiscoverProviders, useProbeModels } from "../../../api/hooks";
import type { DiscoveredProvider } from "../../../api/types";
import { Spinner } from "../../common/ui";
import { StepShell } from "../StepShell";
import { providerMeta } from "../friendly";

const ICONS: Record<string, typeof Bot> = {
  ollama: Server,
  openwebui: Globe,
  claude: Sparkles,
  codex: Cpu,
  gemini: Bot,
  pi: Terminal,
};

export function ProviderStep({
  onPick,
  onBack,
}: {
  onPick: (provider: DiscoveredProvider, apiKey: string) => void;
  onBack: () => void;
}) {
  const { data, isLoading, isFetching, refetch } = useDiscoverProviders();
  const probe = useProbeModels();
  const [expanded, setExpanded] = useState<string | null>(null);
  const [keyDraft, setKeyDraft] = useState("");
  const [keyError, setKeyError] = useState<string | null>(null);

  const providers = data?.providers ?? [];
  const anyAvailable = providers.some((p) => p.available);

  async function choose(p: DiscoveredProvider) {
    setKeyError(null);
    // Reachable OpenWebUI that refused the model list: ask for its key first.
    if (p.available && p.needs_api_key) {
      setExpanded(expanded === p.name ? null : p.name);
      setKeyDraft("");
      return;
    }
    if (p.available) {
      onPick(p, "");
      return;
    }
    // Unavailable key-capable provider: offer the "I have an API key" path.
    if (p.api_key_env) {
      setExpanded(expanded === p.name ? null : p.name);
      setKeyDraft("");
    }
  }

  async function submitKey(p: DiscoveredProvider) {
    const key = keyDraft.trim();
    if (!key) return;
    setKeyError(null);
    if (p.name === "openwebui") {
      // Re-probe with the key so the model menu fills in.
      try {
        const res = await probe.mutateAsync({ type: "openwebui", endpoint: p.endpoint ?? undefined, api_key: key });
        if (!res.models?.length) {
          setKeyError("That key didn't unlock any models. Double-check it and try again.");
          return;
        }
        onPick({ ...p, models: res.models, endpoint: res.endpoint ?? p.endpoint, needs_api_key: false }, key);
      } catch (e) {
        setKeyError((e as Error).message || "Couldn't reach the server with that key. Try again.");
      }
      return;
    }
    onPick({ ...p, available: true, mode: "api" }, key);
  }

  return (
    <StepShell
      step="provider"
      title="Choose your AI"
      subtitle="We scanned your machine for ways to run an AI. Pick one — you can always change it later."
      onBack={onBack}
      wide
    >
      <div className="flex items-center justify-center">
        <button onClick={() => refetch()} disabled={isFetching} className="btn btn-ghost !py-1.5 text-xs">
          <RefreshCw size={13} className={isFetching ? "animate-spin" : ""} />
          {isFetching ? "Scanning…" : "Scan again"}
        </button>
      </div>

      {isLoading ? (
        <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
          {[0, 1, 2, 3].map((i) => (
            <div key={i} className="shimmer h-28 rounded-2xl" />
          ))}
        </div>
      ) : (
        <>
          {!anyAvailable && (
            <div className="card border-[var(--color-warn)]/40 bg-[var(--color-warn)]/10 p-4 text-sm text-[var(--color-mist)]">
              <span className="font-semibold text-[var(--color-warn)]">Nothing found yet.</span>{" "}
              The easiest free option is <span className="font-semibold text-[var(--color-frost)]">Ollama</span>:
              install it from{" "}
              <a href="https://ollama.com" target="_blank" rel="noreferrer" className="text-[var(--color-brand-bright)] underline underline-offset-2">
                ollama.com
              </a>
              , run <code className="font-mono text-[var(--color-frost)]">ollama pull llama3.1</code>, then hit
              “Scan again”. Or pick a provider below and paste an API key.
            </div>
          )}

          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            {providers.map((p) => {
              const meta = providerMeta(p);
              const Icon = ICONS[p.name] ?? Bot;
              const isOpen = expanded === p.name;
              const keyable = (!p.available && !!p.api_key_env) || (p.available && p.needs_api_key);
              return (
                <div
                  key={p.name}
                  className={`card text-left transition-all ${
                    p.available ? "card-hover cursor-pointer" : keyable ? "cursor-pointer opacity-90" : "opacity-50"
                  } ${isOpen ? "border-[var(--color-brand)]/50" : ""}`}
                  onClick={() => (p.available || keyable ? choose(p) : undefined)}
                  role="button"
                  tabIndex={p.available || keyable ? 0 : -1}
                  onKeyDown={(e) => {
                    if ((e.key === "Enter" || e.key === " ") && (p.available || keyable)) {
                      e.preventDefault();
                      choose(p);
                    }
                  }}
                >
                  <div className="flex items-start gap-3 p-4">
                    <div
                      className={`grid h-11 w-11 shrink-0 place-items-center rounded-xl bg-gradient-to-br ${meta.hue} text-[var(--color-ink-950)] shadow-lg`}
                    >
                      <Icon size={22} />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center gap-2">
                        <span className="font-display text-sm font-semibold">{p.label}</span>
                        {p.available ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-[var(--color-good)]/30 bg-[var(--color-good)]/15 px-2 py-0.5 text-[10px] font-semibold text-[var(--color-good)]">
                            <Check size={10} /> Ready
                          </span>
                        ) : keyable ? (
                          <span className="inline-flex items-center gap-1 rounded-full border border-[var(--color-warn)]/30 bg-[var(--color-warn)]/10 px-2 py-0.5 text-[10px] font-semibold text-[var(--color-warn)]">
                            <KeyRound size={10} /> Needs a key
                          </span>
                        ) : (
                          <span className="rounded-full border border-[var(--color-line)] px-2 py-0.5 text-[10px] text-[var(--color-mist)]">
                            Not found
                          </span>
                        )}
                      </div>
                      <p className="mt-1 text-xs leading-relaxed text-[var(--color-mist)]">{meta.tagline}</p>
                      {p.available && p.kind === "http" && p.models.length > 0 && (
                        <p className="mt-1 text-[11px] text-[var(--color-good)]/80">
                          {p.models.length} model{p.models.length === 1 ? "" : "s"} installed
                          {p.endpoint ? ` · ${p.endpoint.replace(/^https?:\/\//, "")}` : ""}
                        </p>
                      )}
                      {!p.available && <p className="mt-1 text-[11px] leading-relaxed text-[var(--color-mist)]/80">{meta.fixHint}</p>}
                    </div>
                    {p.available && !p.needs_api_key && (
                      <ChevronRight size={16} className="mt-1 shrink-0 text-[var(--color-mist)]" />
                    )}
                  </div>

                  {/* Inline API-key entry */}
                  {isOpen && keyable && (
                    <div
                      className="border-t border-[var(--color-line)] p-4"
                      onClick={(e) => e.stopPropagation()}
                      onKeyDown={(e) => e.stopPropagation()}
                    >
                      <label className="mb-1.5 block text-[11px] font-semibold uppercase tracking-wide text-[var(--color-mist)]">
                        {p.api_key_env ? `Paste your ${p.label} API key` : "Paste your server's API key"}
                      </label>
                      <div className="flex items-center gap-2">
                        <input
                          type="password"
                          autoFocus
                          value={keyDraft}
                          onChange={(e) => setKeyDraft(e.target.value)}
                          onKeyDown={(e) => e.key === "Enter" && submitKey(p)}
                          placeholder="sk-…"
                          className="input flex-1"
                        />
                        <button
                          onClick={() => submitKey(p)}
                          disabled={!keyDraft.trim() || probe.isPending}
                          className="btn btn-primary !py-2"
                        >
                          {probe.isPending ? <Spinner size={14} /> : <Check size={14} />} Use it
                        </button>
                      </div>
                      <p className="mt-2 text-[11px] text-[var(--color-mist)]">
                        Stored privately in your local <code className="font-mono">.env</code> file — it never leaves this machine
                        except to talk to {p.label}.
                      </p>
                      {keyError && <p className="mt-2 text-[11px] text-[var(--color-bad)]">{keyError}</p>}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </>
      )}
    </StepShell>
  );
}
