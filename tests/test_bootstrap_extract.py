"""Tests for bootstrap.py — extract_drafts(), validate_draft(), merge helpers.

The Web Speech API and live LLM aren't reachable from pytest, so the
LLM provider is replaced by a fake whose `complete()` returns a scripted
JSON payload. This keeps the test deterministic + cheap, and exercises
every code path the wizard endpoint depends on.
"""

from __future__ import annotations

import json

import pytest

from resume_builder.bootstrap import (
    MIN_CHUNK_CHARS,
    ExtractError,
    extract_drafts,
    merge_drafts_preserving_confirmed,
    too_short,
    validate_draft,
)
from resume_builder.llm import LLMProvider
from resume_builder.session_store import DraftAccomplishment, TimeChunk


# ---------- fakes ----------


class FakeProvider(LLMProvider):
    """Returns a scripted response. Records the prompt for assertions."""

    name = "fake"

    @classmethod
    def is_available(cls) -> bool:
        return True

    def __init__(self, response: str):
        self.response = response
        self.last_system = None
        self.last_user = None

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        self.last_system = system_prompt
        self.last_user = user_message
        return self.response


def _chunk(notes: str = "x" * (MIN_CHUNK_CHARS + 1)) -> TimeChunk:
    return TimeChunk(
        id="chunk-test", label="H1 2024", start="2024-01", end="2024-07",
        raw_notes=notes,
    )


# ---------- too_short / MIN_CHUNK_CHARS ----------


def test_too_short_below_threshold():
    chunk = _chunk(notes="too brief")
    assert too_short(chunk) is True


def test_too_short_above_threshold():
    chunk = _chunk(notes="x" * MIN_CHUNK_CHARS)
    assert too_short(chunk) is False


def test_too_short_strips_whitespace():
    chunk = _chunk(notes="   " + "x" * 5 + "   ")
    assert too_short(chunk) is True


def test_extract_below_threshold_raises():
    fake = FakeProvider(json.dumps({"drafts": []}))
    with pytest.raises(ExtractError, match="chars"):
        extract_drafts(_chunk(notes="hi"), fake)


# ---------- happy path ----------


HAPPY_NOTES = (
    "shipped dispatch service rewrite from Ruby to Go in Q1; "
    "p99 latency dropped from 480ms to 95ms; "
    "led 3-person team; mentored 2 juniors; ran on-call rotation"
)

HAPPY_RESPONSE = json.dumps({
    "drafts": [
        {
            "raw_quote": "shipped dispatch service rewrite from Ruby to Go in Q1; p99 latency dropped from 480ms to 95ms",
            "draft_bullet": "Led migration of dispatch service from Ruby to Go, cutting p99 latency from 480ms to 95ms by [METHOD]",
            "impact_score_hint": 5,
            "tags_hint": ["backend", "go", "ruby", "performance"],
        },
        {
            "raw_quote": "led 3-person team; mentored 2 juniors",
            "draft_bullet": "Mentored 2 junior engineers and led a 3-person team by [METHOD]",
            "impact_score_hint": 4,
            "tags_hint": ["leadership", "mentorship"],
        },
    ],
})


def test_extract_returns_drafts_and_prompt_and_response():
    fake = FakeProvider(HAPPY_RESPONSE)
    chunk = _chunk(notes=HAPPY_NOTES)
    drafts, user_prompt, raw = extract_drafts(chunk, fake)

    assert len(drafts) == 2
    assert user_prompt == fake.last_user
    assert raw == HAPPY_RESPONSE


def test_drafts_carry_chunk_id_and_unique_ids():
    fake = FakeProvider(HAPPY_RESPONSE)
    chunk = _chunk(notes=HAPPY_NOTES)
    drafts, _, _ = extract_drafts(chunk, fake)
    assert {d.chunk_id for d in drafts} == {"chunk-test"}
    assert len({d.id for d in drafts}) == 2  # unique


def test_drafts_classified_with_bock_tier():
    fake = FakeProvider(HAPPY_RESPONSE)
    drafts, _, _ = extract_drafts(_chunk(notes=HAPPY_NOTES), fake)
    first = drafts[0]
    # "by [METHOD]" placeholder doesn't satisfy the z-clause regex,
    # so the bullet should be tier=better at best.
    assert first.tier in ("better", "awesome", "original")
    # If awesome, no missing tags. If better, missing should include z_method.
    if first.tier == "better":
        assert "z_method" in first.missing


def test_extract_user_prompt_includes_chunk_metadata_and_notes():
    fake = FakeProvider(HAPPY_RESPONSE)
    chunk = _chunk(notes=HAPPY_NOTES)
    extract_drafts(chunk, fake, role_family="software-engineering")
    assert "H1 2024" in fake.last_user
    assert "2024-01" in fake.last_user
    assert "software-engineering" in fake.last_user
    assert HAPPY_NOTES in fake.last_user


def test_extract_user_prompt_handles_role_other():
    fake = FakeProvider(HAPPY_RESPONSE)
    chunk = _chunk(notes=HAPPY_NOTES)
    extract_drafts(
        chunk, fake,
        role_family="other", role_family_other="ai safety researcher",
    )
    assert "ai safety researcher" in fake.last_user


# ---------- malformed responses ----------


