"""Python read/write helper + UI schema for the project ``.env`` file.

Historically the ``.env`` file was only ever read/written by the shell
helpers in ``scripts/lib_env.sh`` (sourced by ``kickoff.sh``). The web
UI needs to render a friendly, grouped settings form and persist edits,
so this module provides:

  * a tolerant, comment-preserving ``.env`` parser/writer with the same
    atomic ``tmp + fsync + .bak + os.replace`` discipline as
    :func:`clk_harness.config.save_json`;
  * a secret-key heuristic (so API keys/tokens are masked in the UI and
    never echoed back unless explicitly revealed);
  * ``ENV_SCHEMA`` — a hand-authored description of every known
    ``CLK_*`` / API-key variable (group, label, type, choices, default,
    help) derived from ``.env.example`` so the front end can render
    typed widgets without hard-coding anything client-side.

The file location resolves from ``CLK_ENV_FILE`` if set, else ``.env``
at the repository root (the directory two levels up from this module:
``<repo>/clk_harness/env_file.py`` -> ``<repo>/.env``).
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

# Sentinel returned in place of secret values and accepted on write to
# mean "leave the stored value unchanged". Chosen to be visually obvious
# and extremely unlikely to be a real secret.
MASK_SENTINEL = "••••••••"  # ••••••••

# Substrings (uppercased key) that mark a variable as secret.
_SECRET_SUBSTRINGS = ("SECRET", "PASSWORD", "PASS", "TOKEN", "_KEY", "APIKEY")

# Keys forced secret even if the heuristic would miss them.
_FORCE_SECRET = {
    "ANTHROPIC_API_KEY",
    "OPENAI_API_KEY",
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "MISTRAL_API_KEY",
    "OPENROUTER_API_KEY",
    "CLK_OPENWEBUI_API_KEY",
    "CLK_PI_API_KEY",
    "CLK_TELEGRAM_BOT_TOKEN",
}

# Keys forced non-secret even though they contain a secret-ish substring
# (e.g. CLK_PI_KEY_TYPE names a provider, it is not itself a credential).
_FORCE_PLAIN = {
    "CLK_PI_KEY_TYPE",
}


def is_secret_key(name: str) -> bool:
    """Return True if ``name`` looks like it holds a credential."""
    upper = (name or "").upper()
    if upper in _FORCE_PLAIN:
        return False
    if upper in _FORCE_SECRET:
        return True
    return any(sub in upper for sub in _SECRET_SUBSTRINGS)


def mask_value(value: str) -> str:
    """Mask a secret value for display. Empty stays empty (so the UI can
    show 'unset')."""
    if not value:
        return ""
    return MASK_SENTINEL


def env_path() -> Path:
    """Resolve the canonical ``.env`` path.

    ``CLK_ENV_FILE`` wins; otherwise ``<repo-root>/.env``.
    """
    override = os.environ.get("CLK_ENV_FILE")
    if override:
        return Path(override).expanduser()
    repo_root = Path(__file__).resolve().parent.parent
    return repo_root / ".env"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


@dataclass
class EnvLine:
    """One physical line of a ``.env`` file, preserved for round-tripping."""

    kind: str  # "kv" | "comment" | "blank"
    raw: str = ""
    key: str = ""
    value: str = ""


def _strip_quotes(value: str) -> str:
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def parse_env(text: str) -> List[EnvLine]:
    """Parse ``.env`` text into ordered :class:`EnvLine` records.

    Comments and blank lines are preserved verbatim. ``export KEY=...``
    and surrounding quotes on values are handled. Lines that don't look
    like assignments are kept as ``comment`` so nothing is ever lost.
    """
    lines: List[EnvLine] = []
    for raw in text.splitlines():
        stripped = raw.strip()
        if not stripped:
            lines.append(EnvLine("blank", raw=""))
            continue
        if stripped.startswith("#"):
            lines.append(EnvLine("comment", raw=raw))
            continue
        body = stripped
        if body.startswith("export "):
            body = body[len("export "):]
        if "=" not in body:
            # Not an assignment we understand — preserve as a comment.
            lines.append(EnvLine("comment", raw=raw))
            continue
        key, _, value = body.partition("=")
        key = key.strip()
        if not key:
            lines.append(EnvLine("comment", raw=raw))
            continue
        # Drop a trailing inline comment only when the value is unquoted.
        val = value
        if val and val.lstrip()[:1] not in ("'", '"') and " #" in val:
            val = val.split(" #", 1)[0]
        lines.append(EnvLine("kv", raw=raw, key=key, value=_strip_quotes(val)))
    return lines


def read_env_lines() -> List[EnvLine]:
    path = env_path()
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except Exception as exc:  # noqa: BLE001
        print(f"[env_file.read_env_lines] failed for {path}: {exc}", file=sys.stderr)
        return []
    return parse_env(text)


def read_env() -> Dict[str, str]:
    """Return the current ``{KEY: value}`` mapping from ``.env`` (or {})."""
    out: Dict[str, str] = {}
    for line in read_env_lines():
        if line.kind == "kv":
            out[line.key] = line.value
    return out


# ---------------------------------------------------------------------------
# Writing (atomic, comment-preserving)
# ---------------------------------------------------------------------------


def _quote_if_needed(value: str) -> str:
    if value == "":
        return ""
    if any(c in value for c in " \t#'\"") or value != value.strip():
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        return f'"{escaped}"'
    return value


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically, rotating a ``.bak`` copy.

    Mirrors the discipline in :func:`clk_harness.config.save_json`:
    write to a temp file, fsync, back up the prior file, then
    ``os.replace`` into place.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    if path.exists():
        try:
            bak = path.with_suffix(path.suffix + ".bak")
            os.replace(path, bak)
        except Exception as exc:  # noqa: BLE001
            print(f"[env_file._atomic_write] backup failed for {path}: {exc}", file=sys.stderr)
    os.replace(tmp, path)


def write_env(updates: Dict[str, Optional[str]], *, removals: Optional[set] = None) -> Dict[str, str]:
    """Apply ``updates`` to ``.env`` and persist, preserving structure.

    Semantics of each ``updates`` value:
      * ``MASK_SENTINEL`` — leave the existing stored value unchanged
        (used so the UI can submit masked secrets it never received).
      * ``None`` — blank the value (key kept, value emptied).
      * any other string — set/replace the value.

    Keys in ``removals`` are dropped entirely. Existing keys are updated
    in place; brand-new keys are appended under a clearly marked block.
    Returns the resulting ``{KEY: value}`` mapping.
    """
    removals = removals or set()
    lines = read_env_lines()
    seen: set = set()
    out_lines: List[EnvLine] = []

    for line in lines:
        if line.kind != "kv":
            out_lines.append(line)
            continue
        key = line.key
        if key in removals:
            continue  # drop
        seen.add(key)
        if key in updates:
            new_val = updates[key]
            if new_val == MASK_SENTINEL:
                out_lines.append(line)  # unchanged
            elif new_val is None:
                out_lines.append(EnvLine("kv", key=key, value=""))
            else:
                out_lines.append(EnvLine("kv", key=key, value=str(new_val)))
        else:
            out_lines.append(line)

    # Append genuinely new keys (skip sentinel/None placeholders).
    appended: List[EnvLine] = []
    for key, val in updates.items():
        if key in seen or key in removals:
            continue
        if val is None or val == MASK_SENTINEL:
            continue
        appended.append(EnvLine("kv", key=key, value=str(val)))

    if appended:
        if out_lines and out_lines[-1].kind != "blank":
            out_lines.append(EnvLine("blank"))
        out_lines.append(EnvLine("comment", raw="# Added by CLK web UI"))
        out_lines.extend(appended)

    rendered: List[str] = []
    for line in out_lines:
        if line.kind == "kv":
            rendered.append(f"{line.key}={_quote_if_needed(line.value)}")
        elif line.kind == "comment":
            rendered.append(line.raw)
        else:
            rendered.append("")
    text = "\n".join(rendered)
    if text and not text.endswith("\n"):
        text += "\n"
    _atomic_write(env_path(), text)
    return read_env()


# ---------------------------------------------------------------------------
# Schema for the settings form
# ---------------------------------------------------------------------------


@dataclass
class EnvVar:
    key: str
    group: str
    label: str
    type: str = "string"  # bool | int | float | enum | secret | string
    default: str = ""
    choices: List[str] = field(default_factory=list)
    help: str = ""

    @property
    def is_secret(self) -> bool:
        return self.type == "secret" or is_secret_key(self.key)


_G_CORE = "Core"
_G_AUTH = "Authentication"
_G_KEYS = "API Keys"
_G_GIT = "Git identity"
_G_HTTP = "HTTP providers"
_G_TELEGRAM = "Telegram bot"
_G_ROBUST = "Robustness loops"
_G_RETRY = "Retries & timeouts"
_G_EXTRA = "Other"

# Ordered so the UI renders groups in a sensible top-to-bottom flow.
GROUP_ORDER = [
    _G_CORE, _G_AUTH, _G_KEYS, _G_HTTP, _G_GIT,
    _G_ROBUST, _G_RETRY, _G_TELEGRAM, _G_EXTRA,
]

ENV_SCHEMA: List[EnvVar] = [
    # Core
    EnvVar("CLK_PROVIDER", _G_CORE, "Active provider", "enum", "shell",
           ["shell", "claude", "codex", "gemini", "pi", "ollama", "openwebui"],
           "Which agent backend to use."),
    EnvVar("CLK_MAX_ITERATIONS", _G_CORE, "Max iterations", "int", "10",
           help="Iteration budget for loops."),
    EnvVar("CLK_PROJECT_NAME", _G_CORE, "Project name", "string", "clk-app",
           help="Embedded in commits and shown via `clk status`."),
    EnvVar("CLK_RUN_INSTALL", _G_CORE, "Run install_local.sh", "bool", "false",
           help="Create .clk/venv and install PyYAML on kickoff."),
    EnvVar("CLK_NO_TUI", _G_CORE, "Skip TUI (legacy pipeline)", "bool", "false",
           help="Run the non-interactive init/idea/plan/run/loop pipeline."),
    # Auth
    EnvVar("CLK_AUTH_MODE", _G_AUTH, "Auth mode", "enum", "cli", ["cli", "apikey"],
           "cli = trust the provider CLI login; apikey = use the env-var key below."),
    # API keys
    EnvVar("ANTHROPIC_API_KEY", _G_KEYS, "Anthropic API key", "secret",
           help="Used when provider=claude and auth mode=apikey."),
    EnvVar("OPENAI_API_KEY", _G_KEYS, "OpenAI API key", "secret",
           help="Used when provider=codex and auth mode=apikey."),
    EnvVar("GEMINI_API_KEY", _G_KEYS, "Gemini API key", "secret",
           help="Used when provider=gemini and auth mode=apikey."),
    EnvVar("GOOGLE_API_KEY", _G_KEYS, "Google API key", "secret",
           help="Alternative to GEMINI_API_KEY for the gemini provider."),
    # HTTP providers
    EnvVar("CLK_OLLAMA_ENDPOINT", _G_HTTP, "Ollama endpoint", "string", "http://localhost:11434"),
    EnvVar("CLK_OLLAMA_MODEL", _G_HTTP, "Ollama model", "string", "llama3.1"),
    EnvVar("CLK_OPENWEBUI_ENDPOINT", _G_HTTP, "OpenWebUI endpoint", "string", "http://localhost:8080"),
    EnvVar("CLK_OPENWEBUI_API_KEY", _G_HTTP, "OpenWebUI API key", "secret"),
    EnvVar("CLK_OPENWEBUI_MODEL", _G_HTTP, "OpenWebUI model", "string"),
    EnvVar("CLK_PI_MODEL", _G_HTTP, "Pi model", "string",
           help="Passed as `pi --model <value>` (e.g. openrouter/free)."),
    EnvVar("CLK_PI_KEY_TYPE", _G_HTTP, "Pi key type", "string", "openrouter",
           help="Provider your key belongs to; derives {TYPE}_API_KEY."),
    EnvVar("CLK_PI_API_KEY", _G_HTTP, "Pi API key", "secret"),
    # Git
    EnvVar("CLK_GIT_NAME", _G_GIT, "Git author name", "string"),
    EnvVar("CLK_GIT_EMAIL", _G_GIT, "Git author email", "string"),
    # Robustness
    EnvVar("CLK_ROBUSTNESS_AUTO_CONSENSUS", _G_ROBUST, "Auto-consensus", "enum", "on_careful",
           ["off", "on_careful", "always"], "Fan-out careful dispatches into N stochastic samples."),
    EnvVar("CLK_ROBUSTNESS_AUTO_REFINE", _G_ROBUST, "Auto-refine", "enum", "careful_only",
           ["off", "careful_only", "all"], "Critic-judge draft -> critic -> revise inner loop."),
    EnvVar("CLK_ROBUSTNESS_MAX_QUALITY_RETRIES", _G_ROBUST, "Max quality retries", "int", "2"),
    EnvVar("CLK_ROBUSTNESS_MIN_RESPONSE_CHARS", _G_ROBUST, "Min response chars", "int", "40"),
    EnvVar("CLK_ROBUSTNESS_REFINE_MAX_ROUNDS", _G_ROBUST, "Refine max rounds", "int", "3"),
    EnvVar("CLK_ROBUSTNESS_REFINE_ACCEPT_THRESHOLD", _G_ROBUST, "Refine accept threshold", "float", "0.8"),
    EnvVar("CLK_ROBUSTNESS_QA_PARALLEL_JUDGES", _G_ROBUST, "Parallel Q&A judges", "int", "1"),
    EnvVar("CLK_ROBUSTNESS_MAX_QA_DEPTH", _G_ROBUST, "Max Q&A depth", "int", "3"),
    EnvVar("CLK_ROBUSTNESS_PLATEAU_WINDOW", _G_ROBUST, "Plateau window", "int", "3"),
    EnvVar("CLK_ROBUSTNESS_PLATEAU_ACTION", _G_ROBUST, "Plateau action", "enum", "escalate_then_reframe",
           ["off", "escalate_only", "reframe_only", "escalate_then_reframe"]),
    EnvVar("CLK_ROBUSTNESS_DEBATE", _G_ROBUST, "Debate panel", "enum", "careful_only",
           ["off", "careful_only", "all"],
           "Adversarial panel: one critic per lens instead of a single critic."),
    EnvVar("CLK_ROBUSTNESS_DEBATE_LENSES", _G_ROBUST, "Debate lenses", "string",
           "correctness,security,simplicity",
           help="Comma-separated; one parallel critic per lens."),
    EnvVar("CLK_ROBUSTNESS_DEBATE_MAX_ROUNDS", _G_ROBUST, "Debate max rounds", "int", "2"),
    # Gauntlet loop (layer 12)
    EnvVar("GAUNTLET_LOOP", _G_ROBUST, "Gauntlet loop", "bool", "true",
           help="Acceptance criteria first, then critique / revise / verify, "
                "on every agent and sub-agent. Off restores the previous "
                "dispatch path exactly."),
    EnvVar("CLK_GAUNTLET_PRESET", _G_ROBUST, "Gauntlet preset", "enum", "standard",
           ["quick", "standard", "rigorous"],
           "Critique-round cap: quick=1, standard=3, rigorous=5."),
    EnvVar("CLK_GAUNTLET_MAX_ROUNDS", _G_ROBUST, "Gauntlet max rounds", "int", "0",
           help="Critique rounds per dispatch. 0 = derive from the preset (default 3)."),
    EnvVar("CLK_GAUNTLET_MAX_DISPATCHES", _G_ROBUST, "Gauntlet dispatch budget",
           "int", "500",
           help="Total gauntlet dispatches for the whole session. 0 = unlimited."),
    EnvVar("CLK_GAUNTLET_SCOPE", _G_ROBUST, "Gauntlet scope", "enum", "all",
           ["all", "careful_only", "producing_only"],
           "Which dispatches the gauntlet wraps."),
    EnvVar("CLK_GAUNTLET_ANSWER_KEY", _G_ROBUST, "Gauntlet answer key", "bool", "true",
           help="Derive acceptance criteria before judging the work."),
    EnvVar("CLK_GAUNTLET_FINAL_VERIFICATION", _G_ROBUST, "Gauntlet final verification",
           "bool", "true",
           help="Run a closing verification pass against the answer key."),
    EnvVar("CLK_GAUNTLET_ACCEPT_THRESHOLD", _G_ROBUST, "Gauntlet accept threshold",
           "float", "0.8"),
    EnvVar("CLK_GAUNTLET_SUPERSEDE_AUTO_REFINE", _G_ROBUST,
           "Gauntlet replaces auto-refine", "bool", "true",
           help="Retire the auto_refine critic pass while the gauntlet runs, "
                "so work is not critiqued twice. Explicit refine: blocks in "
                "workflow YAML still run."),
    EnvVar("CLK_GAUNTLET_FOCUS", _G_ROBUST, "Gauntlet focus lenses", "string", "",
           help="Comma-separated extra critique lenses."),
    # Retries & timeouts
    EnvVar("CLK_PROVIDER_TIMEOUT_S", _G_RETRY, "Provider timeout (s)", "int", "0",
           help="0 = harness default (300s)."),
    EnvVar("CLK_PROVIDER_NO_OUTPUT_TIMEOUT_S", _G_RETRY, "No-output watchdog (s)", "int", "0"),
    EnvVar("CLK_PROVIDER_RETRY_MAX_RETRIES", _G_RETRY, "Provider retry max", "int", "10"),
    EnvVar("CLK_PROVIDER_RETRY_BACKOFF_S", _G_RETRY, "Provider retry backoff (s)", "int", "5"),
    EnvVar("CLK_PROVIDER_RETRY_STAGE_MAX_RETRIES", _G_RETRY, "Stage retry max", "int", "10"),
    EnvVar("CLK_PROVIDER_RETRY_STAGE_BACKOFF_S", _G_RETRY, "Stage retry backoff (s)", "int", "30"),
    EnvVar("CLK_SUPERVISE_MAX_CYCLES", _G_RETRY, "Supervise max cycles", "int", "20"),
    EnvVar("CLK_CONSENSUS_MAX_SAMPLES", _G_RETRY, "Consensus max samples", "int", "6"),
    EnvVar("CLK_CONSENSUS_MAX_PARALLEL", _G_RETRY, "Consensus max parallel", "int", "4"),
    EnvVar("CLK_CASTING_MAX_DYNAMIC_ROLES", _G_RETRY, "Max dynamic roles", "int", "12"),
    EnvVar("CLK_AUTO_COMMIT", _G_RETRY, "Auto-commit", "bool", "true"),
    EnvVar("CLK_VALIDATION_MAX_FILES_PER_BATCH", _G_RETRY, "Max files per batch", "int", "25"),
    EnvVar("CLK_VALIDATION_WARN_FILES_PER_BATCH", _G_RETRY, "Warn files per batch", "int", "5"),
    EnvVar("CLK_META_PROMPT_DISPATCH", _G_RETRY, "Meta-prompt dispatch", "enum", "careful_only",
           ["off", "careful_only", "always"]),
    EnvVar("CLK_META_PROMPT_ROLE", _G_RETRY, "Meta-prompt role", "enum", "careful_only",
           ["off", "careful_only", "always"]),
    EnvVar("CLK_REVIEW_PER_STAGE", _G_RETRY, "Per-stage checkpoint", "bool", "false"),
    EnvVar("CLK_RECOVERY_MAX_PER_STAGE", _G_RETRY, "Recovery passes per stage", "int", "3"),
    # Telegram
    EnvVar("CLK_TELEGRAM_BOT_TOKEN", _G_TELEGRAM, "Telegram bot token", "secret"),
    EnvVar("CLK_TELEGRAM_ALLOWED_USERS", _G_TELEGRAM, "Allowed user IDs", "string",
           help="Comma-separated numeric Telegram user IDs (empty = nobody)."),
    EnvVar("CLK_TELEGRAM_ENABLED", _G_TELEGRAM, "Telegram enabled", "bool", "false"),
    EnvVar("CLK_TELEGRAM_SKIP", _G_TELEGRAM, "Skip Telegram setup", "bool", "false"),
    EnvVar("CLK_TELEGRAM_WORKSPACE", _G_TELEGRAM, "Default workspace", "string"),
]

_SCHEMA_BY_KEY: Dict[str, EnvVar] = {v.key: v for v in ENV_SCHEMA}


def schema_for(key: str) -> EnvVar:
    """Return the schema entry for ``key`` (synthesizing one for unknown
    keys so user-added vars still render)."""
    existing = _SCHEMA_BY_KEY.get(key)
    if existing is not None:
        return existing
    return EnvVar(
        key=key,
        group=_G_EXTRA,
        label=key,
        type="secret" if is_secret_key(key) else "string",
    )


def describe_env(reveal: bool = False) -> Tuple[List[dict], List[str]]:
    """Return ``(vars, group_order)`` describing the current ``.env``.

    Each var dict carries schema metadata plus the current value, masked
    when secret (unless ``reveal`` is True). Keys present in ``.env`` but
    absent from the schema appear in the "Other" group.
    """
    current = read_env()
    keys: List[str] = []
    for v in ENV_SCHEMA:
        keys.append(v.key)
    for k in current:
        if k not in _SCHEMA_BY_KEY:
            keys.append(k)

    out: List[dict] = []
    for key in keys:
        meta = schema_for(key)
        raw = current.get(key, "")
        secret = meta.is_secret
        value = (mask_value(raw) if secret and not reveal else raw)
        out.append({
            "key": key,
            "group": meta.group,
            "label": meta.label,
            "type": meta.type,
            "choices": list(meta.choices),
            "default": meta.default,
            "help": meta.help,
            "is_secret": secret,
            "masked": bool(secret and not reveal and raw),
            "set": bool(raw),
            "value": value,
        })

    groups = [g for g in GROUP_ORDER if any(v["group"] == g for v in out)]
    return out, groups


__all__ = [
    "MASK_SENTINEL",
    "EnvVar",
    "EnvLine",
    "ENV_SCHEMA",
    "GROUP_ORDER",
    "is_secret_key",
    "mask_value",
    "env_path",
    "parse_env",
    "read_env",
    "read_env_lines",
    "write_env",
    "schema_for",
    "describe_env",
]
