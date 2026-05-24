"""Configuration management for CLK.

CLK keeps every artifact under ``.clk/`` inside the project directory.
This module owns:
  * locating the project root (the dir containing ``.clk/`` or the cwd)
  * reading and writing ``.clk/config/clk.config.json``
  * reading provider and agent registries
  * exposing a :class:`Paths` helper with absolute filesystem paths
"""

from __future__ import annotations

import json
import os
import sys
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Optional


CLK_DIR_NAME = ".clk"


@dataclass
class Paths:
    """Resolved filesystem layout for a CLK project.

    The harness lives under ``.clk/`` (config, state, logs, runs,
    blackboard, and the harness sources copied in by ``kickoff.sh``).
    The actual product the agents are building lives at the project
    root, side-by-side with the user's existing repo layout. Keeping
    them separate means:

      * Agents address files at the project root naturally — no
        ``workspace/`` indirection — and the project tree looks like
        a normal codebase.
      * Action paths emitted by agents resolve at project root but
        the harness rejects any path that targets ``.clk/`` so the
        sandbox stays intact.
      * The user can ``rm -rf .clk`` to reset all harness state
        (memory, runs, blackboard, harness sources) without touching
        the project itself.

    ``Paths.workspace`` is retained as an alias for ``Paths.root`` so
    older call sites keep working; new code should prefer ``root``.
    """

    root: Path
    clk: Path = field(init=False)
    config: Path = field(init=False)
    workflows: Path = field(init=False)
    prompts: Path = field(init=False)
    state: Path = field(init=False)
    logs: Path = field(init=False)
    tools: Path = field(init=False)
    venv: Path = field(init=False)
    runs: Path = field(init=False)
    backups: Path = field(init=False)
    cache: Path = field(init=False)
    blackboard: Path = field(init=False)
    harness: Path = field(init=False)
    workspace: Path = field(init=False)

    def __post_init__(self) -> None:
        self.clk = self.root / CLK_DIR_NAME
        self.config = self.clk / "config"
        self.workflows = self.config / "workflows"
        self.prompts = self.clk / "prompts"
        self.state = self.clk / "state"
        self.logs = self.clk / "logs"
        self.tools = self.clk / "tools"
        self.venv = self.clk / "venv"
        self.runs = self.clk / "runs"
        self.backups = self.clk / "backups"
        self.cache = self.clk / "cache"
        self.blackboard = self.clk / "blackboard"
        self.harness = self.clk / "harness"
        # Backward-compat alias: agents now operate at the project root,
        # so paths.workspace == paths.root. Old call sites keep working.
        self.workspace = self.root

    def ensure(self) -> None:
        """Create all directories. Idempotent."""
        for p in [
            self.clk,
            self.config,
            self.workflows,
            self.prompts,
            self.state,
            self.logs,
            self.tools,
            self.runs,
            self.backups,
            self.cache,
            self.blackboard,
        ]:
            try:
                p.mkdir(parents=True, exist_ok=True)
            except Exception as exc:
                print(f"[config.Paths.ensure] failed to create {p}: {exc}", file=sys.stderr)
                traceback.print_exc()


def find_project_root(start: Optional[Path] = None) -> Path:
    """Walk up from ``start`` (default: cwd) looking for a ``.clk`` directory.

    Returns the directory that contains ``.clk`` if found, else ``start``.
    """
    here = (start or Path.cwd()).resolve()
    cur = here
    while True:
        if (cur / CLK_DIR_NAME).is_dir():
            return cur
        if cur.parent == cur:
            return here
        cur = cur.parent


def load_json(path: Path, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not path.exists():
        return dict(default or {})
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:
        print(f"[config.load_json] failed to read {path}: {exc}", file=sys.stderr)
        traceback.print_exc()
        return dict(default or {})


def save_json(path: Path, data: Dict[str, Any], *, backup: bool = True) -> None:
    """Atomically write JSON to ``path``.

    Writes to a sibling tempfile, fsyncs, rotates the previous file to
    ``path.bak`` (when ``backup``), then renames into place. A Ctrl-C
    between any two of those steps leaves either the old file or the new
    file intact — never a torn write.
    """
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        body = json.dumps(data, indent=2, sort_keys=True) + "\n"
        tmp = path.with_name(path.name + ".tmp")
        # Use os.open so we can fsync the file descriptor before closing.
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o644)
        try:
            os.write(fd, body.encode("utf-8"))
            try:
                os.fsync(fd)
            except OSError:
                pass
        finally:
            os.close(fd)
        if backup and path.exists():
            try:
                path.replace(path.with_name(path.name + ".bak"))
            except OSError:
                pass
        os.replace(str(tmp), str(path))
    except Exception as exc:
        print(f"[config.save_json] failed to write {path}: {exc}", file=sys.stderr)
        traceback.print_exc()


# Default config payloads -----------------------------------------------------