def test_extract_raises_on_invalid_json():
    fake = FakeProvider("not even close to JSON")
    with pytest.raises(ExtractError, match="(did not contain|parse)"):
        extract_drafts(_chunk(notes=HAPPY_NOTES), fake)


def test_extract_strips_code_fences():
    fenced = "```json\n" + HAPPY_RESPONSE + "\n```"
    fake = FakeProvider(fenced)
    drafts, _, _ = extract_drafts(_chunk(notes=HAPPY_NOTES), fake)
    assert len(drafts) == 2


def test_extract_recovers_from_leading_prose():
    prefixed = "Sure, here's the JSON you asked for:\n\n" + HAPPY_RESPONSE
    fake = FakeProvider(prefixed)
    drafts, _, _ = extract_drafts(_chunk(notes=HAPPY_NOTES), fake)
    assert len(drafts) == 2


def test_extract_raises_when_no_drafts_field():
    fake = FakeProvider(json.dumps({"items": []}))
    with pytest.raises(ExtractError, match="drafts"):
        extract_drafts(_chunk(notes=HAPPY_NOTES), fake)


def test_extract_skips_malformed_draft_entries():
    """An entry without raw_quote or draft_bullet is dropped, not raised."""
    response = json.dumps({
        "drafts": [
            {"raw_quote": "x"},  # missing draft_bullet
            {"draft_bullet": "Y"},  # missing raw_quote
            {  # valid
                "raw_quote": "shipped dispatch rewrite",
                "draft_bullet": "Migrated dispatch service by [METHOD]",
            },
        ],
    })
    fake = FakeProvider(response)
    drafts, _, _ = extract_drafts(_chunk(notes=HAPPY_NOTES), fake)
    assert len(drafts) == 1


# ---------- anti-fabrication guard (validate_draft) ----------


def _draft(bullet: str, raw_quote: str, followups=()) -> DraftAccomplishment:
    return DraftAccomplishment(
        id="d1", chunk_id="c1",
        raw_quote=raw_quote, draft_bullet=bullet,
        tier="awesome", missing=[],
        user_followups=list(followups),
    )


def test_validate_clean_draft_passes():
    d = _draft(
        bullet="Migrated dispatch service from Ruby to Go, cutting p99 from 480ms to 95ms",
        raw_quote="shipped dispatch rewrite from Ruby to Go; p99 480ms -> 95ms",
    )
    assert validate_draft(d) == []


def test_validate_catches_invented_number():
    d = _draft(
        bullet="Cut p99 latency by 99% on 12M daily requests",
        raw_quote="shipped dispatch rewrite",
    )
    reasons = validate_draft(d)
    assert reasons
    assert any("99" in r or "12m" in r.lower() for r in reasons)


def test_validate_catches_invented_proper_noun():
    d = _draft(
        bullet="Led migration to Kubernetes on AWS",
        raw_quote="shipped service rewrite",
    )
    reasons = validate_draft(d)
    assert reasons
    assert any(
        "kubernetes" in r.lower() or "aws" in r.lower() for r in reasons
    )


def test_validate_allows_placeholders():
    """`[NUMBER]` and `[METHOD]` are NOT fabrications."""
    d = _draft(
        bullet="Cut p99 latency by [NUMBER]% by [METHOD]",
        raw_quote="reduced latency on the dispatch service",
    )
    assert validate_draft(d) == []


def test_validate_accepts_user_followups_as_legal_vocab():
    """When the user supplies a number during polish, it becomes legal."""
    d = _draft(
        bullet="Cut p99 latency by 80% using a Go rewrite",
        raw_quote="reduced latency on the dispatch service",
        followups=["the actual number was 80%", "via a Go rewrite"],
    )
    assert validate_draft(d) == []


def test_validate_catches_magnitude_suffixed_number():
    """Reuses the same _NUMBER_RE that catches $99K + 12M variants."""
    d = _draft(
        bullet="Saved $99K in vendor costs",
        raw_quote="reduced cloud spend",
    )
    reasons = validate_draft(d)
    assert reasons
    # The guard's number regex picks up "99k" specifically.
    assert any("99k" in r.lower() for r in reasons)


# ---------- merge_drafts_preserving_confirmed ----------


def test_merge_preserves_confirmed_drops_unconfirmed():
    existing = [
        DraftAccomplishment(
            id="d-old-1", chunk_id="c1", raw_quote="x", draft_bullet="A",
            user_confirmed=True,
        ),
        DraftAccomplishment(
            id="d-old-2", chunk_id="c1", raw_quote="x", draft_bullet="B",
            user_confirmed=False,
        ),
    ]
    fresh = [
        DraftAccomplishment(
            id="d-new-1", chunk_id="c1", raw_quote="x", draft_bullet="C",
        ),
    ]
    merged = merge_drafts_preserving_confirmed(existing, fresh)
    ids = [d.id for d in merged]
    assert "d-old-1" in ids
    assert "d-old-2" not in ids
    assert "d-new-1" in ids
    assert len(merged) == 2


def test_merge_with_empty_existing():
    fresh = [
        DraftAccomplishment(
            id="d-new-1", chunk_id="c1", raw_quote="x", draft_bullet="A",
        ),
    ]
    merged = merge_drafts_preserving_confirmed([], fresh)
    assert merged == fresh
