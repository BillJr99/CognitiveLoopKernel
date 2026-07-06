"""Unit tests for clk_harness.orchestration.response_quality."""

from __future__ import annotations

import pytest

from clk_harness.orchestration import response_quality as rq


def test_empty_response_flagged() -> None:
    q = rq.score("")
    assert not q.ok
    assert "empty" in q.flags
    assert q.recoverable is True


def test_short_response_flagged() -> None:
    q = rq.score("ok", min_chars=40)
    assert not q.ok
    assert "empty" in q.flags


def test_substantive_response_passes() -> None:
    text = (
        "Here is a substantive answer covering the points raised. "
        "It includes a recommendation and rationale."
    )
    q = rq.score(text)
    assert q.ok, q.flags
    assert q.score == 1.0


def test_malformed_action_block_flagged() -> None:
    # Two unclosed ACTION blocks (balance=2) must be flagged.
    # A single EOF-truncated block (balance=1) is tolerated because the
    # parser handles it correctly; only genuinely interleaved unclosed
    # blocks corrupt parsing and warrant a retry.
    text = (
        "Doing the work now.\n\n"
        "ACTION: write\n"
        "PATH: foo.py\n"
        "CONTENT:\n"
        "print('hello')\n"
        # Missing END_ACTION — first open block
        "ACTION: run\n"
        "CMD: echo ok\n"
        # Missing END_ACTION — second open block
    )
    q = rq.score(text)
    assert "malformed_action" in q.flags
    assert not q.ok


def test_single_eof_truncated_action_tolerated() -> None:
    # One unclosed block at EOF is forgiven — the parser handles it.
    text = (
        "Doing the work now.\n\n"
        "ACTION: write\n"
        "PATH: foo.py\n"
        "CONTENT:\n"
        "print('hello')\n"
        # Missing END_ACTION — EOF truncation of final block
    )
    q = rq.score(text)
    assert "malformed_action" not in q.flags


def test_balanced_action_block_passes() -> None:
    text = (
        "Doing the work now.\n\n"
        "ACTION: write\n"
        "PATH: foo.py\n"
        "CONTENT:\n"
        "print('hello')\n"
        "END_ACTION\n"
        "\n"
        "Wrapped up."
    )
    q = rq.score(text)
    assert "malformed_action" not in q.flags


def test_progress_signal_yes() -> None:
    assert rq.progress_signal("Did the thing.\nPROGRESS: yes\n") is True


def test_progress_signal_no() -> None:
    assert rq.progress_signal("Blocked on X.\nPROGRESS: no\n") is False


def test_progress_signal_absent() -> None:
    assert rq.progress_signal("No marker here.") is None
    assert rq.progress_signal("") is None
    assert rq.progress_signal(None) is None


def test_progress_signal_last_marker_wins() -> None:
    text = "PROGRESS: no\nReconsidered after the fix landed.\nPROGRESS: yes\n"
    assert rq.progress_signal(text) is True


def test_malformed_post_block_flagged() -> None:
    text = (
        "POST: finding\n"
        "BODY:\n"
        "important result\n"
        # Missing END_POST
        "More text\n"
    )
    q = rq.score(text)
    assert "malformed_post" in q.flags
    assert not q.ok


def test_missing_outputs_flagged() -> None:
    text = (
        "POST: finding\n"
        "PRODUCES: alpha\n"
        "BODY:\n"
        "the alpha result\n"
        "END_POST\n"
    )
    q = rq.score(text, expected_outputs=["alpha", "beta"])
    assert "outputs_missing" in q.flags
    assert "beta" in q.reasons[0] or any("beta" in r for r in q.reasons)


def test_outputs_missing_repair_hint_shows_example_post() -> None:
    q = rq.score("Some content that is long enough to pass the empty check.", expected_outputs=["post_draft"])
    assert "outputs_missing" in q.flags
    hint = q.repair_hint()
    # Must show the missing key AND a concrete POST block example
    assert "post_draft" in hint
    assert "POST: finding" in hint
    assert "PRODUCES: post_draft" in hint
    assert "END_POST" in hint


def test_satisfied_outputs_pass() -> None:
    text = (
        "POST: finding\n"
        "PRODUCES: alpha beta\n"
        "BODY:\n"
        "covered both\n"
        "END_POST\n"
        "Continuing with analysis to ensure the response is substantive."
    )
    q = rq.score(text, expected_outputs=["alpha", "beta"])
    assert "outputs_missing" not in q.flags


def test_low_confidence_flagged() -> None:
    text = (
        "Here is a long enough answer that includes uncertainty markers.\n"
        "CONFIDENCE: 0.2\n"
    )
    q = rq.score(text)
    assert q.confidence == pytest.approx(0.2)
    assert "low_confidence" in q.flags


def test_high_confidence_passes() -> None:
    text = (
        "Here is a long enough answer including a confidence line.\n"
        "CONFIDENCE: 0.9\n"
    )
    q = rq.score(text)
    assert q.confidence == pytest.approx(0.9)
    assert "low_confidence" not in q.flags
    assert q.ok


def test_needs_review_flagged() -> None:
    text = (
        "Here is a long enough answer including a review marker.\n"
        "NEEDS_REVIEW: true\n"
    )
    q = rq.score(text)
    assert q.needs_review is True
    assert "needs_review_self" in q.flags


def test_refusal_marks_not_recoverable() -> None:
    text = (
        "I cannot help with that request, sorry."
    )
    q = rq.score(text)
    assert "refusal" in q.flags
    assert not q.ok
    assert not q.recoverable
    assert not rq.is_recoverable(q)


def test_repair_hint_lists_reasons() -> None:
    text = ""
    q = rq.score(text)
    hint = q.repair_hint()
    assert hint
    assert "empty" in hint.lower() or "minimum" in hint.lower()


def test_repair_hint_empty_for_ok() -> None:
    text = "Long enough to clear the empty threshold; this is fine."
    q = rq.score(text)
    assert q.ok
    assert q.repair_hint() == ""


def test_confidence_on_0_100_scale_normalised() -> None:
    text = "Long answer.\nCONFIDENCE: 85"
    q = rq.score(text)
    assert q.confidence == pytest.approx(0.85)


def test_require_confidence_flag() -> None:
    text = "Long enough answer without a CONFIDENCE line at all here."
    q = rq.score(text, require_confidence=True)
    assert "confidence_missing" in q.flags


def test_require_confidence_off_by_default() -> None:
    text = "Long enough answer without a CONFIDENCE line at all here."
    q = rq.score(text)
    assert "confidence_missing" not in q.flags
    assert q.ok


def test_malformed_todos_flagged() -> None:
    q = rq.score("TODOS:\n- [ ] x\n(no closing marker)", min_chars=1)
    assert "malformed_todos" in q.flags
    assert not q.ok


def test_balanced_todos_not_flagged() -> None:
    q = rq.score(
        "TODOS:\n- [ ] x\nEND_TODOS\nplus a substantive sentence of real content.",
        min_chars=1,
    )
    assert "malformed_todos" not in q.flags
