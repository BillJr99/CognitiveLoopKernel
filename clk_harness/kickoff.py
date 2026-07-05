"""``clk kickoff`` — bootstrap a self-contained kickoff workspace.

Python port of the historical ``kickoff.sh`` body.  The shell script is
now a thin wrapper that ``exec``s this subcommand; everything it used to
do lives here:

  * load ``.env`` (via :mod:`clk_harness.env_file`) and export it,
  * apply ``--arg`` overrides, then built-in defaults,
  * validate the resolved configuration (offering ``--setup`` on a TTY),
  * create ``workspace/kickoff-<ts>/`` with the harness copied under
    ``.clk/harness/``, its own git repo, secret-blocking ``.gitignore``
    and pre-push hook,
  * apply ``CLK_*`` env-var overrides into ``.clk/config/clk.config.json``
    (:func:`apply_config_env_overrides` — the former embedded heredoc #1),
  * activate the chosen provider in ``.clk/config/providers.json``
    (:func:`activate_provider` — the former embedded heredoc #2),
  * drive the non-interactive idea → plan → run → loop pipeline, or hand
    control to the TUI dashboard,
  * host the interactive ``--setup`` wizard with per-step resume via
    ``.clk/.setup-progress`` (reusing :mod:`clk_harness.env_file` for
    atomic ``.env`` writes).

Configuration precedence mirrors the shell script exactly:
``--arg overrides  →  .env file  →  shell environment vars  →  defaults``
(the ``.env`` file is sourced *after* the environment, so its values win —
same as ``set -a; . .env`` did).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Mapping, MutableMapping, Optional, TextIO, Tuple

from . import env_file

VALID_PROVIDERS = ("shell", "claude", "codex", "gemini", "pi", "ollama", "openwebui")

# Built-in defaults, applied after .env and --arg overrides (mirrors
# kickoff.sh's `_apply_defaults`). Empty counts as unset, like `${VAR:-def}`.
_DEFAULTS: Tuple[Tuple[str, str], ...] = (
    ("CLK_PROVIDER", "shell"),
    ("CLK_MAX_ITERATIONS", "10"),
    ("CLK_PROJECT_NAME", "clk-app"),
    ("CLK_RUN_INSTALL", "false"),
    ("CLK_NO_TUI", "false"),
    ("CLK_AUTH_MODE", "cli"),
    ("CLK_OLLAMA_ENDPOINT", "http://localhost:11434"),
    ("CLK_OLLAMA_MODEL", "llama3.1"),
)


def harness_root() -> Path:
    """The directory holding ``clk_harness/``, ``scripts/`` and ``.env``.

    This is kickoff.sh's ``SCRIPT_DIR``: the repo root in a source tree,
    or ``<kickoff>/.clk/harness`` in a kickoff layout.
    """
    return Path(__file__).resolve().parent.parent


def _open_tty() -> Optional[TextIO]:
    """Open ``/dev/tty`` read/write, or None when non-interactive."""
    try:
        return open("/dev/tty", "r+", buffering=1)
    except OSError:
        return None


# ===========================================================================
# CLK_* env-var overrides -> .clk/config/clk.config.json
#
# Ported verbatim from kickoff.sh's first embedded Python heredoc. The
# harness ships sane defaults in DEFAULT_CLK_CONFIG (see config.py); this
# lets the user override any of them from the environment without
# hand-editing the JSON. Any unset (or empty, or uncastable) variable
# falls through to the harness's default — keys we didn't see are never
# touched, so a partially-set env still gets the rest from the defaults.
# ===========================================================================


def _bool(s) -> bool:
    return str(s).strip().lower() in {"1", "true", "yes", "y", "on"}


def _csv(s) -> List[str]:
    return [item.strip() for item in str(s).split(",") if item.strip()]


# (env var, config path, cast) — the exact map from the shell heredoc.
CONFIG_ENV_OVERRIDES: Tuple[Tuple[str, Tuple[str, ...], Callable], ...] = (
    # Robustness block
    ("CLK_ROBUSTNESS_AUTO_CONSENSUS", ("robustness", "auto_consensus"), str),
    ("CLK_ROBUSTNESS_AUTO_REFINE", ("robustness", "auto_refine"), str),
    ("CLK_ROBUSTNESS_MAX_QUALITY_RETRIES", ("robustness", "max_quality_retries"), int),
    ("CLK_ROBUSTNESS_MIN_RESPONSE_CHARS", ("robustness", "min_response_chars"), int),
    ("CLK_ROBUSTNESS_REFINE_MAX_ROUNDS", ("robustness", "refine_max_rounds"), int),
    ("CLK_ROBUSTNESS_REFINE_ACCEPT_THRESHOLD", ("robustness", "refine_accept_threshold"), float),
    ("CLK_ROBUSTNESS_QA_PARALLEL_JUDGES", ("robustness", "qa_parallel_judges"), int),
    ("CLK_ROBUSTNESS_MAX_QA_DEPTH", ("robustness", "max_qa_depth"), int),
    ("CLK_ROBUSTNESS_PLATEAU_WINDOW", ("robustness", "plateau_window"), int),
    ("CLK_ROBUSTNESS_PLATEAU_ACTION", ("robustness", "plateau_action"), str),
    ("CLK_ROBUSTNESS_DEBATE", ("robustness", "debate"), str),
    ("CLK_ROBUSTNESS_DEBATE_LENSES", ("robustness", "debate_lenses"), _csv),
    ("CLK_ROBUSTNESS_DEBATE_MAX_ROUNDS", ("robustness", "debate_max_rounds"), int),
    # Autonomous mission block
    ("CLK_MISSION_MAX_PHASES", ("mission", "max_phases"), int),
    ("CLK_MISSION_MAX_ITERATIONS_PER_PHASE", ("mission", "max_iterations_per_phase"), int),
    ("CLK_MISSION_MAX_TOTAL_CYCLES", ("mission", "max_total_cycles"), int),
    ("CLK_MISSION_PHASE_GATE", ("mission", "phase_gate"), _bool),
    ("CLK_MISSION_REFINE_REQUIRED", ("mission", "refine_required"), _bool),
    ("CLK_MISSION_AUTO_CONSENSUS_ON_STALL", ("mission", "auto_consensus_on_stall"), _bool),
    ("CLK_MISSION_CHARTER_FIRST", ("mission", "charter_first"), _bool),
    ("CLK_MISSION_COMMIT_TRACE", ("mission", "commit_trace"), _bool),
    ("CLK_MISSION_COMMIT_GRANULARITY", ("mission", "commit_granularity"), str),
    ("CLK_MISSION_MIN_CYCLES_BEFORE_DONE", ("mission", "min_cycles_before_done"), int),
    ("CLK_MISSION_TELEMETRY_STDOUT", ("mission", "telemetry_stdout"), _bool),
    ("CLK_MISSION_ON_BUDGET_EXHAUSTED", ("mission", "on_budget_exhausted"), str),
    ("CLK_MISSION_DEFAULT_PHASES", ("mission", "default_phases"), _csv),
    # Done-gate block
    ("CLK_DONE_GATE_ENABLED", ("done_gate", "enabled"), _bool),
    ("CLK_DONE_GATE_REQUIRE_TESTS_GREEN", ("done_gate", "require_tests_green"), _bool),
    ("CLK_DONE_GATE_REQUIRE_DELIVERABLES", ("done_gate", "require_deliverables"), _bool),
    ("CLK_DONE_GATE_MIN_DELIVERABLE_FILES", ("done_gate", "min_deliverable_files"), int),
    ("CLK_DONE_GATE_REQUIRE_QA_PASS", ("done_gate", "require_qa_pass"), _bool),
    ("CLK_DONE_GATE_REQUIRE_RALPH_PASS", ("done_gate", "require_ralph_pass"), _bool),
    ("CLK_DONE_GATE_FORBID_TODO_MARKERS", ("done_gate", "forbid_todo_markers"), _bool),
    ("CLK_DONE_GATE_MAX_FINISH_ATTEMPTS", ("done_gate", "max_finish_attempts"), int),
    # No-op guard block
    ("CLK_NOOP_GUARD_ENABLED", ("noop_guard", "enabled"), _bool),
    ("CLK_NOOP_GUARD_MAX_REDISPATCH", ("noop_guard", "max_redispatch"), int),
    ("CLK_NOOP_GUARD_PRODUCING_AGENTS", ("noop_guard", "producing_agents"), _csv),
    ("CLK_NOOP_GUARD_TREAT_OUTPUTS_STAGE_AS_PRODUCING", ("noop_guard", "treat_outputs_stage_as_producing"), _bool),
    # Deliberation block
    ("CLK_DELIBERATION_ENABLED", ("deliberation", "enabled"), _bool),
    ("CLK_DELIBERATION_ENCOURAGE_QUESTIONS", ("deliberation", "encourage_questions"), _bool),
    ("CLK_DELIBERATION_REQUIRE_OPEN_QUESTIONS_RESOLVED", ("deliberation", "require_open_questions_resolved"), _bool),
    ("CLK_DELIBERATION_SELF_REFLECT_PREAMBLE", ("deliberation", "self_reflect_preamble"), _bool),
    ("CLK_DELIBERATION_MIN_DEBATE_ROUNDS", ("deliberation", "min_debate_rounds"), int),
    # Validation auto-derive
    ("CLK_VALIDATION_AUTO_DERIVE", ("validation", "auto_derive"), _bool),
    # Prior knobs
    ("CLK_PROVIDER_TIMEOUT_S", ("provider_timeout_s",), int),
    ("CLK_PROVIDER_NO_OUTPUT_TIMEOUT_S", ("provider_no_output_timeout_s",), int),
    ("CLK_PROVIDER_RETRY_MAX_RETRIES", ("provider_retry", "max_retries"), int),
    ("CLK_PROVIDER_RETRY_BACKOFF_S", ("provider_retry", "backoff_s"), float),
    ("CLK_PROVIDER_RETRY_STAGE_MAX_RETRIES", ("provider_retry", "stage_max_retries"), int),
    ("CLK_PROVIDER_RETRY_STAGE_BACKOFF_S", ("provider_retry", "stage_backoff_s"), float),
    ("CLK_SUPERVISE_MAX_CYCLES", ("supervise", "max_cycles"), int),
    ("CLK_CONSENSUS_MAX_SAMPLES", ("consensus", "max_samples"), int),
    ("CLK_CONSENSUS_MAX_PARALLEL", ("consensus", "max_parallel"), int),
    ("CLK_CASTING_MAX_DYNAMIC_ROLES", ("casting", "max_dynamic_roles"), int),
    ("CLK_AUTO_COMMIT", ("auto_commit",), _bool),
    ("CLK_VALIDATION_MAX_FILES_PER_BATCH", ("validation", "max_files_per_batch"), int),
    ("CLK_VALIDATION_WARN_FILES_PER_BATCH", ("validation", "warn_files_per_batch"), int),
    ("CLK_META_PROMPT_DISPATCH", ("meta_prompt", "dispatch"), str),
    ("CLK_META_PROMPT_ROLE", ("meta_prompt", "role"), str),
    ("CLK_REVIEW_PER_STAGE", ("review", "per_stage"), _bool),
    ("CLK_RECOVERY_MAX_PER_STAGE", ("recovery", "max_per_stage"), int),
)


def apply_config_env_overrides(config_path: Path, env: Optional[Mapping[str, str]] = None) -> None:
    """Apply recognised ``CLK_*`` env vars into ``clk.config.json``.

    Exact port of kickoff.sh's first heredoc: unset/empty values are
    skipped, uncastable values are silently ignored, intermediate dicts
    are created with ``setdefault``, and the file is rewritten with
    ``indent=2, sort_keys=True``. Missing file is a no-op.
    """
    environ: Mapping[str, str] = os.environ if env is None else env
    p = Path(config_path)
    if not p.exists():
        return  # nothing to override
    cfg = json.loads(p.read_text(encoding="utf-8"))
    for env_var, path, cast in CONFIG_ENV_OVERRIDES:
        raw = environ.get(env_var)
        if raw is None or raw == "":
            continue
        try:
            val = cast(raw)
        except (TypeError, ValueError):
            continue
        cur = cfg
        for key in path[:-1]:
            cur = cur.setdefault(key, {})
        cur[path[-1]] = val
    p.write_text(json.dumps(cfg, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ===========================================================================
# Provider activation -> .clk/config/providers.json
#
# Ported verbatim from kickoff.sh's second embedded Python heredoc,
# including the `VAR="${VAR:-default}"` prefixes the shell put on the
# invocation (an unset OR empty variable collapses to the default).
# ===========================================================================


def activate_provider(providers_path: Path, env: Optional[Mapping[str, str]] = None) -> None:
    """Set the active provider and wire per-provider settings/keys.

    For CLI-driven providers, mode=cli (default) spawns the CLI
    subprocess. mode=api makes the provider call the upstream HTTP API
    directly with no subprocess — which is exactly what the user expects
    when they choose "apikey" auth: the API key alone, no local CLI
    dependency.
    """
    environ: Mapping[str, str] = os.environ if env is None else env

    def _dflt(key: str, default: str = "") -> str:
        # `${VAR:-default}` semantics from the shell invocation.
        return environ.get(key) or default

    p = Path(providers_path)
    data = json.loads(p.read_text(encoding="utf-8"))
    provider = environ["CLK_PROVIDER"]
    data["active"] = provider
    provs = data.setdefault("providers", {})
    auth_mode = _dflt("CLK_AUTH_MODE", "cli")
    for cli_provider in ("claude", "codex", "gemini"):
        provs.setdefault(cli_provider, {"type": cli_provider})
        provs[cli_provider]["mode"] = "api" if auth_mode == "apikey" else "cli"
    if provider == "claude" and auth_mode == "apikey":
        provs["claude"]["api_key"] = environ.get("ANTHROPIC_API_KEY", "")
    if provider == "codex" and auth_mode == "apikey":
        provs["codex"]["api_key"] = environ.get("OPENAI_API_KEY", "")
    if provider == "gemini" and auth_mode == "apikey":
        provs["gemini"]["api_key"] = (
            environ.get("GEMINI_API_KEY", "")
            or environ.get("GOOGLE_API_KEY", "")
        )
    if provider == "ollama":
        provs.setdefault("ollama", {})
        provs["ollama"]["endpoint"] = _dflt("CLK_OLLAMA_ENDPOINT", "http://localhost:11434")
        provs["ollama"]["model"] = _dflt("CLK_OLLAMA_MODEL", "llama3.1")
    elif provider == "openwebui":
        provs.setdefault("openwebui", {"type": "openwebui"})
        provs["openwebui"]["type"] = "openwebui"
        provs["openwebui"]["endpoint"] = _dflt("CLK_OPENWEBUI_ENDPOINT")
        provs["openwebui"]["api_key"] = _dflt("CLK_OPENWEBUI_API_KEY")
        provs["openwebui"]["model"] = _dflt("CLK_OPENWEBUI_MODEL")
    elif provider == "pi":
        provs.setdefault("pi", {"type": "pi", "command": "pi", "args": []})
        pi_model = _dflt("CLK_PI_MODEL").strip()
        pi_key = _dflt("CLK_PI_API_KEY").strip()
        pi_key_type = _dflt("CLK_PI_KEY_TYPE").strip().lower()
        if pi_model:
            provs["pi"]["model"] = pi_model
        if pi_key:
            provs["pi"]["api_key"] = pi_key
        if pi_key_type:
            provs["pi"]["key_type"] = pi_key_type
    p.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ===========================================================================
# Defaults + validation (ports of _apply_defaults and _clk_missing)
# ===========================================================================


def _apply_defaults(cfg: Dict[str, str]) -> None:
    for key, default in _DEFAULTS:
        if not cfg.get(key):
            cfg[key] = default


def _missing_config(cfg: Mapping[str, str], environ: Mapping[str, str], idea: str) -> List[str]:
    """Validate resolved config. One string per problem; empty when OK."""
    problems: List[str] = []
    provider = cfg["CLK_PROVIDER"]
    auth_mode = cfg["CLK_AUTH_MODE"]
    if provider == "shell":
        pass
    elif provider == "claude":
        if auth_mode == "apikey" and not environ.get("ANTHROPIC_API_KEY"):
            problems.append(
                "ANTHROPIC_API_KEY is unset — required when CLK_PROVIDER=claude and "
                "CLK_AUTH_MODE=apikey (or set CLK_AUTH_MODE=cli to use 'claude login')"
            )
    elif provider == "codex":
        if auth_mode == "apikey" and not environ.get("OPENAI_API_KEY"):
            problems.append(
                "OPENAI_API_KEY is unset — required when CLK_PROVIDER=codex and "
                "CLK_AUTH_MODE=apikey (or set CLK_AUTH_MODE=cli)"
            )
    elif provider == "gemini":
        if auth_mode == "apikey" and not environ.get("GEMINI_API_KEY") and not environ.get("GOOGLE_API_KEY"):
            problems.append(
                "GEMINI_API_KEY (or GOOGLE_API_KEY) is unset — required when "
                "CLK_PROVIDER=gemini and CLK_AUTH_MODE=apikey"
            )
    elif provider == "pi":
        pass  # Nothing strictly required; pi login handles auth
    elif provider == "ollama":
        pass  # Has built-in defaults
    elif provider == "openwebui":
        if not environ.get("CLK_OPENWEBUI_ENDPOINT"):
            problems.append("CLK_OPENWEBUI_ENDPOINT is unset — required for CLK_PROVIDER=openwebui")
        if not environ.get("CLK_OPENWEBUI_MODEL"):
            problems.append(
                "CLK_OPENWEBUI_MODEL is unset — required for CLK_PROVIDER=openwebui "
                "(use --setup to pick from a live model list)"
            )
    else:
        problems.append(
            f"CLK_PROVIDER='{provider}' is not recognised (valid: {'|'.join(VALID_PROVIDERS)})"
        )

    if not re.fullmatch(r"[0-9]+", cfg["CLK_MAX_ITERATIONS"]):
        problems.append(
            f"CLK_MAX_ITERATIONS must be a positive integer (got '{cfg['CLK_MAX_ITERATIONS']}')"
        )

    if cfg.get("CLK_NO_TUI", "false") == "true" and not idea:
        problems.append(
            "An idea argument is required when CLK_NO_TUI=true — pass it as the first "
            "positional argument"
        )
    return problems


# ===========================================================================
# --restore / --list / --clean early-exit modes
# ===========================================================================


def _cmd_restore(script_dir: Path) -> int:
    env_path = script_dir / ".env"
    bak = script_dir / ".env.bak"
    if not bak.is_file():
        print(f"[lib_env] no backup at {bak}", file=sys.stderr)
        print("[kickoff] no .env.bak to restore", file=sys.stderr)
        return 1
    os.replace(bak, env_path)
    print("[kickoff] .env restored from .env.bak")
    return 0


def _cmd_list() -> int:
    ws_dir = Path.cwd() / "workspace"
    if not ws_dir.is_dir():
        print("[kickoff] no workspace/ dir yet — nothing to list.")
        return 0
    print(f"{'kickoff dir':<32} {'last activity':<19} idea")
    for d in sorted(ws_dir.glob("kickoff-*")):
        if not d.is_dir():
            continue
        try:
            last = datetime.fromtimestamp(d.stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        except OSError:
            last = "?"
        idea = ""
        idea_json = d / ".clk" / "state" / "idea.json"
        if idea_json.is_file():
            try:
                idea = (json.loads(idea_json.read_text(encoding="utf-8")).get("title") or "")[:60]
            except Exception:
                idea = ""
        print(f"{d.name:<32} {last:<19} {idea}")
    return 0


def _cmd_clean(older_than: str) -> int:
    ws_dir = Path.cwd() / "workspace"
    if not ws_dir.is_dir():
        print("[kickoff] no workspace/ dir; nothing to clean.")
        return 0
    unit, qty_s = older_than[-1:], older_than[:-1]
    if not re.fullmatch(r"[0-9]+", qty_s):
        print(f"[kickoff] --clean expects something like 7d or 30m (got {older_than})", file=sys.stderr)
        return 2
    qty = int(qty_s)
    if unit == "d":
        divisor = 86400
    elif unit == "m":
        divisor = 60
    else:
        print("[kickoff] --clean unit must be d (days) or m (minutes)", file=sys.stderr)
        return 2
    now = time.time()
    targets = []
    for d in sorted(ws_dir.glob("kickoff-*")):
        if not d.is_dir():
            continue
        try:
            age = now - d.stat().st_mtime
        except OSError:
            continue
        # find's -mtime +N / -mmin +N semantics: strictly more than N whole units.
        if int(age // divisor) > qty:
            targets.append(d)
    if not targets:
        print(f"[kickoff] no kickoff dirs older than {older_than}.")
        return 0
    print(f"[kickoff] would remove {len(targets)} kickoff dirs older than {older_than}:")
    for t in targets:
        print(f"  - {t}")
    tty = _open_tty()
    if tty is None:
        print("[kickoff] non-interactive; refusing to delete without confirmation.", file=sys.stderr)
        print("[kickoff] re-run from a terminal to confirm.", file=sys.stderr)
        return 2
    try:
        tty.write("Delete these? [y/N]: ")
        tty.flush()
        ans = (tty.readline() or "").strip().lower()
    finally:
        tty.close()
    if ans in ("y", "yes"):
        for t in targets:
            shutil.rmtree(t, ignore_errors=True)
            print(f"[kickoff] removed {t}")
    else:
        print("[kickoff] nothing deleted.")
    return 0


# ===========================================================================
# Kickoff directory scaffolding (sections 5 & 6 of the shell script)
# ===========================================================================

_LAUNCHER_SHIM = """\
#!/usr/bin/env bash
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" >/dev/null 2>&1 && pwd)"
PROJECT_ROOT="$(cd -- "$SCRIPT_DIR/../.." >/dev/null 2>&1 && pwd)"
export CLK_PROJECT_ROOT="$PROJECT_ROOT"
exec "$PROJECT_ROOT/.clk/harness/scripts/clk" "$@"
"""

_KICKOFF_GITIGNORE = """\
# All harness state lives under .clk/ — ignore it entirely.
.clk/
# Secrets
/.env
/.env.example
/.env.bak
/.env.partial
.env.local
*.pem
*.key
*_id_rsa*
/secrets/
/.secrets/
# Editor / OS junk
__pycache__/
*.pyc
.DS_Store
.idea/
.vscode/
"""

_PRE_PUSH_HOOK = """\
#!/usr/bin/env bash
# CLK pre-push secret scan. Bypass with `git push --no-verify` when sure.
set -eo pipefail
while read -r local_ref local_sha remote_ref remote_sha; do
  [ "$local_sha" = "0000000000000000000000000000000000000000" ] && continue
  range="$local_sha"
  if [ "$remote_sha" != "0000000000000000000000000000000000000000" ]; then
    range="$remote_sha..$local_sha"
  fi
  hits=$(git log -p "$range" 2>/dev/null | grep -E \\
    -e 'ANTHROPIC_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'OPENAI_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'OPENROUTER_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'GEMINI_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'GOOGLE_API_KEY=[A-Za-z0-9_\\-]+' \\
    -e 'sk-[A-Za-z0-9]{20,}' \\
    -e 'xoxb-[A-Za-z0-9-]{20,}' \\
    -e 'BEGIN (RSA|OPENSSH|EC|DSA|PGP) PRIVATE KEY' \\
    || true)
  if [ -n "$hits" ]; then
    echo "[pre-push] aborting — possible secret(s) in $range:" >&2
    echo "$hits" | head -n 5 >&2
    echo "" >&2
    echo "To override: git push --no-verify  (only when you're sure)." >&2
    exit 1
  fi
