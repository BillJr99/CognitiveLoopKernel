# Configuration

Part of the [CLK documentation](../README.md). Cost guardrails, customization points, and dry-run mode. Provider/`.env` setup lives in [QUICKSTART](QUICKSTART.md), [WEBUI](WEBUI.md), and [KICKOFF](KICKOFF.md).

## Cost guardrails

Title-bar dollar cost is computed from the per-provider table in
`clk_harness/pricing.py`:

| Provider | Default $/1k in | Default $/1k out |
|---|---|---|
| claude (sonnet-4-5)  | $0.003   | $0.015  |
| claude (haiku-latest)| $0.0008  | $0.004  |
| claude (opus-latest) | $0.015   | $0.075  |
| codex (gpt-4o)       | $0.0025  | $0.010  |
| codex (gpt-4o-mini)  | $0.00015 | $0.0006 |
| codex (o1)           | $0.015   | $0.060  |
| gemini (1.5-pro)     | $0.00125 | $0.005  |
| gemini (1.5-flash)   | $0.000075| $0.0003 |
| pi                   | $0.003   | $0.015  (blended default; override per route) |
| ollama / shell       | $0.00    | $0.00   |

**Override per project** by adding to `.clk/config/providers.json`:

```jsonc
"providers": {
  "pi": {
    "type": "pi",
    "pricing": { "input_per_1k": 0.002, "output_per_1k": 0.008 }
  }
}
```

Or per model:

```jsonc
"pricing_by_model": { "openrouter/free": { "input_per_1k": 0.0, "output_per_1k": 0.0 } }
```

`/status` prints the per-provider breakdown so you can see which
provider is eating the budget. Updated lazily from the same numbers
the title bar shows.

### Robustness-loop multipliers

The robustness loops (see **Robustness loops**) trade tokens for
quality. Use this table to pick a regime:

| Knob                                   | Worst-case multiplier per affected dispatch                              | Recommended starting point |
|----------------------------------------|--------------------------------------------------------------------------|----------------------------|
| `robustness.auto_consensus`            | `off` → ×1; `on_careful` → ×(N+1) on careful stages only; `always` → ×(N+1) on every dispatch (where N = `consensus.max_samples`, default 6) | `on_careful` (default)     |
| `robustness.auto_refine`               | `off` → ×1; `careful_only` → ×(1 + 1 worker revision + 1 critic) on careful stages; `all` → that on every producing stage | `all` (default — every producing stage refines) |
| `robustness.max_quality_retries`       | At most this many extra dispatches when a response fails the quality check; 0 disables | 4 (default)                |
| `robustness.refine_max_rounds`         | Cap on critic↔worker round-trips inside a refine loop                    | 10 (default)               |
| `robustness.debate`                    | `off` → ×1; `careful_only` → an adversarial panel (one critic per lens) on careful stages; `all` → on every producing stage | `careful_only` (default)   |
| `robustness.debate_lenses`             | Adversarial lenses (one parallel critic each) — more lenses = more critic dispatches per round | `[correctness, security, simplicity]` |
| `robustness.debate_max_rounds`         | Cap on debate rounds (panel critique + worker revision) per stage        | 2 (default)                |
| `robustness.max_qa_depth`              | Cap on inter-agent Q&A chain depth (each peer answer can ask one peer)   | 3 (default)                |
| `robustness.max_delegate_depth`        | Cap on DELEGATE sub-agent nesting; 1 = a worker may spawn one isolated child, but that child cannot itself delegate (each level can add a bounded child dispatch) | 1 (default)                |
| `robustness.plateau_window`            | How many no-improvement Ralph/autoresearch iterations before escalation  | 3 (default)                |
| `robustness.plateau_action`            | `off` disables adaptive loop termination entirely                        | `escalate_then_reframe`    |

Cost-minimal regime (closest to legacy CLK behavior, no extra tokens):

```jsonc
"robustness": {
  "auto_consensus": "off",
  "auto_refine": "off",
  "max_quality_retries": 0,
  "plateau_action": "off"
}
```

Cost-maximal "lean into the loop" regime (every dispatch fans out,
critic gates every careful stage, plateau detection on, Q&A protocol
fully open):

```jsonc
"robustness": {
  "auto_consensus": "always",
  "auto_refine": "all",
  "max_quality_retries": 3,
  "refine_max_rounds": 4,
  "plateau_action": "escalate_then_reframe"
}
```
## Customization

- Edit prompts in `.clk/prompts/` to change agent behavior.
- Edit `.clk/config/agents.json` to bind specific agents to specific
  providers (e.g. `engineer` -> `claude`, `researcher` -> `ollama`).
- Edit `.clk/config/workflows/*.yaml` to add new stages or new
  workflows. Reference any new workflow with `clk run --workflow NAME`.
- `clk configure --set key=value` updates `.clk/config/clk.config.json`.
## Dry-run mode

Every loop and workflow command accepts `--dry-run`. Providers honor it
and skip side effects. Use it to preview prompt rendering and stage
ordering without writing files or committing.