DEFAULT_CLK_CONFIG: Dict[str, Any] = {
    "version": 1,
    "project_name": None,
    "default_provider": "shell",
    "default_workflow": "engineering",
    "max_iterations": 20,
    "dry_run": False,
    "auto_commit": True,
    "provider_timeout_s": 0,
    "provider_no_output_timeout_s": 0,
    "provider_retry": {
        "max_retries": 10,
        "backoff_s": 5,
        "stage_max_retries": 10,
        "stage_backoff_s": 30,
    },
    "supervise": {
        "max_cycles": 20,
    },
    "consensus": {
        "max_samples": 6,
        "max_parallel": 4,
    },
    "validation": {
        "max_files_per_batch": 25,
        "warn_files_per_batch": 5,
    },
    "casting": {
        "max_dynamic_roles": 12,
        "auto_cast_on_idea": True,
    },
    "robustness": {
        # Sub-sub-agent fan-out on dispatch.
        # off | on_careful (fan out only stages marked careful=true) | always.
        "auto_consensus": "on_careful",
        # Critic-judge inner refinement loop.
        # off | careful_only (default) | all
        "auto_refine": "careful_only",
        # Cap on automatic re-dispatch attempts after a quality failure.
        "max_quality_retries": 2,
        # Below this, responses are treated as suspect and may be re-run.
        "min_response_chars": 40,
        # Critic-judge loop bounds.
        "refine_max_rounds": 3,
        "refine_accept_threshold": 0.8,
        # Inter-agent Q&A bounds.
        "qa_parallel_judges": 1,
        "max_qa_depth": 3,
        # Ralph / autoresearch plateau detection.
        "plateau_window": 3,
        # escalate_then_reframe | escalate_only | reframe_only | off
        "plateau_action": "escalate_then_reframe",
    },
}

DEFAULT_PROVIDERS: Dict[str, Any] = {
    "providers": {
        "shell": {
            "type": "shell",
            "description": "Dummy provider that echoes prompts. Always available.",
            "command": None,
        },
        "claude": {
            "type": "claude",
            "description": "Claude Code CLI. Detected via 'claude' on PATH.",
            "command": "claude",
            "args": ["--print"],
        },
        "codex": {
            "type": "codex",
            "description": "OpenAI Codex CLI. Detected via 'codex' on PATH.",
            "command": "codex",
            "args": ["exec"],
        },
        "gemini": {
            "type": "gemini",
            "description": "Google Gemini CLI. Detected via 'gemini' on PATH.",
            "command": "gemini",
            "args": [],
        },
        "pi": {
            "type": "pi",
            "description": "Pi terminal harness. Cloned to .clk/tools/pi if needed.",
            "command": "pi",
            "args": [],
            "model": "",
            "api_key": "",
            "key_type": "openrouter",
        },
        "ollama": {
            "type": "ollama",
            "description": "Local Ollama HTTP API.",
            "endpoint": "http://localhost:11434",
            "model": "llama3.1",
        },
        "openwebui": {
            "type": "openwebui",
            "description": "OpenWebUI server (OpenAI-compatible HTTP). Set host/key/model on kickoff.",
            "endpoint": "http://localhost:8080",
            "api_key": "",
            "model": "",
        },
    },
    "active": "shell",
}

DEFAULT_AGENTS: Dict[str, Any] = {
    # Only the immutable baseline ships in agents.json. The chief authors
    # the rest of the roster dynamically once an idea is captured, including
    # the `engineer` role.  Other prompt templates (engineer.md, researcher.md,
    # analyst.md, ...) still ship to disk as scaffolds so the chief can cast a
    # role with an empty PROMPT body and the existing file will be picked up.
    "agents": {
        "chief": {"prompt": "chief.md", "provider": None, "role": "decompose objectives, cast the team, author workflows"},
        "qa":    {"prompt": "qa.md",    "provider": None, "role": "test and audit changes (baseline validator)"},
        "ralph": {"prompt": "ralph.md", "provider": None, "role": "drive iterative refinement and autoresearch loops"},
    }
}


def write_default_configs(paths: Paths, project_name: Optional[str] = None) -> None:
    """Write default config files only if absent. Idempotent."""
    from .templates.prompts import PROMPTS
    from .utils.activity_log import log_event

    cfg_path = paths.config / "clk.config.json"
    if not cfg_path.exists():
        cfg = dict(DEFAULT_CLK_CONFIG)
        cfg["project_name"] = project_name or paths.root.name
        save_json(cfg_path, cfg)

    prov_path = paths.config / "providers.json"
    if not prov_path.exists():
        save_json(prov_path, DEFAULT_PROVIDERS)

    agents_path = paths.config / "agents.json"
    if not agents_path.exists():
        save_json(agents_path, DEFAULT_AGENTS)
        for name, cfg in sorted((DEFAULT_AGENTS.get("agents") or {}).items()):
            prompt_file = cfg.get("prompt") or f"{name}.md"
            prompt = PROMPTS.get(prompt_file, "")
            log_event(
                paths,
                "default_agent_created",
                agent=name,
                action="default_agent_created",
                prompt_file=prompt_file,
                role=cfg.get("role", ""),
                provider=cfg.get("provider"),
                system_prompt=prompt,
                prompt_chars=len(prompt),
            )


def load_clk_config(paths: Paths) -> Dict[str, Any]:
    cfg = dict(DEFAULT_CLK_CONFIG)
    cfg.update(load_json(paths.config / "clk.config.json", DEFAULT_CLK_CONFIG))
    return cfg


def load_providers_config(paths: Paths) -> Dict[str, Any]:
    return load_json(paths.config / "providers.json", DEFAULT_PROVIDERS)


def load_agents_config(paths: Paths) -> Dict[str, Any]:
    return load_json(paths.config / "agents.json", DEFAULT_AGENTS)


def save_agents_config(paths: Paths, data: Dict[str, Any]) -> None:
    save_json(paths.config / "agents.json", data)


def save_providers_config(paths: Paths, data: Dict[str, Any]) -> None:
    save_json(paths.config / "providers.json", data)


def project_paths(start: Optional[Path] = None) -> Paths:
    return Paths(root=find_project_root(start))


def is_initialized(paths: Paths) -> bool:
    return (paths.config / "clk.config.json").exists()