done
"""


def _git(kdir: Path, *args: str, capture: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args], cwd=str(kdir), check=False,
        capture_output=capture, text=True,
    )


def _copy_if_present(src: Path, dst: Path) -> None:
    if src.is_dir():
        shutil.copytree(src, dst, ignore=shutil.ignore_patterns("__pycache__"))
    elif src.exists():
        shutil.copy2(src, dst)


def _create_kickoff_dir(script_dir: Path, cfg: Mapping[str, str], idea: str) -> Optional[Path]:
    ts = time.strftime("%Y%m%d-%H%M%S")
    workspace_dir = Path.cwd() / "workspace"
    kickoff_dir = workspace_dir / f"kickoff-{ts}"
    workspace_dir.mkdir(parents=True, exist_ok=True)
    if kickoff_dir.exists():
        print(f"[kickoff] {kickoff_dir} already exists; refusing to overwrite", file=sys.stderr)
        return None
    print(f"[kickoff] creating {kickoff_dir}")
    kickoff_dir.mkdir(parents=True)

    # Harness sources, scripts, and packaging metadata all live under
    # .clk/harness/ so the project root looks like a normal codebase from
    # the agents' point of view.
    harness = kickoff_dir / ".clk" / "harness"
    harness.mkdir(parents=True)
    _copy_if_present(script_dir / "clk_harness", harness / "clk_harness")
    _copy_if_present(script_dir / "scripts", harness / "scripts")
    _copy_if_present(script_dir / "pyproject.toml", harness / "pyproject.toml")
    _copy_if_present(script_dir / "requirements.txt", harness / "requirements.txt")
    _copy_if_present(script_dir / "README.md", harness / "README.md")

    # Launcher shim lives under .clk/scripts/ so the project root stays clean.
    shim_dir = kickoff_dir / ".clk" / "scripts"
    shim_dir.mkdir(parents=True)
    shim = shim_dir / "clk"
    shim.write_text(_LAUNCHER_SHIM, encoding="utf-8")
    shim.chmod(0o755)

    for rel in ("scripts/clk", "scripts/install_local.sh", "scripts/run_loop.sh"):
        p = harness / rel
        if p.exists():
            p.chmod(p.stat().st_mode | 0o111)

    # Manifest so future-you knows how this dir was launched.
    (kickoff_dir / "KICKOFF.md").write_text(
        "# CLK kickoff manifest\n"
        "\n"
        "| Field            | Value |\n"
        "|------------------|-------|\n"
        f"| Timestamp        | {ts} |\n"
        f"| Source dir       | {script_dir} |\n"
        f"| Project name     | {cfg['CLK_PROJECT_NAME']} |\n"
        f"| Provider         | {cfg['CLK_PROVIDER']} |\n"
        f"| Max iterations   | {cfg['CLK_MAX_ITERATIONS']} |\n"
        f"| Ran installer    | {cfg['CLK_RUN_INSTALL']} |\n"
        f"| Idea             | {idea} |\n"
        "\n"
        "This directory is fully self-contained. Delete it to reset.\n",
        encoding="utf-8",
    )
    return kickoff_dir


def _prepare_kickoff_repo(kickoff_dir: Path, environ: Mapping[str, str]) -> None:
    """gitignore + own git repo + pre-push secret hook + optional remote."""
    # Anchor the project root here so find_project_root() returns this dir.
    (kickoff_dir / ".clk").mkdir(exist_ok=True)
    (kickoff_dir / ".gitignore").write_text(_KICKOFF_GITIGNORE, encoding="utf-8")

    if not shutil.which("git"):
        return
    if not (kickoff_dir / ".git").is_dir():
        _git(kickoff_dir, "init", "-q")
        _git(kickoff_dir, "config", "user.name", "CLK Kickoff")
        _git(kickoff_dir, "config", "user.email", "kickoff@local.invalid")

    hooks = kickoff_dir / ".git" / "hooks"
    hooks.mkdir(parents=True, exist_ok=True)
    hook = hooks / "pre-push"
    hook.write_text(_PRE_PUSH_HOOK, encoding="utf-8")
    hook.chmod(0o755)

    # Connect a GitHub remote if the wizard recorded one.
    remote = environ.get("CLK_GITHUB_REMOTE", "")
    mode = environ.get("CLK_GITHUB_MODE", "skip") or "skip"
    if remote and mode != "skip":
        if mode == "existing":
            if _git(kickoff_dir, "remote", "get-url", "origin").returncode != 0:
                print(f"[kickoff] linking existing GitHub remote: {remote}")
                _git(kickoff_dir, "remote", "add", "origin", remote, capture=False)
        elif mode == "create":
            gh_ok = bool(shutil.which("gh")) and subprocess.run(
                ["gh", "auth", "status"], capture_output=True, check=False,
            ).returncode == 0
            if gh_ok:
                if _git(kickoff_dir, "remote", "get-url", "origin").returncode != 0:
                    print(f"[kickoff] creating GitHub repo: {remote} (private)")
                    rc = subprocess.run(
                        ["gh", "repo", "create", remote, "--private",
                         "--source=.", "--remote=origin"],
                        cwd=str(kickoff_dir), check=False,
                    ).returncode
                    if rc != 0:
                        print("[kickoff] gh repo create failed (continuing without remote)")
            else:
                print("[kickoff] CLK_GITHUB_MODE=create but gh CLI is not authenticated; skipping remote.")


# ===========================================================================
# Pipeline / TUI (section 6 of the shell script) — in-process cmd_* calls
# ===========================================================================


def _drive_harness(kickoff_dir: Path, cfg: Dict[str, str], idea: str) -> int:
    from . import cli  # deferred: cli imports this module

    os.chdir(kickoff_dir)

    if cfg["CLK_RUN_INSTALL"].lower() == "true":
        print("[kickoff] running .clk/harness/scripts/install_local.sh")
        rc = subprocess.run(
            ["bash", ".clk/harness/scripts/install_local.sh"], check=False,
        ).returncode
        if rc != 0:
            print("[kickoff] install_local.sh reported a problem (continuing)")

    print("[kickoff] clk init")
    rc = cli.cmd_init(argparse.Namespace(name=cfg["CLK_PROJECT_NAME"]))
    if rc:
        return rc

    print("[kickoff] applying CLK_* env-var overrides to .clk/config/clk.config.json")
    apply_config_env_overrides(Path(".clk/config/clk.config.json"))

    print(f"[kickoff] activating provider: {cfg['CLK_PROVIDER']}")
    merged = dict(os.environ)
    merged.update(cfg)
    activate_provider(Path(".clk/config/providers.json"), env=merged)
    cli.cmd_configure(argparse.Namespace(
        set=[f"default_provider={cfg['CLK_PROVIDER']}"], show=False,
    ))

    if cfg["CLK_NO_TUI"] == "true":
        # Non-interactive pipeline. Useful for CI / smoke tests and Docker
        # without -it.
        print("[kickoff] clk idea")
        rc = cli.cmd_idea(argparse.Namespace(
            statement=idea, title=cfg["CLK_PROJECT_NAME"], tag=None, no_cast=False,
        ))
        if rc:
            return rc
        print("[kickoff] clk plan")
        if cli.cmd_plan(argparse.Namespace(dry_run=False)):
            print("[kickoff] plan reported failures (continuing)")
        print("[kickoff] clk run")
        if cli.cmd_run(argparse.Namespace(
            once=False, workflow=None, resume=False,
            max_phases=None, max_cycles=None, dry_run=False,
        )):
            print("[kickoff] run reported failures (continuing)")
        print(f"[kickoff] clk loop --max-iterations {cfg['CLK_MAX_ITERATIONS']}")
        rc = cli.cmd_loop(argparse.Namespace(
            mode="ralph", max_iterations=int(cfg["CLK_MAX_ITERATIONS"]), dry_run=False,
        ))
        if rc:
            return rc
    else:
        # Default: hand control to the TUI dashboard. If an idea was given,
        # it pre-seeds the idea and starts an engineering cycle.
        print("[kickoff] launching TUI (use /quit to exit, /help-style commands listed inside)")
        rc = cli.cmd_tui(argparse.Namespace(prompt=idea or None))
        if rc:
            return rc
    return 0


# ===========================================================================
# Setup wizard (--setup) — port of _clk_setup / _clk_setup_github
# ===========================================================================


class _SetupIO:
    """Prompt I/O against /dev/tty when available (so the wizard works
    inside ``docker run -it`` even when stdin is piped), else stdin/stderr."""

    def __init__(self) -> None:
        tty = _open_tty()
        if tty is not None:
            self.fin: TextIO = tty
            self.fout: TextIO = tty
        else:
            self.fin = sys.stdin
            self.fout = sys.stderr

    def say(self, msg: str = "") -> None:
        print(msg, file=self.fout)

    def explain(self, text: str) -> None:
        # Explain-then-ask: tell the user what the value does before asking.
        self.say("\n" + text)

    def _readline(self) -> str:
        line = self.fin.readline()
        return line.rstrip("\n") if line else ""

    def read(self, prompt: str, default: str = "") -> str:
        if default:
            self.fout.write(f"{prompt} [{default}]: ")
        else:
            self.fout.write(f"{prompt}: ")
        self.fout.flush()
        return self._readline() or default

    def secret(self, prompt: str) -> str:
        self.fout.write(f"{prompt} (leave blank to keep): ")
        self.fout.flush()
        try:
            import termios
            fd = self.fin.fileno()
            old = termios.tcgetattr(fd)
            new = list(old)
            new[3] = new[3] & ~termios.ECHO
            termios.tcsetattr(fd, termios.TCSADRAIN, new)
            try:
                value = self._readline()
            finally:
                termios.tcsetattr(fd, termios.TCSADRAIN, old)
        except Exception:
            value = self._readline()
        self.say("")
        return value

    def confirm(self, prompt: str, default: str = "N") -> bool:
        hint = "Y/n" if default.upper() in ("Y", "YES") else "y/N"
        self.fout.write(f"{prompt} [{hint}]: ")
        self.fout.flush()
        answer = self._readline() or default
        return answer.lower() in ("y", "yes")


def _load_env_into(target: MutableMapping[str, str], path: Path) -> None:
    """Mimic `set -a; . <file>; set +a`: every assignment is exported."""
    if not path.is_file():
        return
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return
    for line in env_file.parse_env(text):
        if line.kind == "kv":
            target[line.key] = line.value


def _env_set(key: str, value: str) -> None:
    """Persist one KEY=VALUE to .env atomically and export it."""
    env_file.write_env({key: value})
    os.environ[key] = value


def _install_tool_call(script_dir: Path, func: str, *fargs: str) -> int:
    """Run one function from scripts/install_tool.sh (single source of
    truth for provider CLI install/config) with the wizard's terminal."""
    script = script_dir / "scripts" / "install_tool.sh"
    if not script.is_file():
        return 127
    # $0 must differ from the script path, otherwise install_tool.sh's
    # `BASH_SOURCE[0] = $0` direct-dispatch guard fires while sourcing.
    cmd = ["bash", "-c", 'source "$1" && shift && "$@"', "clk-kickoff",
           str(script), func, *fargs]
    return subprocess.run(cmd, check=False).returncode


