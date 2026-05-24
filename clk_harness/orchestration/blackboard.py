"""Shared blackboard for cross-agent context passing.

The blackboard is a structured shared scratchpad workers post to and
read from across stage boundaries. It lives at ``.clk/blackboard/``
(under the harness state tree, hidden from agents' direct ACTION:write
reach) and is the canonical channel for one agent to hand findings to
another without forcing the chief to copy text between prompts.

A post is a JSON file:

    .clk/blackboard/<ts>-<author>-<slug>.json
    {
      "id":         "<ts>-<author>-<slug>",
      "author":     "researcher",
      "post_type":  "finding" | "decision" | "question" | "summary" | ...,
      "stage_id":   "research_a",        # optional
      "workflow":   "engineering",       # optional
      "consumes":   ["<other-post-id>", ...],
      "produces":   ["<contract-key>", ...],
      "body":       "free-form markdown",
      "ts":         "2026-05-01T12:34:56"
    }

Workers post by emitting a ``POST:`` block in their response (parsed
in ``parse_post_blocks`` below); the harness translates it into a JSON
file under ``.clk/blackboard/``. Workers may also write JSON files
directly via ``ACTION: write`` with path ``blackboard/<id>.json`` — the
action sandbox rewrites that to ``.clk/blackboard/<id>.json`` as a
permitted exception to the general ``.clk/`` write restriction. POST
blocks are preferred because the harness stamps metadata automatically.

The runner reads the blackboard via :func:`digest`, which returns a
filtered text view suitable for splicing into a worker's prompt under
the ``$blackboard_digest`` placeholder. Filters are typically driven
by the calling ``WorkflowStage.inputs`` list.

Schema decisions
- Posts are immutable after write. To revise, post a new entry with
  ``consumes: [<old-id>]`` and ``post_type: "revision"``.
- Posts live under ``.clk/`` so they survive an agent ``rm -rf`` of the
  product tree — they are project memory, not deliverables.
- We deliberately do not store provider tokens or large file contents in
  posts — keep them small and treat them as the "headlines" workers
  write to one another. Big artifacts go in the project root proper.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence

from ..config import Paths
from ..utils.activity_log import log_event
from ..utils.logging_utils import log, log_exception


def blackboard_dir(paths: Paths) -> Path:
    """Directory all blackboard posts live in.

    Lives under ``.clk/`` so it is part of harness state, not the
    product. Agents may write JSON files here via ``ACTION: write``
    with path ``blackboard/<id>.json`` (the harness rewrites it); POST
    blocks are preferred because the harness stamps metadata
    automatically.  Edit, append, and delete are rejected by the
    sandbox to preserve post immutability.
    """
    return paths.blackboard


# ---------------------------------------------------------------------------
# Post dataclass
# ---------------------------------------------------------------------------


@dataclass
class Post:
    id: str
    author: str
    post_type: str = "note"
    body: str = ""
    consumes: List[str] = field(default_factory=list)
    produces: List[str] = field(default_factory=list)
    stage_id: str = ""
    workflow: str = ""
    ts: str = ""
    # Inter-agent Q&A routing. When ``target_agent`` is set on a
    # ``post_type: "question"`` post, the harness dispatches the named
    # agent to answer it before the asker's run returns (when
    # ``urgency == "blocking"``) or surfaces it to the chief on the
    # next supervise cycle (when ``urgency == "async"``).
    target_agent: str = ""
    urgency: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "author": self.author,
            "post_type": self.post_type,
            "stage_id": self.stage_id,
            "workflow": self.workflow,
            "consumes": list(self.consumes),
            "produces": list(self.produces),
            "body": self.body,
            "ts": self.ts,
            "target_agent": self.target_agent,
            "urgency": self.urgency,
        }

    @classmethod
    def from_dict(cls, raw: Dict[str, Any], *, fallback_id: str = "") -> "Post":
        return cls(
            id=str(raw.get("id") or fallback_id or ""),
            author=str(raw.get("author") or ""),
            post_type=str(raw.get("post_type") or "note"),
            body=str(raw.get("body") or ""),
            consumes=[str(x) for x in (raw.get("consumes") or [])],
            produces=[str(x) for x in (raw.get("produces") or [])],
            stage_id=str(raw.get("stage_id") or ""),
            workflow=str(raw.get("workflow") or ""),
            ts=str(raw.get("ts") or ""),
            target_agent=str(raw.get("target_agent") or ""),
            urgency=str(raw.get("urgency") or ""),
        )


# ---------------------------------------------------------------------------
# I/O helpers
# ---------------------------------------------------------------------------


_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify(text: str, max_len: int = 32) -> str:
    if not text:
        return "post"
    s = _SLUG_RE.sub("-", text.lower()).strip("-")
    return (s or "post")[:max_len]


def _new_post_id(author: str, slug_hint: str) -> str:
    ts = datetime.now().strftime("%Y%m%dT%H%M%S%f")
    return f"{ts}-{_slugify(author or 'agent', 20)}-{_slugify(slug_hint or 'post', 32)}"


def post(
    paths: Paths,
    *,
    author: str,
    body: str,
    post_type: str = "note",
    consumes: Optional[Sequence[str]] = None,
    produces: Optional[Sequence[str]] = None,
    stage_id: str = "",
    workflow: str = "",
    slug_hint: str = "",
    target_agent: str = "",
    urgency: str = "",
) -> Post:
    """Persist a new post and return it. Always succeeds (best-effort logging).

    Use the slug_hint to make the on-disk filename hint at the post's
    topic — usually the post_type or the first words of the body.
    """
    bb = blackboard_dir(paths)
    bb.mkdir(parents=True, exist_ok=True)
    pid = _new_post_id(author, slug_hint or post_type)
    p = Post(
        id=pid,
        author=author or "agent",
        post_type=post_type or "note",
        body=body or "",
        consumes=list(consumes or []),
        produces=list(produces or []),
        stage_id=stage_id or "",
        workflow=workflow or "",
        ts=datetime.now().isoformat(timespec="seconds"),
        target_agent=target_agent or "",
        urgency=urgency or "",
    )
    target = bb / f"{pid}.json"
    try:
        target.write_text(json.dumps(p.to_dict(), indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        log_exception("orchestration.blackboard.post.write", exc)
    try:
        log_event(
            paths,
            "blackboard_post",
            agent=p.author,
            post_id=p.id,
            post_type=p.post_type,
            stage_id=p.stage_id,
            workflow=p.workflow,
            consumes=list(p.consumes),
            produces=list(p.produces),
            body_chars=len(p.body or ""),
        )
    except Exception:
        pass
    return p


def list_posts(paths: Paths) -> List[Post]:
    """Return all posts on the blackboard, oldest first by id (= timestamp)."""
    bb = blackboard_dir(paths)
    if not bb.exists():
        return []
    out: List[Post] = []
    for f in sorted(bb.glob("*.json")):
        try:
            raw = json.loads(f.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                continue
            out.append(Post.from_dict(raw, fallback_id=f.stem))
        except Exception as exc:
            log_exception(f"orchestration.blackboard.list_posts.{f.name}", exc)
    return out


def read(paths: Paths, post_id: str) -> Optional[Post]:
    if not post_id:
        return None
    target = blackboard_dir(paths) / f"{post_id}.json"
    if not target.exists():
        return None
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
        return Post.from_dict(raw, fallback_id=post_id)
    except Exception as exc:
        log_exception("orchestration.blackboard.read", exc)
        return None


# ---------------------------------------------------------------------------
# Filtering / digest
# ---------------------------------------------------------------------------


def _matches_filter(p: Post, selector: str) -> bool:
    """Match a Post against a filter token used in WorkflowStage.inputs.

    Selector grammar (all case-insensitive on the left side):

      ``id:<post_id_or_glob>``    exact id match or substring/glob
      ``type:<post_type>``        exact post_type match
      ``author:<agent_name>``     exact author match
      ``stage:<stage_id>``        exact producing-stage match
      ``produces:<contract>``     post.produces contains the contract key
      ``<bare>``                  treated as ``id:<bare>`` then ``type:<bare>``

    A ``*`` in the value behaves as a glob (matches any chars). Returns
    True on the first match.
    """
    if not selector:
        return False
    s = selector.strip()
    if not s:
        return False
    if ":" in s:
        kind, _, value = s.partition(":")
        kind = kind.strip().lower()
        value = value.strip()
    else:
        kind = ""
        value = s
    value_l = value.lower()

    def _glob_match(haystack: str, needle: str) -> bool:
        h = (haystack or "").lower()
        n = (needle or "").lower()
        if not n:
            return False
        if "*" not in n:
            return n == h or n in h
        # tiny glob: convert * to .* and anchor
        pattern = "^" + re.escape(n).replace(r"\*", ".*") + "$"
        return re.match(pattern, h) is not None

    if kind in ("id", ""):
        if _glob_match(p.id, value_l):
            return True
        if kind == "id":
            return False
    if kind in ("type", ""):
        if (p.post_type or "").lower() == value_l:
            return True
        if kind == "type":
            return False
    if kind == "author":
        return (p.author or "").lower() == value_l
    if kind == "stage":
        return (p.stage_id or "").lower() == value_l
    if kind == "produces":
        return any((c or "").lower() == value_l for c in p.produces)
    return False


def filter_posts(posts: Iterable[Post], selectors: Sequence[str]) -> List[Post]:
    """Return posts matching ANY selector. Empty selectors → all posts."""
    items = list(posts)
    sels = [s for s in (selectors or []) if (s or "").strip()]
    if not sels:
        return items
    out: List[Post] = []
    seen: set = set()
    for p in items:
        if p.id in seen:
            continue
        if any(_matches_filter(p, s) for s in sels):
            out.append(p)
            seen.add(p.id)
    return out


def find_outputs_satisfied(
    posts: Iterable[Post],
    *,
    stage_id: str,
    expected: Sequence[str],
) -> List[str]:
    """Return the subset of ``expected`` contract keys that have NOT been
    posted by ``stage_id``.

    A post satisfies an expected key when either:
      * its ``produces`` list contains the key, OR
      * its id ends with ``-<slug>`` matching the slugified key.
    """
    expected = [e for e in (expected or []) if (e or "").strip()]
    if not expected:
        return []
    by_stage = [p for p in posts if (p.stage_id or "") == stage_id]
    missing: List[str] = []
    for key in expected:
        slug = _slugify(key)
        ok = False
        for p in by_stage:
            if key in p.produces:
                ok = True
                break
            if p.id.endswith(f"-{slug}"):
                ok = True
                break
        if not ok:
            missing.append(key)
    return missing


def find_unanswered_questions(
    paths: Paths,
    *,
    target_agent: Optional[str] = None,
) -> List[Post]:
    """Return question posts that have no matching answer.

    A ``post_type="question"`` post is treated as answered when some
    later ``post_type="answer"`` post lists the question's id in its
    ``consumes``. When ``target_agent`` is given, only questions
    targeted at that agent are returned.
    """
    posts = list_posts(paths)
    questions = [p for p in posts if p.post_type == "question"]
    if target_agent:
        questions = [p for p in questions if (p.target_agent or "") == target_agent]
    answered_ids: set = set()
    for p in posts:
        if p.post_type != "answer":
            continue
        for qid in (p.consumes or []):
            answered_ids.add(str(qid))
    return [q for q in questions if q.id not in answered_ids]


def digest(
    paths: Paths,
    *,
    selectors: Optional[Sequence[str]] = None,
    max_posts: int = 20,
    max_chars_per_post: int = 800,
    header: str = "Blackboard digest",
) -> str:
    """Build a markdown digest of recent posts for a worker prompt.

    Newest posts come first. Selectors filter; if empty, returns the
    most recent ``max_posts`` posts of any kind. Each body is truncated
    to ``max_chars_per_post`` so prompts stay bounded.
    """
    posts = list_posts(paths)
    if selectors:
        posts = filter_posts(posts, selectors)
    posts = list(reversed(posts))[:max(0, int(max_posts))]
    if not posts:
        return f"{header}: (empty)"
    lines: List[str] = [f"{header} ({len(posts)} most recent):"]
    for p in posts:
        body = (p.body or "").strip()
        if len(body) > max_chars_per_post:
            body = body[: max_chars_per_post].rstrip() + " …"
        meta_bits = [f"id={p.id}", f"author={p.author}", f"type={p.post_type}"]
        if p.stage_id:
            meta_bits.append(f"stage={p.stage_id}")
        if p.produces:
            meta_bits.append(f"produces={','.join(p.produces)}")
        lines.append("- " + " ".join(meta_bits))
        if body:
            for ln in body.splitlines():
                lines.append(f"  {ln}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# POST: block parsing (lighter alternative to ACTION:write a JSON file)
# ---------------------------------------------------------------------------


_POST_HEAD_RE = re.compile(r"^\s*POST\s*:\s*(?P<type>[A-Za-z][A-Za-z0-9_\-]*)\s*$", re.MULTILINE)
_POST_END_RE = re.compile(r"^\s*END_POST\s*$", re.IGNORECASE)
_POST_FIELD_RE = re.compile(
    r"^(PRODUCES|CONSUMES|TITLE|SLUG|TO|URGENCY)\s*:\s*(.*)$", re.IGNORECASE
)
_POST_BODY_RE = re.compile(r"^\s*BODY\s*:\s*$", re.IGNORECASE)


def parse_post_blocks(text: str) -> List[Dict[str, Any]]:
    """Extract ``POST:`` blocks from an agent's response.

    Block grammar::

        POST: <post_type>
        TITLE: <one line>          # optional, used as slug hint
        PRODUCES: <key1, key2>     # optional, comma-separated
        CONSUMES: <id1, id2>       # optional, comma-separated
        BODY:
        <multi-line markdown body>
        END_POST
    """
    if not text or "POST:" not in text:
        return []
    out: List[Dict[str, Any]] = []
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        m = _POST_HEAD_RE.match(lines[i])
        if not m:
            i += 1
            continue
        block: Dict[str, Any] = {
            "post_type": m.group("type").strip(),
            "title": "",
            "slug": "",
            "produces": [],
            "consumes": [],
            "body": "",
            "target_agent": "",
            "urgency": "",
        }
        i += 1
        body_lines: List[str] = []
        in_body = False
        while i < len(lines):
            line = lines[i]
            if _POST_END_RE.match(line):
                i += 1
                break
            if not in_body:
                fm = _POST_FIELD_RE.match(line)
                if fm:
                    key = fm.group(1).upper()
                    val = fm.group(2).strip()
                    if key == "TITLE":
                        block["title"] = val
                    elif key == "SLUG":
                        block["slug"] = val
                    elif key == "PRODUCES":
                        block["produces"] = [
                            x.strip() for x in re.split(r"[,\s]+", val) if x.strip()
                        ]
                    elif key == "CONSUMES":
                        block["consumes"] = [
                            x.strip() for x in re.split(r"[,\s]+", val) if x.strip()
                        ]
                    elif key == "TO":
                        block["target_agent"] = re.sub(r"[^A-Za-z0-9_\-]", "", val)
                    elif key == "URGENCY":
                        u = val.strip().lower()
                        if u in {"blocking", "block", "sync"}:
                            block["urgency"] = "blocking"
                        elif u in {"async", "background", "deferred"}:
                            block["urgency"] = "async"
                    i += 1
                    continue
                if _POST_BODY_RE.match(line):
                    in_body = True
                    i += 1
                    continue
                i += 1
                continue
            body_lines.append(line)
            i += 1
        block["body"] = "\n".join(body_lines).strip("\n")
        if block["post_type"] and block["body"]:
            out.append(block)
    return out


def apply_post_blocks(
    paths: Paths,
    text: str,
    *,
    author: str,
    stage_id: str = "",
    workflow: str = "",
) -> List[Post]:
    """Parse + persist every POST block in ``text``. Returns posts created."""
    blocks = parse_post_blocks(text)
    out: List[Post] = []
    for b in blocks:
        try:
            slug_hint = b.get("slug") or b.get("title") or b.get("post_type") or ""
            p = post(
                paths,
                author=author,
                body=b.get("body", ""),
                post_type=b.get("post_type", "note"),
                consumes=b.get("consumes") or [],
                produces=b.get("produces") or [],
                stage_id=stage_id,
                workflow=workflow,
                slug_hint=slug_hint,
                target_agent=b.get("target_agent") or "",
                urgency=b.get("urgency") or "",
            )
            out.append(p)
        except Exception as exc:
            log_exception("orchestration.blackboard.apply_post_blocks", exc)
    if out:
        log(
            f"blackboard[{author}]: posted {len(out)} entries "
            f"({', '.join(p.post_type for p in out)})"
        )
    return out


# ---------------------------------------------------------------------------
# Prompt fragment exposed to agent system prompts
# ---------------------------------------------------------------------------


BLACKBOARD_PROTOCOL = """\
Blackboard protocol (shared scratchpad for cross-agent context)

