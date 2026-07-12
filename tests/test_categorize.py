"""Tests for the categorize + merge helpers in bootstrap.py (Phase 3)."""

from __future__ import annotations

import json

import pytest

from resume_builder.bootstrap import (
    ExtractError,
    categorize_drafts,
    merge_two_drafts,
)
from resume_builder.llm import LLMProvider
from resume_builder.session_store import BUCKETS, DraftAccomplishment


class FakeProvider(LLMProvider):
    name = "fake"

    @classmethod
    def is_available(cls):
        return True

    def __init__(self, response: str):
        self.response = response
        self.last_user = None

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        self.last_user = user_message
        return self.response


def _draft(id: str, bullet: str, *, bucket=None, confirmed=False,
           tags=None, raw_quote="x", impact=None, followups=()) -> DraftAccomplishment:
    return DraftAccomplishment(
        id=id, chunk_id="c1",
        raw_quote=raw_quote, draft_bullet=bullet,
        tier="awesome", missing=[],
        impact_score_hint=impact,
        tags_hint=list(tags or []),
        bucket=bucket,
        user_confirmed=confirmed,
        user_followups=list(followups),
    )


# ---------- categorize_drafts ----------


def test_categorize_assigns_each_draft_to_a_bucket():
    response = json.dumps({
        "assignments": [
            {"draft_id": "d-1", "bucket": "experience", "confidence": 5, "rationale": "shipped in a paid role"},
            {"draft_id": "d-2", "bucket": "projects", "confidence": 4, "rationale": "side hustle"},
        ],
    })
    fake = FakeProvider(response)
    drafts = [
        _draft("d-1", "Led migration of dispatch service"),
        _draft("d-2", "Built personal blog with custom static gen"),
    ]
    assignments, _, raw = categorize_drafts(drafts, fake)
    assert raw == response
    assert assignments["d-1"]["bucket"] == "experience"
    assert assignments["d-2"]["bucket"] == "projects"
    assert assignments["d-1"]["confidence"] == 5
    assert "shipped" in assignments["d-1"]["rationale"]


def test_categorize_idempotent_skips_bucketed_drafts():
    response = json.dumps({"assignments": []})
    fake = FakeProvider(response)
    drafts = [
        _draft("d-1", "Shipped Service", bucket="experience"),
    ]
    assignments, prompt, raw = categorize_drafts(drafts, fake)
    # No call needed — short-circuits.
    assert assignments == {}
    assert prompt == ""
    assert raw == ""


def test_categorize_only_sends_unbucketed_drafts_to_llm():
    response = json.dumps({
        "assignments": [
            {"draft_id": "d-2", "bucket": "awards", "confidence": 4, "rationale": "scholarship"},
        ],
    })
    fake = FakeProvider(response)
    drafts = [
        _draft("d-1", "Already done", bucket="experience"),
        _draft("d-2", "Won a Rhodes Scholarship"),
    ]
    assignments, prompt, _ = categorize_drafts(drafts, fake)
    assert "d-1" not in assignments
    assert assignments["d-2"]["bucket"] == "awards"
    # Only d-2 should appear in the prompt (idempotence detail).
    assert "d-2" in prompt
    assert "d-1" not in prompt


def test_categorize_threads_role_family_into_prompt():
    fake = FakeProvider(json.dumps({"assignments": []}))
    categorize_drafts(
        [_draft("d-1", "x")], fake,
        role_family="software-engineering",
    )
    assert "software-engineering" in fake.last_user


def test_categorize_threads_role_other():
    fake = FakeProvider(json.dumps({"assignments": []}))
    categorize_drafts(
        [_draft("d-1", "x")], fake,
        role_family="other", role_family_other="ai safety researcher",
    )
    assert "ai safety researcher" in fake.last_user


def test_categorize_drops_invalid_bucket_assignments():
    response = json.dumps({
        "assignments": [
            {"draft_id": "d-1", "bucket": "nonsense"},
            {"draft_id": "d-2", "bucket": "experience"},
        ],
    })
    fake = FakeProvider(response)
    assignments, _, _ = categorize_drafts(
        [_draft("d-1", "x"), _draft("d-2", "y")], fake,
    )
    assert "d-1" not in assignments
    assert assignments["d-2"]["bucket"] == "experience"