def run_setup_wizard(script_dir: Path) -> int:
    """Interactive wizard to write or update .env.

    Same design as the shell wizard it replaces:
      * Explain-then-ask before every prompt.
      * Atomic writes: each answer persists to .env immediately (via
        env_file.write_env), so Ctrl-C never leaves a half-written file.
      * Per-step resume: .clk/.setup-progress records the last completed
        step; the next run offers to skip ahead.
      * Always-confirm: installs/pushes prompt y/N every time.
    """
    io = _SetupIO()

    env_path = Path(os.environ.get("CLK_ENV_FILE") or (script_dir / ".env"))
    os.environ["CLK_ENV_FILE"] = str(env_path)
    progress_file = script_dir / ".clk" / ".setup-progress"
    progress_file.parent.mkdir(parents=True, exist_ok=True)

    # Seed defaults from .env.example first, then let an existing .env override.
    _load_env_into(os.environ, script_dir / ".env.example")
    if env_path.is_file() and env_path.stat().st_size > 0:
        _load_env_into(os.environ, env_path)
        io.say(f"[setup] loaded existing values from {env_path}")
    else:
        io.say(f"[setup] {env_path} is empty or missing — using .env.example defaults")

    # Per-step resume.
    last_step = ""
    if progress_file.is_file():
        lines = [ln for ln in progress_file.read_text(encoding="utf-8").splitlines() if ln.strip()]
        if lines:
            last_step = lines[-1]
    skip_until = ""
    if last_step:
        if io.confirm(f"[setup] Resume from after step '{last_step}'?", "Y"):
            skip_until = last_step
        else:
            progress_file.write_text("", encoding="utf-8")

    io.say("\n=== CLK Setup Wizard ===")
    io.say("Press Enter to keep the value shown in [brackets].")
    io.say("Every install, push, and destructive action is confirmed y/N first.")

    def mark_step(name: str) -> None:
        with progress_file.open("a", encoding="utf-8") as fh:
            fh.write(name + "\n")

    def should_run_step(name: str) -> bool:
        nonlocal skip_until
        if not skip_until:
            return True
        if name == skip_until:
            skip_until = ""  # stop skipping after this match
        return False

    env = os.environ  # exported view, mirrors `set -a`

    # --- provider --------------------------------------------------------
    if should_run_step("provider"):
        io.explain(
            "=== Provider ===\n"
            "The provider is the AI that actually writes your code each cycle.\n"
            "\n"
            "  shell      no AI — useful for smoke tests and the /tutorial walkthrough\n"
            "  claude     Anthropic Claude Code CLI (best at writing code, supports tools)\n"
            "  codex      OpenAI Codex CLI\n"
            "  gemini     Google Gemini CLI\n"
            "  pi         Pi terminal harness — routes through OpenRouter/Anthropic/OpenAI/Google\n"
            "  ollama     local LLM via the Ollama daemon (no external API, free)\n"
            "  openwebui  OpenWebUI server (self-hosted, OpenAI-compatible)"
        )
        provider = io.read("Provider", env.get("CLK_PROVIDER") or "shell")
        _env_set("CLK_PROVIDER", provider)
        mark_step("provider")
    else:
        provider = env.get("CLK_PROVIDER") or "shell"

    # --- max iterations + project name + flags ---------------------------
    if should_run_step("loop_settings"):
        io.explain(
            "=== Loop settings ===\n"
            "`max iterations` caps how many refinement cycles the Ralph and\n"
            "autoresearch loops can run. `project name` becomes the title of the\n"
            "captured idea and (optionally) the GitHub repo name. The `run install`\n"
            "flag triggers .clk/harness/scripts/install_local.sh inside each kickoff\n"
            "dir so providers like pi can find PyYAML and other deps — leave it\n"
            "`false` (the default) when running inside Docker, because the image\n"
            "already has all Python dependencies installed at build time.\n"
            "`no TUI` switches to a non-interactive pipeline — handy for CI."
        )
        proj_name = ""
        _env_set("CLK_MAX_ITERATIONS", io.read("Max loop iterations", env.get("CLK_MAX_ITERATIONS") or "10"))
        proj_name = io.read("Project name", env.get("CLK_PROJECT_NAME") or "clk-app")
        _env_set("CLK_PROJECT_NAME", proj_name)
        _env_set("CLK_RUN_INSTALL", io.read(
            "Run install_local.sh in each kickoff (true|false)", env.get("CLK_RUN_INSTALL") or "false"))
        _env_set("CLK_NO_TUI", io.read(
            "Skip TUI / non-interactive (true|false)", env.get("CLK_NO_TUI") or "false"))
        mark_step("loop_settings")
    else:
        proj_name = env.get("CLK_PROJECT_NAME") or "clk-app"

    # --- auth mode (CLI providers) ---------------------------------------
    if provider in ("claude", "codex", "gemini"):
        if should_run_step("auth_mode"):
            io.explain(
                f"=== Auth mode ({provider}) ===\n"
                f"'cli'    — use your existing local CLI login (run `{provider} login` once;\n"
                f"           best when you already use {provider} day-to-day).\n"
                "'apikey' — call the provider's HTTP API directly using an API key.\n"
                "           No CLI dependency, but you must paste a key below."
            )
            _env_set("CLK_AUTH_MODE", io.read("Auth mode", env.get("CLK_AUTH_MODE") or "cli"))
            mark_step("auth_mode")

    # --- install + configure the chosen tool -----------------------------
    if should_run_step("tool_setup"):
        if provider != "shell":
            io.explain(
                f"=== Tool detection ({provider}) ===\n"
                f"Checking whether `{provider}` is installed and reachable. If it\n"
                "isn't, the wizard will suggest an install command and ask before\n"
                "running it. After the tool is available, we'll walk through\n"
                "first-use config (auth -> route -> model -> verify)."
            )
            if _install_tool_call(script_dir, "check_tool", provider) == 0:
                io.say(f"[setup] {provider} is available on this machine.")
            else:
                if _install_tool_call(script_dir, "install_tool", provider, "--prompt") != 0:
                    io.say(f"[setup] {provider} install was skipped or failed; continuing.")
            if _install_tool_call(script_dir, "check_tool", provider) == 0:
                already = _install_tool_call(script_dir, "tool_configured", provider) == 0
                if already and not io.confirm(f"Re-run first-use config for {provider}?", "N"):
                    io.say(f"[setup] {provider} already configured (per .clk/state/configured-tools.json).")
                else:
                    if _install_tool_call(script_dir, "configure_tool", provider) != 0:
                        io.say(f"[setup] {provider} configure step exited non-zero; continuing.")
            else:
                io.say(f"[setup] {provider} is still unavailable — provider calls will fail until you install it.")
        mark_step("tool_setup")
        # Reload .env so values just written by configure_tool become
        # visible to the rest of the wizard.
        _load_env_into(os.environ, env_path)

    # --- docker host fallback for local LLM endpoints --------------------
    if should_run_step("docker_host_fallback"):
        io.explain(
            "=== Local LLM endpoint check ===\n"
            "If you have ollama or OpenWebUI running on the host but CLK is in a\n"
            "container, 'localhost' won't reach them. We'll probe each configured\n"
            "endpoint and, when only host.docker.internal works, offer to switch."
        )
        _install_tool_call(
            script_dir, "_it_offer_docker_host_fallback", "Ollama", "CLK_OLLAMA_ENDPOINT",
            env.get("CLK_OLLAMA_ENDPOINT") or "http://localhost:11434")
        _install_tool_call(
            script_dir, "_it_offer_docker_host_fallback", "OpenWebUI", "CLK_OPENWEBUI_ENDPOINT",
            env.get("CLK_OPENWEBUI_ENDPOINT") or "http://localhost:8080")
        mark_step("docker_host_fallback")
        _load_env_into(os.environ, env_path)

    # --- telegram --------------------------------------------------------
    tg_setup = "N"
    if should_run_step("telegram"):
        io.explain(
            "=== Telegram bot (optional) ===\n"
            "If enabled, you can drive CLK from your phone: send the bot an idea,\n"
            "get progress updates back, /stop or /abort remotely. The dedicated\n"
            "wizard at scripts/telegram_setup_wizard.sh walks through BotFather\n"
            "token creation and discovers your numeric user ID so we can allowlist\n"
            "only you."
        )
        default_tg = "y" if env.get("CLK_TELEGRAM_ENABLED", "false") == "true" else "N"
        tg_setup = io.read("Set up Telegram bot now? (y/N)", default_tg)
        if tg_setup.lower() == "y":
            _env_set("CLK_TELEGRAM_SKIP", "false")
        else:
            io.say("[setup] Skipping Telegram. CLK_TELEGRAM_SKIP=true will be written to .env.")
            _env_set("CLK_TELEGRAM_SKIP", "true")
        mark_step("telegram")

    # --- GitHub ----------------------------------------------------------
    if should_run_step("github"):
        _setup_github(io, script_dir, proj_name)
        mark_step("github")

    # --- git identity ----------------------------------------------------
    if should_run_step("git_identity"):
        io.explain(
            "=== Git identity (used in kickoff commits) ===\n"
            "Each kickoff workspace is its own git repo and CLK auto-commits after\n"
            "every successful agent run. The author/committer comes from your\n"
            "global git config unless you set CLK_GIT_NAME / CLK_GIT_EMAIL here\n"
            "(useful inside containers where the global config doesn't propagate)."
        )
        cur_name = subprocess.run(
            ["git", "config", "--global", "user.name"],
            capture_output=True, text=True, check=False).stdout.strip()
        cur_email = subprocess.run(
            ["git", "config", "--global", "user.email"],
            capture_output=True, text=True, check=False).stdout.strip()
        io.say(f"  Current global git name:  {cur_name or '<not set>'}")
        io.say(f"  Current global git email: {cur_email or '<not set>'}")
        _env_set("CLK_GIT_NAME", io.read("Git user.name  (blank = keep current)", env.get("CLK_GIT_NAME", "")))
        _env_set("CLK_GIT_EMAIL", io.read("Git user.email (blank = keep current)", env.get("CLK_GIT_EMAIL", "")))
        mark_step("git_identity")

    io.say(f"\n[setup] saved {env_path}")
    io.say(f"[setup] previous values are in {env_path}.bak")

    if tg_setup.lower() == "y":
        io.say("\n[setup] launching Telegram wizard...")
        wizard = script_dir / "scripts" / "telegram_setup_wizard.sh"
        rc = subprocess.run(
            ["bash", str(wizard)],
            env={**os.environ, "CLK_ENV_FILE": str(env_path)},
            check=False,
        ).returncode if wizard.is_file() else 127
        if rc != 0:
            io.say("[setup] telegram wizard exited non-zero; continuing")

    # Wizard finished cleanly — clear progress so a future --setup starts
    # at the top instead of asking to resume.
    try:
        progress_file.unlink()
    except OSError:
        pass
    return 0


