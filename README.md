# Cognitive Loop Kernel (CLK)

Local-only multi-agent development harness. Drop `clk` into an empty
directory, capture an idea, and let a team of agents iterate the idea
into a working system through repeated agentic development cycles. The
chief casts the team dynamically per project, the agents emit machine-
parsed `ACTION:` blocks that the harness executes, and every change is
committed automatically.

> **Experimental software — use at your own risk.**
> CLK is a research prototype. It is not intended for, and has not been
> evaluated or deemed suitable for, any particular purpose, production
> use, or critical workload. No warranty is provided, express or implied.
> By using this software you accept all associated risks.
>
> Contributions, bug reports, and ideas are very welcome — feel free to
> open an issue or pull request!

## Quick start

```bash
pip install -e ".[api]"   # from a clone; or use ./kickoff.sh from a bare clone
clk                       # launches the TUI; type your idea to begin
```

See [docs/QUICKSTART.md](docs/QUICKSTART.md) for the full walk-through
(setup wizard, `.env` defaults, CLI overrides, and the lower-level CLI),
or pick your path below.

## Documentation

| I want to…                            | Read                                             |
| ------------------------------------- | ------------------------------------------------ |
| Get running locally (Python)          | [docs/QUICKSTART.md](docs/QUICKSTART.md)         |
| Use the browser dashboard             | [docs/WEBUI.md](docs/WEBUI.md)                   |
| Run in Docker / from a bare clone     | [docs/KICKOFF.md](docs/KICKOFF.md)               |
| Drive CLK from code (REST API)        | [docs/REST_API.md](docs/REST_API.md)             |
| Chat-control from my phone            | [docs/TELEGRAM.md](docs/TELEGRAM.md)             |
| Live in the terminal UI               | [docs/TUI.md](docs/TUI.md)                       |
| Pick / configure an LLM provider      | [docs/PROVIDERS.md](docs/PROVIDERS.md)           |
| Tune cost guardrails & knobs          | [docs/CONFIGURATION.md](docs/CONFIGURATION.md)   |
| Understand missions & orchestration   | [docs/MISSIONS.md](docs/MISSIONS.md)             |
| Understand the design & layout        | [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)     |
| Run the test suites                   | [docs/TESTING.md](docs/TESTING.md)               |
| See what's new in this release        | [docs/CHANGELOG.md](docs/CHANGELOG.md)           |

## Safety

- Failed work is never silently deleted. The Ralph loop reverts via
  `git reset --hard <pre-iter-sha>`; failed agent outputs remain in
  `.clk/runs/<run_id>/`.
- Operations that touch more than 5 files are logged before execution
  (warning) and refused above 25 (configurable).
- All exceptions are logged with `[location] message` and a full
  traceback.

## License

MIT.
