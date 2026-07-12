"""Tests for bootstrap.polish_draft + WHERE_TO_LOOK constant.

Same fake-provider pattern as test_bootstrap_extract.py — the LLM is
replaced by a scripted response so the test is deterministic + cheap.

What we're checking:
- A full followup set should let the draft reach the "awesome" tier
  (action verb + measurable outcome + how-clause).
- A partial followup keeps unanswered placeholders intact instead of
  silently inventing.
- The anti-fabrication guard catches an LLM that tries to slip in a
  number not supported by raw_quote OR user_followups.
- user_followups accumulate across polish runs so the guard's legal
  vocabulary grows with the user's answers.
- WHERE_TO_LOOK exposes recall hints for every missing-piece key.
"""

from __future__ import annotations

import json

import pytest

from resume_builder.bootstrap import (
    POLISH_SYSTEM_PROMPT,
    WHERE_TO_LOOK,
    PolishError,
    polish_draft,
    validate_draft,
)
from resume_builder.llm import LLMProvider
from resume_builder.session_store import DraftAccomplishment


# ---------- fakes ----------


class FakeProvider(LLMProvider):
    name = "fake"

    @classmethod
    def is_available(cls):
        return True

    def __init__(self, response: str):
        self.response = response
        self.last_system = None
        self.last_user = None

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        self.last_system = system_prompt
        self.last_user = user_message
        return self.response


def _draft(
    bullet="Migrated dispatch service from Ruby to Go by [METHOD]",
    raw_quote="shipped dispatch rewrite from Ruby to Go",
    tier="better",
    missing=("z_method",),
    followups=(),
):
    return DraftAccomplishment(
        id="d-1", chunk_id="c-1",
        raw_quote=raw_quote, draft_bullet=bullet,
        tier=tier, missing=list(missing),
        user_followups=list(followups),
    )


# ---------- WHERE_TO_LOOK constant ----------


def test_where_to_look_has_entries_for_each_missing_kind():
    for key in ("y_metric", "z_method", "x_strong_verb"):
        assert key in WHERE_TO_LOOK, f"missing recall hints for {key}"
        assert len(WHERE_TO_LOOK[key]) >= 3, f"thin hints for {key}"


def test_where_to_look_y_metric_includes_bock_recall_sources():
    """Bock Part 02: perf reviews / OKRs / Slack / PRs / calendar / emails."""
    hints = " ".join(WHERE_TO_LOOK["y_metric"]).lower()
    assert any(s in hints for s in ("performance", "review"))
    assert "okr" in hints or "kpi" in hints
    assert "slack" in hints or "channel" in hints
    assert "calendar" in hints or "email" in hints


# ---------- happy path: full followups, reaches awesome ----------


_AWESOME_RESPONSE = json.dumps({
    "polished_bullet": "Cut dispatch service p99 latency by 80%, from 480ms to 95ms, by rewriting the worker pool in Go",
    "rationale": "Substituted user's metric and method into the [NUMBER] / [METHOD] placeholders.",
})


def test_polish_with_full_followups_reaches_awesome():
    fake = FakeProvider(_AWESOME_RESPONSE)
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="shipped dispatch rewrite; p99 dropped from 480ms to 95ms",
        missing=["y_metric", "z_method"],
    )
    polished, user_prompt, raw, warnings = polish_draft(
        d, {"y_metric": "80%", "z_method": "by rewriting the worker pool in Go"}, fake,
    )
    assert polished.tier == "awesome"
    assert "z_method" not in polished.missing
    assert "y_metric" not in polished.missing
    assert warnings == []
    assert "polished" in user_prompt.lower() or "follow-up" in user_prompt.lower()
    assert raw == _AWESOME_RESPONSE


def test_polish_accumulates_user_followups():
    fake = FakeProvider(_AWESOME_RESPONSE)
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="shipped dispatch rewrite",
        missing=["y_metric", "z_method"],
        followups=["original answer from a previous polish"],
    )
    polished, _, _, _ = polish_draft(
        d, {"y_metric": "80%", "z_method": "by rewriting the worker pool in Go"}, fake,
    )
    # Original followup preserved; new ones appended.
    assert polished.user_followups[0] == "original answer from a previous polish"
    assert "80%" in polished.user_followups
    assert any("worker pool in Go" in f for f in polished.user_followups)


# ---------- partial followups: placeholders preserved ----------


_PARTIAL_RESPONSE = json.dumps({
    "polished_bullet": "Cut p99 latency by 80% by [METHOD]",
    "rationale": "Substituted only the metric; method placeholder kept since user didn't supply one.",
})


