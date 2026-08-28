"""Gauntlet loop — Layer 12 of the robustness loops.

Every other critique layer in CLK judges a response against a critic's
in-the-moment opinion, so "good" gets invented after the work is already
done. The gauntlet inverts that order: it establishes *checkable
acceptance criteria first*, then generates, attacks, revises, and finally
verifies the result against those same criteria.

The loop wraps every non-meta dispatch in
:class:`~clk_harness.orchestration.agent.AgentRunner`:

1. **Answer key** — the worker's own ``ANSWER_KEY:`` block is used when
   present (free, prompt-level); otherwise one ``phase: gauntlet_key``
   dispatch derives it.
2. **Candidate 0** — the existing dispatch path, untouched. Auto-consensus
   and the quality-retry loop still run underneath.
3. **Adversarial critique** — ``phase: gauntlet_critique``, judged against
   the key, each finding classified material / non-material.
4. **Revise + iterate** — ``phase: gauntlet_revise``, until no material
   defect remains or the preset round cap is hit.
5. **Final verification** — ``phase: gauntlet_verify`` against the original
   objective plus every key check, with one bounded final repair.

Presets cap the critique/revision rounds: ``quick`` = 1, ``standard`` = 3
(the default), ``rigorous`` = 5.

Everything here is provider-agnostic and free of side effects apart from
:func:`~clk_harness.utils.activity_log.log_event`, so the parsing and
settings logic is unit-testable without a provider — the same shape as
``response_quality.py``.

Kill switches, highest precedence first: ``--no-gauntlet``,
``GAUNTLET_LOOP=False``, ``CLK_ROBUSTNESS_GAUNTLET=off``,
``clk.config.json::gauntlet.enabled: false``.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Dict, List, Mapping, Optional, Sequence, Tuple

from ..utils.activity_log import log_event
from ..utils.logging_utils import get_logger

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..config import Paths
    from .agent.transcript import AgentRun

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Presets
# ---------------------------------------------------------------------------

#: Round caps per preset, straight from the gauntlet-loop discipline.
PRESET_ROUNDS: Dict[str, int] = {
    "quick": 1,
    "standard": 3,
    "rigorous": 5,
}

DEFAULT_PRESET = "standard"

#: Critique lenses applied on top of the answer key. ``rigorous`` adds the
#: back half; ``quick`` uses only the first two.
PRESET_LENSES: Dict[str, Tuple[str, ...]] = {
    "quick": ("requirements", "correctness"),
    "standard": (
        "requirements",
        "correctness",
        "reasoning",
        "hidden assumptions",
        "edge cases",
        "feasibility",
    ),
    "rigorous": (
        "requirements",
        "factual correctness",
        "reasoning validity",
        "hidden assumptions",
        "counterexamples",
        "edge cases",
        "internal consistency",
        "evidence quality",
        "implementation feasibility",
        "user-impact risk",
    ),
}


# ---------------------------------------------------------------------------
# Boolean parsing
# ---------------------------------------------------------------------------

_TRUE_TOKENS = frozenset({"1", "true", "yes", "y", "on", "enabled"})
_FALSE_TOKENS = frozenset({"0", "false", "no", "n", "off", "disabled"})


def parse_bool(value: Any, default: bool) -> bool:
    """Strict tri-state boolean parse.

    Deliberately *not* ``kickoff._bool``: that helper maps every
    unrecognized string to ``False``, so a typo in ``GAUNTLET_LOOP`` would
    silently switch the loop off. Here an unrecognized value keeps
    ``default`` and is logged, so the failure is loud rather than silent.
    """
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    token = str(value).strip().lower()
    if not token:
        return default
    if token in _TRUE_TOKENS:
        return True
    if token in _FALSE_TOKENS:
        return False
    logger.warning(
        "gauntlet: unrecognized boolean %r — keeping default %s "
        "(use one of %s / %s)",
        value,
        default,
        ",".join(sorted(_TRUE_TOKENS)),
        ",".join(sorted(_FALSE_TOKENS)),
    )
    return default


def _as_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _as_csv(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [item.strip() for item in str(value).split(",") if item.strip()]


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------


@dataclass
class GauntletSettings:
    """Resolved gauntlet configuration for one dispatch."""

    enabled: bool = True
    preset: str = DEFAULT_PRESET
    max_rounds: int = 0
    #: Total gauntlet dispatches allowed per session. 0 = unlimited.
    max_dispatches: int = 500
    scope: str = "all"
    exclude_agents: List[str] = field(default_factory=lambda: ["critic"])
    critic: str = "critic"
    answer_key: bool = True
    final_verification: bool = True
    accept_threshold: float = 0.8
    supersede_auto_refine: bool = True
    focus: List[str] = field(default_factory=list)

    @property
    def rounds(self) -> int:
        """Effective round cap: explicit ``max_rounds`` wins over the preset."""
        if self.max_rounds and self.max_rounds > 0:
            return self.max_rounds
        return PRESET_ROUNDS.get(self.preset, PRESET_ROUNDS[DEFAULT_PRESET])

    @property
    def lenses(self) -> List[str]:
        """Critique lenses for this preset plus any configured ``focus``."""
        base = list(PRESET_LENSES.get(self.preset, PRESET_LENSES[DEFAULT_PRESET]))
        for extra in self.focus:
            if extra not in base:
                base.append(extra)
        return base


#: Defaults live in ``config.DEFAULT_CLK_CONFIG["gauntlet"]``; this mirror
#: keeps :func:`resolve_settings` usable with an empty config (tests, and
#: any caller that has not loaded a project config yet).
_DEFAULTS = GauntletSettings()


def resolve_settings(
    clk_cfg: Optional[Mapping[str, Any]] = None,
    env: Optional[Mapping[str, str]] = None,
    cli_override: Optional[Mapping[str, Any]] = None,
) -> GauntletSettings:
    """Layer config → env → CLI into one :class:`GauntletSettings`.

    Precedence, highest first:

    1. ``cli_override`` (``--gauntlet`` / ``--no-gauntlet`` / ``--gauntlet-preset``)
    2. ``GAUNTLET_LOOP`` — the documented short name, wins over the family name
    3. ``CLK_ROBUSTNESS_GAUNTLET`` / ``CLK_GAUNTLET_*``
    4. ``clk.config.json::gauntlet``
    5. built-in defaults

    Reading the environment here (rather than only through the kickoff
    env→config mapping) is what makes ``GAUNTLET_LOOP=False clk run`` work
    in a workspace that was never kicked off — the same direct-read
    precedent as ``CLK_PROVIDER`` in ``agent/runner.py``.
    """
    cfg: Mapping[str, Any] = ((clk_cfg or {}).get("gauntlet") or {})
    environ: Mapping[str, str] = os.environ if env is None else env
    cli: Mapping[str, Any] = cli_override or {}

    def _cfg(key: str, default: Any) -> Any:
        val = cfg.get(key)
        return default if val is None else val

    enabled = parse_bool(_cfg("enabled", _DEFAULTS.enabled), _DEFAULTS.enabled)
    # Family name first, then the short name, so GAUNTLET_LOOP wins.
    if environ.get("CLK_ROBUSTNESS_GAUNTLET") is not None:
        enabled = parse_bool(environ.get("CLK_ROBUSTNESS_GAUNTLET"), enabled)
    if environ.get("GAUNTLET_LOOP") is not None:
        enabled = parse_bool(environ.get("GAUNTLET_LOOP"), enabled)
    if cli.get("enabled") is not None:
        enabled = bool(cli["enabled"])

    preset = str(_cfg("preset", _DEFAULTS.preset)).strip().lower()
    preset = str(environ.get("CLK_GAUNTLET_PRESET") or preset).strip().lower()
    if cli.get("preset"):
        preset = str(cli["preset"]).strip().lower()
    if preset not in PRESET_ROUNDS:
        logger.warning(
            "gauntlet: unknown preset %r — falling back to %r (choose one of %s)",
            preset, DEFAULT_PRESET, ", ".join(sorted(PRESET_ROUNDS)),
        )
        preset = DEFAULT_PRESET

    max_rounds = _as_int(_cfg("max_rounds", _DEFAULTS.max_rounds), _DEFAULTS.max_rounds)
    max_rounds = _as_int(environ.get("CLK_GAUNTLET_MAX_ROUNDS", max_rounds), max_rounds)
    if cli.get("max_rounds") is not None:
        max_rounds = _as_int(cli["max_rounds"], max_rounds)

    max_dispatches = _as_int(
        _cfg("max_dispatches", _DEFAULTS.max_dispatches), _DEFAULTS.max_dispatches,
    )
    max_dispatches = _as_int(
        environ.get("CLK_GAUNTLET_MAX_DISPATCHES", max_dispatches), max_dispatches,
    )
    if cli.get("max_dispatches") is not None:
        max_dispatches = _as_int(cli["max_dispatches"], max_dispatches)

    scope = str(_cfg("scope", _DEFAULTS.scope)).strip().lower()
    scope = str(environ.get("CLK_GAUNTLET_SCOPE") or scope).strip().lower()
    if scope not in ("all", "careful_only", "producing_only"):
        scope = _DEFAULTS.scope

    exclude = _as_csv(_cfg("exclude_agents", list(_DEFAULTS.exclude_agents)))
    if environ.get("CLK_GAUNTLET_EXCLUDE_AGENTS"):
        exclude = _as_csv(environ.get("CLK_GAUNTLET_EXCLUDE_AGENTS"))

    critic = str(_cfg("critic", _DEFAULTS.critic)).strip() or _DEFAULTS.critic
    critic = str(environ.get("CLK_GAUNTLET_CRITIC") or critic).strip()

    answer_key = parse_bool(_cfg("answer_key", _DEFAULTS.answer_key), _DEFAULTS.answer_key)
    answer_key = parse_bool(environ.get("CLK_GAUNTLET_ANSWER_KEY"), answer_key)

    final_verification = parse_bool(
        _cfg("final_verification", _DEFAULTS.final_verification), _DEFAULTS.final_verification,
    )
    final_verification = parse_bool(
        environ.get("CLK_GAUNTLET_FINAL_VERIFICATION"), final_verification,
    )

    threshold = _as_float(
        _cfg("accept_threshold", _DEFAULTS.accept_threshold), _DEFAULTS.accept_threshold,
    )
    threshold = _as_float(environ.get("CLK_GAUNTLET_ACCEPT_THRESHOLD", threshold), threshold)

    supersede = parse_bool(
        _cfg("supersede_auto_refine", _DEFAULTS.supersede_auto_refine),
        _DEFAULTS.supersede_auto_refine,
    )
    supersede = parse_bool(environ.get("CLK_GAUNTLET_SUPERSEDE_AUTO_REFINE"), supersede)

    focus = _as_csv(_cfg("focus", list(_DEFAULTS.focus)))
    if environ.get("CLK_GAUNTLET_FOCUS"):
        focus = _as_csv(environ.get("CLK_GAUNTLET_FOCUS"))

    return GauntletSettings(
        enabled=enabled,
        preset=preset,
        max_rounds=max_rounds,
        max_dispatches=max_dispatches,
        scope=scope,
        exclude_agents=exclude,
        critic=critic,
        answer_key=answer_key,
        final_verification=final_verification,
        accept_threshold=threshold,
        supersede_auto_refine=supersede,
        focus=focus,
    )


class DispatchBudget:
    """Session-wide cap on how many dispatches the gauntlet may spend.

    The round cap bounds a *single* dispatch; this bounds the whole
    session. Without it, a long mission with hundreds of stages could
    spend an unbounded number of critique dispatches — the round cap
    alone does not stop that, because it resets on every stage.

    Not thread-safe by design: the count is advisory, and workflow stages
    run in a pool, so an exact bound would cost a lock on a hot path for
    no benefit. Overshooting by a few dispatches under contention is
    acceptable for a runaway guard.
    """

    def __init__(self, limit: int = 0) -> None:
        self.limit = max(0, int(limit))
        self.used = 0
        # Exhaustion is logged once, not on every subsequent dispatch: a long
        # run would otherwise write hundreds of identical lines and drown the
        # activity log in the one place someone is looking for the cause.
        self.reported = False

    @property
    def exhausted(self) -> bool:
        return self.limit > 0 and self.used >= self.limit

    def spend(self, n: int = 1) -> bool:
        """Charge ``n`` dispatches. Returns False when the budget is spent."""
        if self.exhausted:
            return False
        self.used += n
        return True

    def claim_report(self) -> bool:
        """True the first time exhaustion is worth logging, False after."""
        if self.reported:
            return False
        self.reported = True
        return True

    def reset(self) -> None:
        self.used = 0
        self.reported = False

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        cap = self.limit or "unlimited"
        return f"DispatchBudget(used={self.used}, limit={cap})"


def enabled_for(
    settings: GauntletSettings,
    agent_name: str,
    extra: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Whether the gauntlet should wrap this particular dispatch.

    Meta-phase and dry-run filtering happens in the caller (the runner
    already knows both); this decides the agent/scope question only.
    """
    if not settings.enabled:
        return False
    if agent_name in settings.exclude_agents:
        return False
    ctx: Mapping[str, Any] = extra or {}
    if settings.scope == "careful_only":
        return bool(ctx.get("careful"))
    if settings.scope == "producing_only":
        # A stage that declares outputs, or one that is expected to write
        # files, is "producing"; pure review/QA chatter is not.
        return bool(ctx.get("stage_outputs") or ctx.get("commit"))
    return True


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

