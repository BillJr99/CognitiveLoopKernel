// First screen of guided mode: what this is, in one breath.
import { ArrowRight, Brain, FolderDown, MessagesSquare, Sparkles, Users, Wand2 } from "lucide-react";

export function WelcomeStep({
  onStart,
  onAdvanced,
}: {
  onStart: () => void;
  onAdvanced: () => void;
}) {
  return (
    <div className="step-in mx-auto flex min-h-full w-full max-w-3xl flex-col items-center justify-center gap-8 px-6 py-12 text-center">
      <div className="relative">
        <div className="grid h-24 w-24 place-items-center rounded-[28px] bg-gradient-to-br from-[var(--color-brand)] via-[var(--color-iris)] to-[var(--color-good)] shadow-[0_24px_70px_-14px_rgba(122,162,255,0.75)]">
          <Brain size={48} className="text-[var(--color-ink-950)]" />
        </div>
        <Sparkles className="absolute -right-3 -top-3 text-[var(--color-warn)]" size={26} />
      </div>

      <div className="space-y-3">
        <h1 className="font-display text-4xl font-bold tracking-tight sm:text-5xl">
          Build anything with <span className="gradient-text">a team of AI agents</span>
        </h1>
        <p className="mx-auto max-w-xl text-base leading-relaxed text-[var(--color-mist)]">
          Pick an AI, describe your idea in plain words, and watch a small team plan it, build it,
          and hand you the files. No setup knowledge needed — we'll walk you through it.
        </p>
      </div>

      <div className="flex flex-col items-center gap-3">
        <button onClick={onStart} className="btn btn-primary !px-7 !py-3 !text-base">
          Get started <ArrowRight size={18} />
        </button>
        <button
          onClick={onAdvanced}
          className="text-xs text-[var(--color-mist)] underline-offset-4 transition-colors hover:text-[var(--color-frost)] hover:underline"
        >
          I know what I'm doing — take me to the full console
        </button>
      </div>

      <div className="mt-2 grid w-full grid-cols-1 gap-3 sm:grid-cols-3">
        <HowCard
          icon={<Wand2 size={18} />}
          step="1"
          title="Describe it"
          body="A website, a script, a plan — anything. Plain English works."
        />
        <HowCard
          icon={<Users size={18} />}
          step="2"
          title="A team builds it"
          body="A lead agent assembles specialists who write, test, and save the work."
        />
        <HowCard
          icon={<FolderDown size={18} />}
          step="3"
          title="Get the files"
          body="Browse or download everything, then ask for changes until it's right."
        />
      </div>

      <div className="flex items-center gap-2 text-[11px] text-[var(--color-mist)]">
        <MessagesSquare size={13} />
        Everything runs on your machine — switch to the full console any time.
      </div>
    </div>
  );
}

function HowCard({ icon, step, title, body }: { icon: React.ReactNode; step: string; title: string; body: string }) {
  return (
    <div className="card-lux card-hover p-4 text-left">
      <div className="mb-2 flex items-center gap-2">
        <div className="inline-grid h-9 w-9 place-items-center rounded-lg bg-[var(--color-ink-700)] text-[var(--color-brand-bright)]">
          {icon}
        </div>
        <span className="text-[10px] font-bold uppercase tracking-widest text-[var(--color-mist)]">Step {step}</span>
      </div>
      <div className="font-display text-sm font-semibold">{title}</div>
      <div className="mt-1 text-xs leading-relaxed text-[var(--color-mist)]">{body}</div>
    </div>
  );
}