def test_categorize_clamps_confidence_to_1_5():
    response = json.dumps({
        "assignments": [
            {"draft_id": "d-1", "bucket": "experience", "confidence": 99},
            {"draft_id": "d-2", "bucket": "projects", "confidence": -5},
            {"draft_id": "d-3", "bucket": "skills", "confidence": "nope"},
        ],
    })
    fake = FakeProvider(response)
    assignments, _, _ = categorize_drafts(
        [_draft("d-1", "x"), _draft("d-2", "y"), _draft("d-3", "z")], fake,
    )
    assert assignments["d-1"]["confidence"] == 5
    assert assignments["d-2"]["confidence"] == 1
    assert assignments["d-3"]["confidence"] == 3  # neutral default


def test_categorize_raises_on_malformed_json():
    fake = FakeProvider("not even json")
    with pytest.raises(ExtractError, match="(did not contain|parse)"):
        categorize_drafts([_draft("d-1", "x")], fake)


def test_categorize_raises_when_no_assignments_field():
    fake = FakeProvider(json.dumps({"items": []}))
    with pytest.raises(ExtractError, match="assignments"):
        categorize_drafts([_draft("d-1", "x")], fake)


def test_categorize_handles_code_fences():
    response = "```json\n" + json.dumps({
        "assignments": [{"draft_id": "d-1", "bucket": "experience"}],
    }) + "\n```"
    fake = FakeProvider(response)
    assignments, _, _ = categorize_drafts([_draft("d-1", "x")], fake)
    assert assignments["d-1"]["bucket"] == "experience"


def test_categorize_recovers_from_prose_prefix():
    fenced = "Sure! Here it is:\n\n" + json.dumps({
        "assignments": [{"draft_id": "d-1", "bucket": "projects"}],
    })
    fake = FakeProvider(fenced)
    assignments, _, _ = categorize_drafts([_draft("d-1", "x")], fake)
    assert assignments["d-1"]["bucket"] == "projects"


def test_buckets_constant_matches_system_prompt():
    """Sanity: all 7 canonical buckets show up in the system prompt."""
    from resume_builder.bootstrap import CATEGORIZE_SYSTEM_PROMPT
    for b in BUCKETS:
        assert b in CATEGORIZE_SYSTEM_PROMPT


# ---------- merge_two_drafts ----------


def test_merge_combines_raw_quotes_with_divider():
    a = _draft("d-a", "First bullet", raw_quote="quote from a")
    b = _draft("d-b", "Second bullet", raw_quote="quote from b")
    merged = merge_two_drafts(a, b)
    assert "quote from a" in merged.raw_quote
    assert "quote from b" in merged.raw_quote
    assert "combined with" in merged.raw_quote


def test_merge_concatenates_user_followups():
    a = _draft("d-a", "x", followups=["the number was 80%"])
    b = _draft("d-b", "y", followups=["used Go"])
    merged = merge_two_drafts(a, b)
    assert merged.user_followups == ["the number was 80%", "used Go"]


def test_merge_unions_tags_preserving_order():
    a = _draft("d-a", "x", tags=["backend", "go"])
    b = _draft("d-b", "y", tags=["go", "performance"])
    merged = merge_two_drafts(a, b)
    assert merged.tags_hint == ["backend", "go", "performance"]


def test_merge_resets_user_confirmed_to_false():
    a = _draft("d-a", "x", confirmed=True)
    b = _draft("d-b", "y", confirmed=True)
    merged = merge_two_drafts(a, b)
    assert merged.user_confirmed is False


def test_merge_inherits_higher_impact_hint():
    a = _draft("d-a", "x", impact=3)
    b = _draft("d-b", "y", impact=5)
    merged = merge_two_drafts(a, b)
    assert merged.impact_score_hint == 5


def test_merge_handles_missing_impact_hints():
    a = _draft("d-a", "x", impact=None)
    b = _draft("d-b", "y", impact=4)
    merged = merge_two_drafts(a, b)
    assert merged.impact_score_hint == 4


def test_merge_inherits_first_bucket_then_second():
    a = _draft("d-a", "x", bucket=None)
    b = _draft("d-b", "y", bucket="projects")
    merged = merge_two_drafts(a, b)
    assert merged.bucket == "projects"


def test_merge_produces_a_new_id():
    a = _draft("d-a", "x")
    b = _draft("d-b", "y")
    merged = merge_two_drafts(a, b)
    assert merged.id not in ("d-a", "d-b")
    assert merged.id.startswith("draft-")
