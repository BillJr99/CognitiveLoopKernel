import { useState } from "react";
import { ArrowRight, Brain, FileCog, Rocket, Sparkles, Workflow } from "lucide-react";
import { useCreateWorkspace } from "../api/hooks";
import { useActiveWorkspace } from "../state/activeWorkspace";
import { Spinner } from "./common/ui";

// A warm, one-click first-run experience: name a project and you're in.
export function Onboarding() {
  const create = useCreateWorkspace();
  const { setActiveId } = useActiveWorkspace();
  const [name, setName] = useState("");

  async function go() {
    const res = await create.mutateAsync(name.trim() || "my-first-project");
    setActiveId(res.workspace_id);
  }

  return (
    <div className="float-in mx-auto flex max-w-2xl flex-col items-center gap-6 py-10 text-center">
      <div className="relative">
        <div className="grid h-20 w-20 place-items-center rounded-3xl bg-gradient-to-br from-[var(--color-brand)] via-[var(--color-iris)] to-[var(--color-good)] shadow-[0_20px_60px_-12px_rgba(122,162,255,0.7)]">
          <Brain size={40} className="text-[var(--color-ink-950)]" />
        </div>
        <Sparkles className="absolute -right-2 -top-2 text-[var(--color-warn)]" size={22} />
      </div>

      <div className="space-y-2">
        <h1 className="text-3xl font-bold tracking-tight">
          Welcome to <span className="gradient-text">Cognitive Loop Kernel</span>
        </h1>
        <p className="max-w-lg text-[var(--color-mist)]">
          Spin up a team of AI agents that plan, build, test, and commit — all in your browser.
          Name a project to begin and watch the loop come alive in real time.
        </p>
      </div>

      <div className="flex w-full max-w-md items-center gap-2">
        <input
          autoFocus
          value={name}
          onChange={(e) => setName(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && go()}
          placeholder="my-first-project"
          className="min-w-0 flex-1 rounded-xl border border-[var(--color-line)] bg-[var(--color-ink-900)] px-4 py-3 text-sm outline-none transition-colors focus:border-[var(--color-brand)]"
        />
        <button
          onClick={go}
          disabled={create.isPending}
          className="flex shrink-0 items-center gap-2 rounded-xl bg-gradient-to-r from-[var(--color-brand)] to-[var(--color-iris)] px-5 py-3 text-sm font-semibold text-[var(--color-ink-950)] transition-transform hover:scale-[1.03] disabled:opacity-50"
        >
          {create.isPending ? <Spinner size={16} /> : <Rocket size={16} />}
          Create workspace
          {!create.isPending && <ArrowRight size={16} />}
        </button>
      </div>

      <div className="mt-4 grid w-full grid-cols-1 gap-3 sm:grid-cols-3">
        <Feature icon={<Workflow size={18} />} title="Kick off workflows" body="Capture an idea; the chief casts a team and runs it." />
        <Feature icon={<Sparkles size={18} />} title="Watch live" body="Agent cards, a timeline, and token/cost meters update in real time." />
        <Feature icon={<FileCog size={18} />} title="Configure everything" body="Every feature and .env setting, edited from the browser." />
      </div>
    </div>
  );
}

function Feature({ icon, title, body }: { icon: React.ReactNode; title: string; body: string }) {
  return (
    <div className="card card-hover p-4 text-left">
      <div className="mb-2 inline-grid h-9 w-9 place-items-center rounded-lg bg-[var(--color-ink-700)] text-[var(--color-brand-bright)]">
        {icon}
      </div>
      <div className="text-sm font-semibold">{title}</div>
      <div className="mt-1 text-xs leading-relaxed text-[var(--color-mist)]">{body}</div>
    </div>
  );
}