_ANSWER_KEY_RE = re.compile(
    r"^[ \t]*ANSWER_KEY:[ \t]*\n(?P<body>.*?)^[ \t]*END_ANSWER_KEY[ \t]*$",
    re.IGNORECASE | re.MULTILINE | re.DOTALL,
)
_CHECK_LINE_RE = re.compile(
    r"^[ \t]*(?:[-*]\s*)?(?P<id>[A-Za-z][A-Za-z0-9_.-]*)\s*[:|]\s*(?P<body>.+?)\s*$",
)
_VERDICT_RE = re.compile(
    r"^\s*VERDICT\s*:\s*(accept|revise|reject)\b", re.IGNORECASE | re.MULTILINE,
)
_SCORE_RE = re.compile(
    r"^\s*SCORE\s*:\s*([0-9]*\.?[0-9]+)", re.IGNORECASE | re.MULTILINE,
)
_MATERIAL_RE = re.compile(
    r"^\s*MATERIAL_DEFECTS\s*:\s*(?P<n>\d+)\b", re.IGNORECASE | re.MULTILINE,
)


@dataclass
class AnswerKeyCheck:
    """One checkable acceptance condition."""

    id: str
    condition: str

    def render(self) -> str:
        return f"- {self.id}: {self.condition}"


def parse_answer_key(text: str) -> List[AnswerKeyCheck]:
    """Extract ``ANSWER_KEY:`` / ``END_ANSWER_KEY`` checks from a response.

    Tolerant by design: an unparseable line is skipped rather than
    failing the whole key, and a missing block simply yields ``[]`` so the
    caller can decide whether to spend a dispatch deriving one.
    """
    if not text or "ANSWER_KEY" not in text.upper():
        return []
    match = _ANSWER_KEY_RE.search(text)
    if match is None:
        return []
    checks: List[AnswerKeyCheck] = []
    seen: set = set()
    for raw in (match.group("body") or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        hit = _CHECK_LINE_RE.match(line)
        if hit is None:
            continue
        check_id = hit.group("id").strip()
        condition = hit.group("body").strip()
        if not condition or check_id.lower() in seen:
            continue
        seen.add(check_id.lower())
        checks.append(AnswerKeyCheck(id=check_id, condition=condition))
    return checks


def render_answer_key(checks: Sequence[AnswerKeyCheck]) -> str:
    """Render checks back into the block form agents emit."""
    if not checks:
        return "(no acceptance checks derived)"
    return "\n".join(check.render() for check in checks)


@dataclass
class Critique:
    """Parsed verdict from one adversarial critique dispatch."""

    verdict: str = "revise"
    score: float = 0.0
    material_defects: int = 0
    feedback: str = ""

    @property
    def converged(self) -> bool:
        """True when the critique found nothing worth another round.

        Per the discipline: "A clean critique is a valid outcome." An
        explicit ``MATERIAL_DEFECTS: 0`` converges even if the critic also
        listed cosmetic nits.
        """
        if self.verdict == "accept":
            return True
        return self.material_defects == 0 and self.verdict != "reject"


def parse_critique(text: str, accept_threshold: float = 0.8) -> Critique:
    """Parse ``VERDICT:`` / ``SCORE:`` / ``MATERIAL_DEFECTS:`` from a critic.

    Mirrors the shape ``workflow/review.py`` already uses for the
    critic-judge loop so critics only have to learn one output contract.
    A missing score defaults to 1.0 on accept and 0.4 on revise, matching
    that module's behavior.
    """
    body = text or ""
    verdict_hit = _VERDICT_RE.search(body)
    verdict = (verdict_hit.group(1).lower() if verdict_hit else "revise")

    score_hit = _SCORE_RE.search(body)
    if score_hit is not None:
        score = _as_float(score_hit.group(1), 0.0)
    else:
        score = 1.0 if verdict == "accept" else 0.4
    score = max(0.0, min(1.0, score))

    material_hit = _MATERIAL_RE.search(body)
    if material_hit is not None:
        material = _as_int(material_hit.group("n"), 0)
    else:
        # No explicit count: infer from the verdict, and let a
        # high-scoring "revise" still count as one defect so the worker
        # gets the feedback rather than the harness swallowing it.
        material = 0 if verdict == "accept" else 1

    if verdict == "accept" and score < accept_threshold:
        # An "accept" the critic scored below the bar is not an accept.
        verdict = "revise"
        material = max(material, 1)

    return Critique(
        verdict=verdict,
        score=score,
        material_defects=material,
        feedback=body.strip(),
    )


# ---------------------------------------------------------------------------
# Objective builders
# ---------------------------------------------------------------------------

_MAX_QUOTED_CHARS = 4000


def _truncate(text: str, limit: int = _MAX_QUOTED_CHARS) -> str:
    body = (text or "").strip()
    if len(body) <= limit:
        return body
    return body[:limit] + "\n\n[... truncated for the critique prompt ...]"


def build_key_objective(objective: str, settings: GauntletSettings) -> str:
    """Objective for the ``gauntlet_key`` dispatch (stages 1-3)."""
    return (
        "GAUNTLET :: acceptance answer key.\n\n"
        "Before any work is judged, write the checkable acceptance criteria "
        "for the task below. Derive them from the task itself and from the "
        "project's stated requirements — do not invent a standard of your "
        "own, and do not weaken, reinterpret, or drop any constraint the "
        "task states.\n\n"
        "Emit exactly one block, nothing else:\n\n"
        "ANSWER_KEY:\n"
        "- <check_id>: <unambiguous pass condition, objectively decidable>\n"
        "- <check_id>: <...>\n"
        "END_ANSWER_KEY\n\n"
        "Rules:\n"
        "- Prefer binary, verifiable conditions over matters of taste.\n"
        "- Weight correctness above style.\n"
        "- Cover every explicit constraint and exclusion in the task.\n"
        f"- Aim for {min(3 + settings.rounds, 10)} checks or fewer; each must earn its place.\n"
        "- If a decision is genuinely unresolved and would materially change "
        "the result, add a check that names it rather than assuming an answer.\n\n"
        f"TASK:\n{objective}"
    )


def build_critique_objective(
    objective: str,
    candidate_text: str,
    checks: Sequence[AnswerKeyCheck],
    settings: GauntletSettings,
    round_idx: int,
) -> str:
    """Objective for a ``gauntlet_critique`` dispatch (stage 6)."""
    lenses = ", ".join(settings.lenses)
    return (
        f"GAUNTLET :: adversarial critique, round {round_idx}/{settings.rounds}.\n\n"
        "Attack this work. Do not defend it, do not summarize it, and do not "
        "praise it. Your job is to find what is wrong before anyone else "
        "does.\n\n"
        "Judge it against the acceptance answer key below — that key, plus "
        "the original task, is the source of truth. Do not substitute your "
        "own notion of quality for it, and do not move the goalposts to make "
        "a weak candidate pass.\n\n"
        f"ACCEPTANCE ANSWER KEY:\n{render_answer_key(checks)}\n\n"
        f"ORIGINAL TASK:\n{_truncate(objective)}\n\n"
        f"CANDIDATE:\n{_truncate(candidate_text)}\n\n"
        f"Look through these lenses: {lenses}.\n\n"
        "Check specifically for: failed or omitted acceptance checks; "
        "unsupported claims; factual errors and invalid reasoning; hidden or "
        "conflicting assumptions; ambiguity and contradictions; "
        "counterexamples and edge cases; implementation gaps; unnecessary "
        "complexity; integration failures between parts that each look fine "
        "alone; and overconfidence or false claims of verification.\n\n"
        "Classify every issue as material or non-material. A **material "
        "defect** could change correctness, usefulness, compliance, "
        "interpretation, feasibility, safety, or a reader's decision. Minor "
        "style preferences, synonym choices, and cosmetic reordering are "
        "**not** material. Finding nothing material is a valid outcome — say "
        "so rather than inventing a complaint.\n\n"
        "For each material defect, name the answer-key check it breaks.\n\n"
        "End your response with exactly these three lines:\n"
        "MATERIAL_DEFECTS: <integer>\n"
        "VERDICT: accept   # or: revise\n"
        "SCORE: <0..1>"
    )


def build_revise_objective(
    objective: str,
    critique: Critique,
    checks: Sequence[AnswerKeyCheck],
    settings: GauntletSettings,
    round_idx: int,
) -> str:
    """Objective for a ``gauntlet_revise`` dispatch (stages 7-8)."""
    return (
        f"GAUNTLET :: revision, round {round_idx}/{settings.rounds}.\n\n"
        f"An adversarial critic scored your previous response "
        f"{critique.score:.2f}/1.0 and found {critique.material_defects} "
        "material defect(s):\n\n"
        f"{_truncate(critique.feedback)}\n\n"
        "Fix every material defect. Integrate the corrections into the work "
        "itself — do not append caveats, disclaimers, or a changelog "
        "explaining what you fixed. Keep what already works; rewrite only "
        "what was flagged. Do not weaken the task's requirements to make the "
        "critique go away.\n\n"
        "Re-check the ORIGINAL TASK below, not just the critique — a "
        "revision that satisfies the critic while drifting from the original "
        "request has failed.\n\n"
        "Re-emit your POST and ACTION blocks the same way you did the first "
        "time so the harness records the updated work.\n\n"
        f"ACCEPTANCE ANSWER KEY:\n{render_answer_key(checks)}\n\n"
        f"ORIGINAL TASK:\n{objective}"
    )


def build_verify_objective(
    objective: str,
    candidate_text: str,
    checks: Sequence[AnswerKeyCheck],
    settings: GauntletSettings,
) -> str:
    """Objective for the ``gauntlet_verify`` dispatch (stage 9)."""
    return (
        "GAUNTLET :: final verification.\n\n"
        "This is the last gate before the work is accepted. Re-evaluate the "
        "candidate against all of the following:\n\n"
        "1. The original request, in full.\n"
        "2. Every explicit constraint and exclusion it states.\n"
        "3. Every check in the acceptance answer key.\n"
        "4. Integration and regression: do the parts work together, and did "
        "the revisions break anything that previously worked?\n"
        "5. The evidence actually available to you.\n\n"
        "Prefer deterministic evidence over opinion: run or read tests, "
        "inspect the artifacts on disk, check the numbers. Do **not** claim "
        "you tested, researched, or externally verified something you did "
        "not — an honest 'unverified' is worth more than a false 'verified'.\n\n"
        f"ACCEPTANCE ANSWER KEY:\n{render_answer_key(checks)}\n\n"
        f"ORIGINAL TASK:\n{_truncate(objective)}\n\n"
        f"CANDIDATE:\n{_truncate(candidate_text)}\n\n"
        "End your response with exactly these three lines:\n"
        "MATERIAL_DEFECTS: <integer>\n"
        "VERDICT: accept   # or: revise\n"
        "SCORE: <0..1>"
    )


def build_repair_objective(
    objective: str,
    critique: Critique,
    checks: Sequence[AnswerKeyCheck],
) -> str:
    """Objective for the single bounded repair allowed after verification."""
    return (
        "GAUNTLET :: final repair (one pass only).\n\n"
        "Final verification found a remaining material defect. This is the "
        "only repair round left — fix the defect and nothing else. Do not "
        "start new work, do not refactor beyond the fix, and do not widen "
        "the scope.\n\n"
        f"{_truncate(critique.feedback)}\n\n"
        f"ACCEPTANCE ANSWER KEY:\n{render_answer_key(checks)}\n\n"
        f"ORIGINAL TASK:\n{objective}"
    )


# ---------------------------------------------------------------------------
# The loop
# ---------------------------------------------------------------------------

#: Phases the gauntlet dispatches under. All of these must be in
#: ``AgentRunner._META_PHASES`` or the loop recurses into itself.
PHASE_KEY = "gauntlet_key"
PHASE_CRITIQUE = "gauntlet_critique"
PHASE_REVISE = "gauntlet_revise"
PHASE_VERIFY = "gauntlet_verify"
PHASE_REPAIR = "gauntlet_repair"

GAUNTLET_PHASES: Tuple[str, ...] = (
    PHASE_KEY,
    PHASE_CRITIQUE,
    PHASE_REVISE,
    PHASE_VERIFY,
    PHASE_REPAIR,
)


class GauntletLoop:
    """Runs the gauntlet over one candidate response.

    The loop never raises: any failure inside it (missing critic, provider
    error, unparseable critique) falls back to returning the best candidate
    it has, so the gauntlet can only improve a dispatch or leave it alone —
    never lose it.
    """

    def __init__(
        self,
        runner: Any,
        settings: GauntletSettings,
        paths: Optional["Paths"] = None,
        budget: Optional[DispatchBudget] = None,
    ) -> None:
        self.runner = runner
        self.settings = settings
        self.paths = paths if paths is not None else getattr(runner, "paths", None)
        # Shared across the session when the runner supplies one, so the
        # cap bounds the whole run rather than each dispatch separately.
        self.budget = budget if budget is not None else DispatchBudget(settings.max_dispatches)

    # -- helpers ---------------------------------------------------------

    def _log(self, event: str, **fields: Any) -> None:
        if self.paths is None:
            return
        try:
            log_event(self.paths, event, **fields)
        except Exception as exc:  # pragma: no cover - logging must never break a run
            logger.debug("gauntlet log_event failed: %s", exc)

    def _observe(self, line: str) -> None:
        try:
            self.runner._observer_log(line)
        except Exception as exc:  # pragma: no cover
            logger.debug("gauntlet observer log failed: %s", exc)

    def _critic_name(self, agent_name: str) -> str:
        """Resolve the critic, falling back to the worker's own agent.

        A dynamic roster may not have cast a ``critic``. Rather than skip
        the gauntlet entirely, fall back to a self-critique dispatch: a
        fresh audit in the same role is weaker than an independent critic,
        but it still catches omissions, and the discipline is explicit that
        this must not be *called* independent verification.
        """
        agents_cfg = (getattr(self.runner, "agents_cfg", {}) or {}).get("agents") or {}
        wanted = self.settings.critic
        if wanted and wanted in agents_cfg:
            return wanted
        if "critic" in agents_cfg:
            return "critic"
        if "qa" in agents_cfg:
            return "qa"
        return agent_name

    def _dispatch(
        self,
        agent_name: str,
        objective: str,
        phase: str,
        base_extra: Mapping[str, Any],
        dry_run: Optional[bool],
    ) -> Optional["AgentRun"]:
        extra = {
            k: v for k, v in (base_extra or {}).items()
            if k not in ("phase", "gauntlet_ran")
        }
        extra["phase"] = phase
        if not self.budget.spend():
            # Session budget spent: stop wrapping rather than keep spending.
            # The caller falls back to the best candidate it already has.
            if self.budget.claim_report():
                self._log(
                    "gauntlet_budget_exhausted",
                    phase=phase,
                    agent=agent_name,
                    limit=self.budget.limit,
                )
            return None
        try:
            return self.runner.run(agent_name, objective, extra=extra, dry_run=dry_run)
        except Exception as exc:
            logger.warning("gauntlet %s dispatch failed: %s", phase, exc)
            self._log("gauntlet_dispatch_failed", phase=phase, agent=agent_name, error=str(exc))
            return None

    @staticmethod
    def _text_of(run: Optional["AgentRun"]) -> str:
        if run is None:
            return ""
        response = getattr(run, "response", None)
        return getattr(response, "text", "") or ""

    @staticmethod
    def _ok(run: Optional["AgentRun"]) -> bool:
        if run is None:
            return False
        response = getattr(run, "response", None)
        return bool(getattr(response, "ok", False))

    # -- stages ----------------------------------------------------------

    def _build_answer_key(
        self,
        agent_name: str,
        objective: str,
        candidate: "AgentRun",
        base_extra: Mapping[str, Any],
        dry_run: Optional[bool],
    ) -> List[AnswerKeyCheck]:
        """Stages 1-3. Prefer the worker's own key; derive one only if needed."""
        checks = parse_answer_key(self._text_of(candidate))
        if checks:
            self._log(
                "gauntlet_key",
                agent=agent_name,
                source="worker",
                checks=len(checks),
                ids=[c.id for c in checks],
            )
            return checks
        if not self.settings.answer_key:
            return []
        key_run = self._dispatch(
            self._critic_name(agent_name),
            build_key_objective(objective, self.settings),
            PHASE_KEY,
            base_extra,
            dry_run,
        )
        checks = parse_answer_key(self._text_of(key_run))
        self._log(
            "gauntlet_key",
            agent=agent_name,
            source="derived",
            checks=len(checks),
            ids=[c.id for c in checks],
        )
        return checks

    # -- entry point -----------------------------------------------------

    def run(
        self,
        agent_name: str,
        objective: str,
        candidate: "AgentRun",
        *,
        extra: Optional[Mapping[str, Any]] = None,
        dry_run: Optional[bool] = None,
    ) -> "AgentRun":
        """Put ``candidate`` through the gauntlet and return the best run."""
        base_extra: Mapping[str, Any] = extra or {}
        rounds = self.settings.rounds

        # A candidate that already failed, or came back empty, has nothing
        # to critique — the quality-retry layer beneath us already spent its
        # budget on it, and critiquing an empty string just burns tokens to
        # rediscover that it is empty.
        if not self._ok(candidate) or not self._text_of(candidate).strip():
            return candidate

        if self.budget.exhausted:
            if self.budget.claim_report():
                self._log(
                    "gauntlet_budget_exhausted",
                    agent=agent_name,
                    limit=self.budget.limit,
                    phase="entry",
                )
            return candidate

        self._log(
            "gauntlet_started",
            agent=agent_name,
            preset=self.settings.preset,
            rounds=rounds,
            scope=self.settings.scope,
            budget_used=self.budget.used,
            budget_limit=self.budget.limit,
        )

        checks = self._build_answer_key(agent_name, objective, candidate, base_extra, dry_run)
        critic = self._critic_name(agent_name)
        current = candidate
        last_critique: Optional[Critique] = None

        for round_idx in range(1, rounds + 1):
            critique_run = self._dispatch(
                critic,
                build_critique_objective(
                    objective, self._text_of(current), checks, self.settings, round_idx,
                ),
                PHASE_CRITIQUE,
                base_extra,
                dry_run,
            )
            if not self._ok(critique_run):
                # No usable critique — stop rather than revise blindly.
                self._log("gauntlet_critique_unavailable", agent=agent_name, round=round_idx)
                break

            critique = parse_critique(self._text_of(critique_run), self.settings.accept_threshold)
            last_critique = critique
            self._log(
                "gauntlet_critique",
                agent=agent_name,
                critic=critic,
                round=round_idx,
                max_rounds=rounds,
                verdict=critique.verdict,
                score=critique.score,
                material_defects=critique.material_defects,
            )
            self._observe(
                f"gauntlet :: {agent_name} :: round {round_idx}/{rounds} "
                f"{critic}→ {critique.verdict} score={critique.score:.2f} "
                f"material={critique.material_defects}"
            )

            if critique.converged:
                self._log("gauntlet_converged", agent=agent_name, round=round_idx)
                break

            revised = self._dispatch(
                agent_name,
                build_revise_objective(objective, critique, checks, self.settings, round_idx),
                PHASE_REVISE,
                base_extra,
                dry_run,
            )
            if revised is None or not self._ok(revised):
                # Keep the last good candidate rather than a failed revision.
                self._log("gauntlet_revision_failed", agent=agent_name, round=round_idx)
                break
            current = revised
        else:
            self._log("gauntlet_round_cap", agent=agent_name, rounds=rounds)

        current = self._final_verification(
            agent_name, objective, current, checks, base_extra, dry_run,
        )

        self._log(
            "gauntlet_final",
            agent=agent_name,
            checks=len(checks),
            last_verdict=(last_critique.verdict if last_critique else "none"),
        )
        return current

    def _final_verification(
        self,
        agent_name: str,
        objective: str,
        current: "AgentRun",
        checks: Sequence[AnswerKeyCheck],
        base_extra: Mapping[str, Any],
        dry_run: Optional[bool],
    ) -> "AgentRun":
        """Stage 9, plus the one bounded repair the discipline allows."""
        if not self.settings.final_verification:
            return current

        verify_run = self._dispatch(
            self._critic_name(agent_name),
            build_verify_objective(objective, self._text_of(current), checks, self.settings),
            PHASE_VERIFY,
            base_extra,
            dry_run,
        )
        if not self._ok(verify_run):
            self._log("gauntlet_verify_unavailable", agent=agent_name)
            return current

        verdict = parse_critique(self._text_of(verify_run), self.settings.accept_threshold)
        self._log(
            "gauntlet_verify",
            agent=agent_name,
            verdict=verdict.verdict,
            score=verdict.score,
            material_defects=verdict.material_defects,
        )
        if verdict.converged:
            return current

        repaired = self._dispatch(
            agent_name,
            build_repair_objective(objective, verdict, checks),
            PHASE_REPAIR,
            base_extra,
            dry_run,
        )
        if repaired is not None and self._ok(repaired):
            self._log("gauntlet_repaired", agent=agent_name)
            return repaired
        self._log("gauntlet_repair_failed", agent=agent_name)
        return current
