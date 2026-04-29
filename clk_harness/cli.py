"""CLK command-line interface.

Sub-commands:
  init        - bootstrap .clk/, configs, prompts, workflows, git repo
  idea        - capture an idea
  plan        - run the discovery + product workflows
  run         - run a single development cycle (engineering workflow by default)
  loop        - repeat the Ralph (or autoresearch) loop
  status      - print harness status and recent activity
  providers   - list providers and availability
  configure   - edit configuration interactively or via --set key=value
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import textwrap
import traceback
import venv
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .config import (
    DEFAULT_CLK_CONFIG,
    Paths,
    is_initialized,
    load_agents_config,
    load_clk_config,
    load_providers_config,
    project_paths,
    save_agents_config,
    save_json,
    write_default_configs,
)
from .git_ops import (
    add_all,
    commit as git_commit,
    has_changes,
    init_repo,
    is_repo,
)
from .orchestration import (
    AgentRunner,
    AutoresearchLoop,
    Evaluator,
    RalphLoop,
    RoleProposal,
    WorkflowRunner,
    casting_objective,
    is_baseline,
    list_roles,
    load_workflow,
    register_role,
    remove_role,
    render_roster_summary,
)
from .providers import available_providers, load_provider
from .templates import PROMPTS, WORKFLOWS
from .utils.logging_utils import close_log, init_log_file, log, log_exception


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _print_header(title: str) -> None:
    print(f"\n=== {title} ===")


def _ensure_initialized(paths: Paths) -> bool:
    if not is_initialized(paths):
        print("CLK is not initialized in this directory. Run `clk init` first.", file=sys.stderr)
        return False
    return True


def _materialize_prompts(paths: Paths) -> List[str]:
    written = []
    for name, body in PROMPTS.items():
        target = paths.prompts / name
        if not target.exists():
            try:
                target.write_text(body, encoding="utf-8")
                written.append(str(target.relative_to(paths.root)))
            except Exception as exc:
                log_exception("cli._materialize_prompts", exc)
    return written


def _materialize_workflows(paths: Paths) -> List[str]:
    written = []
    for name, body in WORKFLOWS.items():
        target = paths.workflows / name
        if not target.exists():
            try:
                target.write_text(body, encoding="utf-8")
                written.append(str(target.relative_to(paths.root)))
            except Exception as exc:
                log_exception("cli._materialize_workflows", exc)
    return written


def _ensure_gitignore(paths: Paths) -> bool:
    gi = paths.root / ".gitignore"
    block_marker = "# CLK local-only artifacts"
    block = textwrap.dedent(
        """\
        # CLK local-only artifacts
        .clk/venv/
        .clk/node/
        .clk/tools/
        .clk/logs/
        .clk/runs/
        .clk/state/agent_memory.jsonl
        .clk/state/experiments.jsonl
        .clk/backups/
        .clk/cache/
        """
    )
    try:
        if gi.exists():
            current = gi.read_text(encoding="utf-8")
            if block_marker in current:
                return False
            gi.write_text(current.rstrip() + "\n\n" + block, encoding="utf-8")
            return True
        gi.write_text(block, encoding="utf-8")
        return True
    except Exception as exc:
        log_exception("cli._ensure_gitignore", exc)
        return False


def _setup_local_venv(paths: Paths) -> bool:
    if paths.venv.exists():
        return False
    try:
        log(f"creating local venv at {paths.venv}")
        builder = venv.EnvBuilder(with_pip=False, clear=False, symlinks=True)
        builder.create(str(paths.venv))
        return True
    except Exception as exc:
        log_exception("cli._setup_local_venv", exc)
        return False


def _make_runner(paths: Paths) -> AgentRunner:
    return AgentRunner(
        paths=paths,
        agents_cfg=load_agents_config(paths),
        providers_cfg=load_providers_config(paths),
        clk_cfg=load_clk_config(paths),
    )


def _make_evaluator(paths: Paths) -> Evaluator:
    cfg = load_clk_config(paths)
    checks = cfg.get("validation_checks") or []
    if not checks:
        # Default sanity check: the project is still initialized. Users should
        # override `validation_checks` in clk.config.json with a project-specific
        # gate (e.g. `pytest -q` or `npm test`) once the project has real code.
        checks = ["test -f .clk/config/clk.config.json"]
    return Evaluator(root=paths.root, default_checks=checks)


# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------


def cmd_init(args: argparse.Namespace) -> int:
    paths = project_paths()
    paths.ensure()
    init_log_file(paths.logs, "init")
    log(f"clk init -> {paths.root}")

    write_default_configs(paths, project_name=args.name)
    prompts_written = _materialize_prompts(paths)
    workflows_written = _materialize_workflows(paths)
    gitignore_changed = _ensure_gitignore(paths)
    venv_created = _setup_local_venv(paths)
    repo_initialized = init_repo(paths.root)

    # Seed initial state files
    state = paths.state
    state.mkdir(parents=True, exist_ok=True)
    if not (state / "progress.md").exists():
        (state / "progress.md").write_text(
            f"# Progress\n\n- {datetime.now().isoformat(timespec='seconds')} :: harness initialized\n",
            encoding="utf-8",
        )
    if not (state / "decisions.md").exists():
        (state / "decisions.md").write_text("# Decisions\n\n", encoding="utf-8")

    summary_lines = [
        f"project_root: {paths.root}",
        f"prompts_written: {len(prompts_written)}",
        f"workflows_written: {len(workflows_written)}",
        f"gitignore_updated: {gitignore_changed}",
        f"venv_created: {venv_created}",
        f"git_repo: {repo_initialized}",
    ]
    log("\n".join(summary_lines))

    # Commit scaffold
    if repo_initialized and is_repo(paths.root) and has_changes(paths.root):
        if add_all(paths.root):
            git_commit(
                paths.root,
                agent="clk-init",
                objective="Initialize CLK harness scaffold",
                files_changed=prompts_written + workflows_written,
                validation="prompt and workflow templates materialized",
                next_step="run `clk idea \"<your idea>\"`",
            )

    print("CLK initialized.")
    print("\n".join("  " + l for l in summary_lines))
    print("\nNext: clk idea \"<your idea>\"")
    close_log()
    return 0


def cmd_idea(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, "idea")

    title = args.title or (args.statement.split(".")[0][:80] if args.statement else "Untitled idea")
    payload = {
        "title": title,
        "statement": args.statement,
        "captured_at": datetime.now().isoformat(timespec="seconds"),
        "tags": args.tag or [],
    }
    save_json(paths.state / "idea.json", payload)

    brief_path = paths.state / "system_brief.md"
    brief = textwrap.dedent(
        f"""\
        # System brief

        **Title:** {title}

        ## Idea
        {args.statement}

        ## Captured at
        {payload['captured_at']}

        ## Notes
        - This brief is the seed for all downstream agents.
        - Update with research findings, decisions, and constraints as the system evolves.
        """
    )
    try:
        brief_path.write_text(brief, encoding="utf-8")
    except Exception as exc:
        log_exception("cli.cmd_idea.brief", exc)

    if is_repo(paths.root) and has_changes(paths.root):
        if add_all(paths.root):
            git_commit(
                paths.root,
                agent="clk-idea",
                objective=f"Capture idea: {title}",
                files_changed=[".clk/state/idea.json", ".clk/state/system_brief.md"],
                validation="idea captured",
                next_step="run `clk cast` then `clk run`",
            )

    print(f"Idea captured: {title}")
    print(f"  -> {paths.state / 'idea.json'}")
    print(f"  -> {brief_path}")

    # Auto-cast: ask the chief to design the roster + workflow for this
    # idea. Skipped if the user disabled it in config.
    cfg = load_clk_config(paths)
    auto = bool(((cfg.get("casting") or {}).get("auto_cast_on_idea", True)))
    if auto and not getattr(args, "no_cast", False):
        try:
            print("\nRunning chief casting (auto)...")
            _run_casting(paths, statement=args.statement, title=title, dry_run=False)
        except Exception as exc:
            log_exception("cli.cmd_idea.auto_cast", exc)
            print(f"  casting failed: {exc}", file=sys.stderr)

    close_log()
    return 0


def _run_casting(paths: Paths, *, statement: str, title: str, dry_run: bool) -> None:
    """Invoke the chief in casting mode.

    The chief reads the current idea + roster and emits PROPOSE_ROLE /
    PROPOSE_WORKFLOW blocks; AgentRunner applies them automatically.
    """
    runner = _make_runner(paths)
    objective = casting_objective(title, statement)
    before_roster = list_roles(paths)
    run = runner.run(
        "chief",
        objective,
        extra={"phase": "casting"},
        dry_run=dry_run,
    )
    after_roster = list_roles(paths)
    added = sorted(set(after_roster) - set(before_roster))
    removed = sorted(set(before_roster) - set(after_roster))
    print(f"  chief casting ok={run.response.ok}")
    if added:
        print(f"  +roles: {', '.join(added)}")
    if removed:
        print(f"  -roles: {', '.join(removed)}")
    if not added and not removed:
        print("  (no roster changes - chief may have only updated existing roles or workflows)")
    print(render_roster_summary(paths))


def cmd_plan(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, "plan")

    runner = _make_runner(paths)
    wf_runner = WorkflowRunner(paths, runner)
    overall_ok = True
    for wf_name in ["discovery", "product"]:
        wf_path = paths.workflows / f"{wf_name}.yaml"
        if not wf_path.exists():
            log(f"workflow missing: {wf_path}", level="WARN")
            overall_ok = False
            continue
        try:
            wf = load_workflow(wf_path)
        except Exception as exc:
            log_exception("cli.cmd_plan.load_workflow", exc)
            overall_ok = False
            continue
        results = wf_runner.run(wf, dry_run=args.dry_run)
        for r in results:
            mark = "OK" if r.run.response.ok and r.validated else "FAIL"
            print(f"  [{wf_name}/{r.stage.id}] {mark} (committed={r.committed})")
            if not (r.run.response.ok and r.validated):
                overall_ok = False
    print("Plan complete." if overall_ok else "Plan completed with failures.")
    close_log()
    return 0 if overall_ok else 1


def cmd_run(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, "run")

    runner = _make_runner(paths)
    wf_runner = WorkflowRunner(paths, runner)
    wf_name = args.workflow or load_clk_config(paths).get("default_workflow") or "engineering"
    wf_path = paths.workflows / f"{wf_name}.yaml"
    if not wf_path.exists():
        print(f"workflow not found: {wf_path}", file=sys.stderr)
        return 2
    try:
        wf = load_workflow(wf_path)
    except Exception as exc:
        log_exception("cli.cmd_run.load_workflow", exc)
        return 1
    results = wf_runner.run(wf, dry_run=args.dry_run)
    ok_count = sum(1 for r in results if r.run.response.ok and r.validated)
    fail_count = len(results) - ok_count
    print(f"workflow {wf_name}: {ok_count} ok, {fail_count} failed, {len(results)} total")
    close_log()
    return 0 if fail_count == 0 else 1


def cmd_loop(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, f"loop-{args.mode}")

    runner = _make_runner(paths)
    evaluator = _make_evaluator(paths)
    max_iter = args.max_iterations or load_clk_config(paths).get("max_iterations") or 20

    if args.mode == "ralph":
        loop = RalphLoop(paths, runner, evaluator, max_iterations=max_iter)
        outcomes = loop.run(dry_run=args.dry_run)
        improved = sum(1 for o in outcomes if o.improved)
        committed = sum(1 for o in outcomes if o.committed)
        print(f"ralph loop: {improved}/{len(outcomes)} improved, {committed} committed")
    else:
        loop = AutoresearchLoop(paths, runner, evaluator, max_iterations=max_iter)
        experiments = loop.run(dry_run=args.dry_run)
        committed = sum(1 for e in experiments if e.committed)
        print(f"autoresearch loop: {len(experiments)} experiments, {committed} committed")
    close_log()
    return 0


def cmd_cast(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, "cast")
    idea_path = paths.state / "idea.json"
    title = "Untitled idea"
    statement = ""
    if idea_path.exists():
        try:
            payload = json.loads(idea_path.read_text(encoding="utf-8"))
            title = payload.get("title") or title
            statement = payload.get("statement") or ""
        except Exception as exc:
            log_exception("cli.cmd_cast.read_idea", exc)
    if not statement:
        print("No idea captured yet. Run `clk idea \"<your idea>\"` first.", file=sys.stderr)
        return 2
    _run_casting(paths, statement=statement, title=title, dry_run=args.dry_run)
    close_log()
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    if args.action == "list":
        print(render_roster_summary(paths))
        return 0
    if args.action == "add":
        if not args.name:
            print("--name is required for `roles add`", file=sys.stderr)
            return 2
        prop = RoleProposal(name=args.name, role=args.role or "", provider=args.provider)
        ok, status = register_role(paths, prop)
        print(f"add {args.name}: {status}")
        return 0 if ok else 1
    if args.action == "remove":
        if not args.name:
            print("--name is required for `roles remove`", file=sys.stderr)
            return 2
        ok, status = remove_role(paths, args.name)
        print(f"remove {args.name}: {status}")
        return 0 if ok else 1
    print(f"unknown action: {args.action}", file=sys.stderr)
    return 2


def cmd_tui(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    init_log_file(paths.logs, "tui")
    try:
        from . import tui as _tui
    except Exception as exc:
        log_exception("cli.cmd_tui.import", exc)
        print(f"failed to import tui: {exc}", file=sys.stderr)
        return 1
    return _tui.run(initial_prompt=args.prompt)


def cmd_status(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2

    cfg = load_clk_config(paths)
    prov_cfg = load_providers_config(paths)
    agents_cfg = load_agents_config(paths)

    _print_header("CLK status")
    print(f"version:           {__version__}")
    print(f"project_root:      {paths.root}")
    print(f"project_name:      {cfg.get('project_name')}")
    print(f"default_provider:  {cfg.get('default_provider')}")
    print(f"active_provider:   {prov_cfg.get('active')}")
    print(f"default_workflow:  {cfg.get('default_workflow')}")
    print(f"git_repo:          {is_repo(paths.root)}")
    print(f"agents:            {', '.join((agents_cfg.get('agents') or {}).keys())}")

    _print_header("Providers")
    for name, ok in available_providers(prov_cfg).items():
        marker = "available" if ok else "unavailable"
        print(f"  {name:<8} {marker}")

    _print_header("Recent runs")
    runs = sorted(paths.runs.glob("*"), reverse=True)[:5] if paths.runs.exists() else []
    if not runs:
        print("  (none)")
    else:
        for r in runs:
            print(f"  {r.name}")

    if (paths.state / "done.md").exists():
        _print_header("Completion")
        print(f"  done.md exists at {paths.state / 'done.md'}")

    return 0


def cmd_providers(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    prov_cfg = load_providers_config(paths)
    avail = available_providers(prov_cfg)
    print(json.dumps({"active": prov_cfg.get("active"), "available": avail}, indent=2))
    return 0


def cmd_configure(args: argparse.Namespace) -> int:
    paths = project_paths()
    if not _ensure_initialized(paths):
        return 2
    cfg = load_clk_config(paths)
    if args.set:
        for kv in args.set:
            if "=" not in kv:
                print(f"ignoring '{kv}' (expected key=value)", file=sys.stderr)
                continue
            k, v = kv.split("=", 1)
            v = v.strip()
            if v.lower() in ("true", "false"):
                cfg[k] = v.lower() == "true"
            elif v.isdigit():
                cfg[k] = int(v)
            else:
                cfg[k] = v
        save_json(paths.config / "clk.config.json", cfg)
    if args.show or not args.set:
        print(json.dumps(cfg, indent=2))
    return 0


# ---------------------------------------------------------------------------
# argparse plumbing
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="clk",
        description="Cognitive Loop Kernel - local-only multi-agent development harness.",
    )
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    p_init = sub.add_parser("init", help="Initialize CLK in the current directory.")
    p_init.add_argument("--name", help="Project name (defaults to directory name).")
    p_init.set_defaults(func=cmd_init)

    p_idea = sub.add_parser("idea", help="Capture an idea.")
    p_idea.add_argument("statement", help="The idea, problem statement, or vision.")
    p_idea.add_argument("--title", help="Short title for the idea.")
    p_idea.add_argument("--tag", action="append", help="Optional tag (repeatable).")
    p_idea.add_argument("--no-cast", action="store_true", help="Skip the automatic chief casting pass.")
    p_idea.set_defaults(func=cmd_idea)

    p_cast = sub.add_parser("cast", help="Run the chief in casting mode (re-design the roster + workflow).")
    p_cast.add_argument("--dry-run", action="store_true")
    p_cast.set_defaults(func=cmd_cast)

    p_roles = sub.add_parser("roles", help="Inspect or edit the current roster.")
    p_roles.add_argument("action", choices=["list", "add", "remove"])
    p_roles.add_argument("--name", help="Role name (snake_case).")
    p_roles.add_argument("--role", help="One-line role description (for add).")
    p_roles.add_argument("--provider", help="Optional provider override.")
    p_roles.set_defaults(func=cmd_roles)

    p_plan = sub.add_parser("plan", help="Run discovery + product workflows.")
    p_plan.add_argument("--dry-run", action="store_true")
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="Run a single development cycle.")
    p_run.add_argument("--workflow", help="Workflow name (default: engineering).")
    p_run.add_argument("--dry-run", action="store_true")
    p_run.set_defaults(func=cmd_run)

    p_loop = sub.add_parser("loop", help="Run a Ralph or autoresearch loop.")
    p_loop.add_argument("--mode", choices=["ralph", "autoresearch"], default="ralph")
    p_loop.add_argument("--max-iterations", type=int)
    p_loop.add_argument("--dry-run", action="store_true")
    p_loop.set_defaults(func=cmd_loop)

    p_tui = sub.add_parser("tui", help="Launch the TUI dashboard.")
    p_tui.add_argument("prompt", nargs="?", help="Optional initial idea / prompt.")
    p_tui.set_defaults(func=cmd_tui)

    p_status = sub.add_parser("status", help="Show harness status.")
    p_status.set_defaults(func=cmd_status)

    p_prov = sub.add_parser("providers", help="List providers and availability.")
    p_prov.set_defaults(func=cmd_providers)

    p_conf = sub.add_parser("configure", help="View or modify clk.config.json.")
    p_conf.add_argument("--set", action="append", default=[], help="key=value (repeatable).")
    p_conf.add_argument("--show", action="store_true")
    p_conf.set_defaults(func=cmd_configure)

    return p


def main(argv: Optional[List[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return int(args.func(args) or 0)
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return 130
    except Exception as exc:
        log_exception("cli.main", exc)
        return 1


if __name__ == "__main__":
    sys.exit(main())