def _setup_github(io: _SetupIO, script_dir: Path, proj_name: str) -> None:
    """GitHub connection block — port of _clk_setup_github."""
    env = os.environ
    io.explain(
        "=== GitHub (optional) ===\n"
        "Each kickoff workspace is already a local git repo. You can optionally\n"
        "connect it to a GitHub remote so:\n"
        "  - every agent commit is checkpointed off your machine\n"
        "  - you (or another machine) can resume the work later by cloning\n"
        "  - friends/teammates can review the run\n"
        "\n"
        "  skip       no GitHub — local commits only (default)\n"
        "  existing   connect to a repo you already own (paste URL)\n"
        "  create     create a brand new private repo under your account\n"
        "\n"
        "The wizard will write a hardened .gitignore (blocking .env, .env.bak,\n"
        "SSH keys, etc.) and install a pre-push hook that aborts when an\n"
        "obvious API key pattern appears in the diff."
    )
    choice = io.read("Connect to GitHub?", env.get("CLK_GITHUB_MODE") or "skip")
    if choice in ("skip", ""):
        _env_set("CLK_GITHUB_MODE", "skip")
        _env_set("CLK_GITHUB_REMOTE", "")
        _env_set("CLK_GITHUB_PUSH_ON_COMMIT", "false")
        io.say("[setup] GitHub disabled.")
        return
    if choice == "existing":
        url = io.read(
            "Existing repo (https://github.com/OWNER/REPO or git@github.com:OWNER/REPO.git)",
            env.get("CLK_GITHUB_REMOTE", ""))
        if not url:
            io.say("[setup] no URL provided; skipping GitHub.")
            _env_set("CLK_GITHUB_MODE", "skip")
            return
        _env_set("CLK_GITHUB_MODE", "existing")
        _env_set("CLK_GITHUB_REMOTE", url)
    elif choice == "create":
        if not shutil.which("gh"):
            io.say("[setup] gh CLI is required to create a repo from here.")
            if _install_tool_call(script_dir, "install_tool", "gh", "--prompt") != 0:
                io.say('[setup] gh unavailable; cannot create. Falling back to "existing" — paste a URL.')
                url = io.read("Existing repo URL", "")
                if url:
                    _env_set("CLK_GITHUB_MODE", "existing")
                    _env_set("CLK_GITHUB_REMOTE", url)
                else:
                    _env_set("CLK_GITHUB_MODE", "skip")
                return
        if subprocess.run(["gh", "auth", "status"], capture_output=True, check=False).returncode != 0:
            io.say("[setup] gh is installed but not authenticated.")
            if io.confirm("Run `gh auth login` now?", "Y"):
                subprocess.run(["gh", "auth", "login"], check=False)
        who = subprocess.run(
            ["gh", "api", "user", "--jq", ".login"],
            capture_output=True, text=True, check=False)
        default_or = who.stdout.strip() if who.returncode == 0 and who.stdout.strip() else (
            env.get("USER") or "user")
        owner_repo = io.read("owner/repo to create", f"{default_or}/{proj_name}-kickoff")
        _env_set("CLK_GITHUB_MODE", "create")
        _env_set("CLK_GITHUB_REMOTE", owner_repo)
        io.say(f'[setup] GitHub repo "{owner_repo}" will be created (private) on the first kickoff push.')
    else:
        io.say(f'[setup] unknown GitHub choice "{choice}"; skipping.')
        _env_set("CLK_GITHUB_MODE", "skip")
        return

    if io.confirm("Auto-push to GitHub after every CLK commit?", "Y"):
        _env_set("CLK_GITHUB_PUSH_ON_COMMIT", "true")
    else:
        _env_set("CLK_GITHUB_PUSH_ON_COMMIT", "false")