def test_polish_with_partial_followups_keeps_remaining_placeholder():
    fake = FakeProvider(_PARTIAL_RESPONSE)
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="shipped dispatch rewrite",
        missing=["y_metric", "z_method"],
    )
    polished, _, _, warnings = polish_draft(d, {"y_metric": "80%"}, fake)
    assert "[METHOD]" in polished.draft_bullet
    assert warnings == []
    # Tier should be at most "better" because the method clause is unfilled.
    assert polished.tier in ("better", "original")


# ---------- adversarial: invented number ----------


_BAD_RESPONSE = json.dumps({
    "polished_bullet": "Cut p99 latency by 80% across 12M daily requests by rewriting in Go",
    "rationale": "Polished.",
})


def test_polish_catches_invented_number_in_fabrication_warnings():
    """LLM slips in '12M' that's not in raw_quote or any followup."""
    fake = FakeProvider(_BAD_RESPONSE)
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="shipped dispatch rewrite; p99 dropped",
        missing=["y_metric", "z_method"],
    )
    polished, _, _, warnings = polish_draft(
        d, {"y_metric": "80%", "z_method": "by rewriting in Go"}, fake,
    )
    assert warnings, "expected fabrication warning for invented number"
    assert any("12m" in w.lower() for w in warnings)


def test_polish_catches_invented_proper_noun_in_fabrication_warnings():
    bad = json.dumps({
        "polished_bullet": "Led migration to Kubernetes on AWS by rewriting worker pool in Go",
        "rationale": "Polished.",
    })
    fake = FakeProvider(bad)
    d = _draft(
        bullet="Migrated service from Ruby to Go by [METHOD]",
        raw_quote="shipped dispatch rewrite from Ruby to Go",
        missing=["z_method"],
    )
    polished, _, _, warnings = polish_draft(
        d, {"z_method": "by rewriting worker pool in Go"}, fake,
    )
    assert warnings
    assert any(
        "kubernetes" in w.lower() or "aws" in w.lower() for w in warnings
    )


# ---------- malformed LLM responses ----------


def test_polish_raises_on_invalid_json():
    fake = FakeProvider("definitely not JSON")
    with pytest.raises(PolishError):
        polish_draft(_draft(), {}, fake)


def test_polish_raises_when_polished_bullet_missing():
    fake = FakeProvider(json.dumps({"rationale": "I forgot the bullet."}))
    with pytest.raises(PolishError, match="polished_bullet"):
        polish_draft(_draft(), {}, fake)


def test_polish_tolerates_code_fences():
    fake = FakeProvider("```json\n" + _AWESOME_RESPONSE + "\n```")
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="shipped dispatch rewrite; p99 480ms to 95ms",
        missing=["y_metric", "z_method"],
    )
    polished, _, _, _ = polish_draft(
        d, {"y_metric": "80%", "z_method": "by rewriting the worker pool in Go"}, fake,
    )
    assert polished.draft_bullet


# ---------- user message includes context ----------


def test_polish_user_message_carries_tier_and_missing():
    fake = FakeProvider(_AWESOME_RESPONSE)
    d = _draft(missing=["y_metric"])
    polish_draft(d, {"y_metric": "80%"}, fake)
    assert "y_metric" in fake.last_user
    assert d.tier in fake.last_user


def test_polish_user_message_includes_no_answer_marker_when_skipped():
    fake = FakeProvider(_AWESOME_RESPONSE)
    d = _draft(missing=["y_metric", "z_method"])
    polish_draft(d, {"y_metric": "80%"}, fake)
    # The unanswered key should be flagged so the LLM keeps the placeholder.
    assert "<user did not answer" in fake.last_user


# ---------- preserves identity + provenance ----------


def test_polish_keeps_id_and_chunk_id():
    fake = FakeProvider(_AWESOME_RESPONSE)
    d = _draft()
    polished, _, _, _ = polish_draft(
        d, {"z_method": "by rewriting the worker pool in Go"}, fake,
    )
    assert polished.id == d.id
    assert polished.chunk_id == d.chunk_id
    assert polished.raw_quote == d.raw_quote


def test_polish_system_prompt_includes_no_invent_rule():
    """Smoke-test the prompt content so a future edit doesn't quietly weaken it."""
    assert "DO NOT INVENT" in POLISH_SYSTEM_PROMPT
    assert "[NUMBER]" in POLISH_SYSTEM_PROMPT
    assert "Awesome" in POLISH_SYSTEM_PROMPT
