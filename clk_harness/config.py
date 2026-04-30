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

    The harness lives under ``.clk/`` (config, state, logs, runs).
    The actual product the agents are building lives under
    ``workspace/``. Keeping them separate means:

      * git history in the kickoff dir tells the project's story
        without harness chatter (we gitignore everything outside
        workspace/ and a few state files).
      * Action paths emitted by agents resolve under ``workspace/``,
        so a confused agent can't write into the harness.
      * The user can ``rm -rf workspace`` to reset the build without
        losing memory in ``.clk/``.
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
        self.workspace = self.root / "workspace"

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
            self.workspace,
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


def save_json(path: Path, data: Dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n", encoding="utf-8")
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
    "provider_timeout_s": 300,
    "provider_no_output_timeout_s": 240,
    "provider_retry": {
        "max_retries": 2,
        "backoff_s": 5,
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
    # the rest of the roster dynamically once an idea is captured. Other
    # prompt templates (researcher.md, analyst.md, ...) still ship to disk
    # as scaffolds so the chief can re-cast a seed role with an empty
    # PROMPT body and the existing file will be picked up.
    "agents": {
        "chief":        {"prompt": "chief.md",        "provider": None, "role": "decompose objectives, cast the team, author workflows"},
        "engineer":     {"prompt": "engineer.md",     "provider": None, "role": "implement vertical slices (baseline implementer)"},
        "qa":           {"prompt": "qa.md",           "provider": None, "role": "test and audit changes (baseline validator)"},
        "ralph":        {"prompt": "ralph.md",        "provider": None, "role": "drive ralph-style iterative loops"},
        "autoresearch": {"prompt": "autoresearch.md", "provider": None, "role": "drive autoresearch-style improvement"},
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