You can read posts other agents have written, and you can post your
own findings so later agents (and the chief) see them. The blackboard
lives at ``.clk/blackboard/`` as JSON files. You may write there in
two ways:

1. POST block (preferred) — the harness stamps metadata automatically:

  POST: <post_type>
  TITLE: <short one-line title>            # optional
  PRODUCES: <contract_key1, contract_key2> # optional, satisfies stage outputs
  CONSUMES: <other_post_id1, other_post_id2> # optional, links provenance
  BODY:
  <multi-line markdown body — keep it short, headline-style>
  END_POST

2. Direct write — use ACTION:write with path ``blackboard/<filename>.json``
   (the harness rewrites it to ``.clk/blackboard/<filename>.json``).
   The JSON must match the post schema: id, author, post_type, body,
   ts, stage_id, workflow, consumes, produces.

Rules
- Post at most a handful of entries per turn — the blackboard is for
  headlines other agents will read, not for full artifacts. Big files
  still go in $project_root via ACTION:write.
- A post is immutable. To revise, write a new POST with
  CONSUMES: <old_post_id> and a post_type like ``revision``.
- If the stage you are running declared OUTPUTS in its workflow YAML,
  emit a POST whose PRODUCES list includes each declared key, otherwise
  the runner will warn that the contract was unmet.
- The blackboard digest you receive in this prompt is filtered to your
  stage's declared INPUTS. To see more, ask the chief to widen them.
"""