# ===========================================================================
# Entry point
# ===========================================================================


def cmd_kickoff(args: argparse.Namespace) -> int:
    script_dir = harness_root()

    # Early-exit modes (mirror the shell's flag handlers).
    if getattr(args, "restore", False):
        return _cmd_restore(script_dir)
    if getattr(args, "list_mode", False):
        return _cmd_list()
    if getattr(args, "clean", None):
        return _cmd_clean(args.clean)
    if getattr(args, "setup", False):
        rc = run_setup_wizard(script_dir)
        if rc == 0:
            print("[setup] run  'clk kickoff'  to start a new session", file=sys.stderr)
        return rc

    idea = args.idea or ""

    # First-run nudge: if .env is missing and we have a TTY, offer setup
    # inline. Declining falls through to defaults. CI / non-interactive
    # containers skip silently.
    env_path = script_dir / ".env"
    if not (env_path.is_file() and env_path.stat().st_size > 0):
        tty = _open_tty()
        if tty is not None:
            try:
                if env_path.is_file():
                    print(f"[kickoff] {env_path} is empty (placeholder) — first run?", file=sys.stderr)
                else:
                    print(f"[kickoff] No .env found at {env_path} — first run?", file=sys.stderr)
                tty.write("[kickoff] Run --setup now to configure? [Y/n]: ")
                tty.flush()
                ans = (tty.readline() or "").strip().lower()
            finally:
                tty.close()
            if ans in ("", "y", "yes"):
                run_setup_wizard(script_dir)
            else:
                print("[kickoff] Skipping setup; continuing with defaults.", file=sys.stderr)

    # Load .env (export every assigned var so subprocesses inherit it).
    if env_path.is_file():
        print(f"[kickoff] loading {env_path}")
        _load_env_into(os.environ, env_path)

    # Apply git identity overrides from .env (useful inside Docker).
    if os.environ.get("CLK_GIT_NAME"):
        subprocess.run(["git", "config", "--global", "user.name", os.environ["CLK_GIT_NAME"]],
                       capture_output=True, check=False)
    if os.environ.get("CLK_GIT_EMAIL"):
        subprocess.run(["git", "config", "--global", "user.email", os.environ["CLK_GIT_EMAIL"]],
                       capture_output=True, check=False)

    def resolve_cfg() -> Dict[str, str]:
        """--arg overrides over env/.env, then built-in defaults."""
        cfg = {key: os.environ.get(key, "") for key, _ in _DEFAULTS}
        if getattr(args, "provider", None):
            cfg["CLK_PROVIDER"] = args.provider
        if getattr(args, "max_iterations", None):
            cfg["CLK_MAX_ITERATIONS"] = str(args.max_iterations)
        if getattr(args, "project_name", None):
            cfg["CLK_PROJECT_NAME"] = args.project_name
        if getattr(args, "no_tui_override", None):
            cfg["CLK_NO_TUI"] = args.no_tui_override
        if getattr(args, "run_install", False):
            cfg["CLK_RUN_INSTALL"] = "true"
        _apply_defaults(cfg)
        return cfg

    cfg = resolve_cfg()

    # Validate; if anything is missing, offer --setup then retry or exit.
    missing = _missing_config(cfg, os.environ, idea)
    if missing:
        print("[kickoff] Cannot start — missing or invalid configuration:\n", file=sys.stderr)
        for line in missing:
            print(f"  • {line}", file=sys.stderr)
        print("", file=sys.stderr)

        do_setup = False
        tty = _open_tty()
        if tty is not None:
            try:
                print("[kickoff] Run  'clk kickoff --setup'  to configure, or answer below.", file=sys.stderr)
                tty.write("[kickoff] Run --setup now? [y/N]: ")
                tty.flush()
                do_setup = (tty.readline() or "").strip().lower() == "y"
            finally:
                tty.close()
        else:
            print("[kickoff] Re-run with  'clk kickoff --setup'  to configure interactively.", file=sys.stderr)

        if not do_setup:
            return 2
        run_setup_wizard(script_dir)
        # Reload .env and re-apply overrides + defaults.
        _load_env_into(os.environ, env_path)
        cfg = resolve_cfg()
        missing = _missing_config(cfg, os.environ, idea)
        if missing:
            print("[kickoff] Still missing after setup — cannot continue:\n", file=sys.stderr)
            for line in missing:
                print(f"  • {line}", file=sys.stderr)
            return 2

    # Create the kickoff directory under workspace/ and drive the harness.
    kickoff_dir = _create_kickoff_dir(script_dir, cfg, idea)
    if kickoff_dir is None:
        return 1
    workspace_dir = kickoff_dir.parent
    _prepare_kickoff_repo(kickoff_dir, os.environ)

    rc = _drive_harness(kickoff_dir, cfg, idea)
    if rc:
        return rc

    print()
    print("[kickoff] complete")
    print(f"[kickoff] kickoff dir: {kickoff_dir}")
    print(f'[kickoff] inspect:     cd "{kickoff_dir}" && ./.clk/scripts/clk status')
    print(f"[kickoff] workspace:   {workspace_dir}")
    print(f'[kickoff] reset:       rm -rf "{kickoff_dir}"')
    return 0


__all__ = [
    "CONFIG_ENV_OVERRIDES",
    "VALID_PROVIDERS",
    "activate_provider",
    "apply_config_env_overrides",
    "cmd_kickoff",
    "harness_root",
    "run_setup_wizard",
]
