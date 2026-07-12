"""Tests for the reflection-prompt bank."""

from __future__ import annotations

from resume_builder import role_families
from resume_builder.wizard_prompts import (
    BASE_PROMPTS,
    ROLE_PROMPTS,
    reflection_prompts,
)


def test_base_prompts_are_non_empty():
    assert len(BASE_PROMPTS) >= 6


def test_none_role_returns_base_only():
    out = reflection_prompts(None)
    assert out == list(BASE_PROMPTS)


def test_unknown_role_returns_base_only():
    out = reflection_prompts("not-a-real-family")
    assert out == list(BASE_PROMPTS)


def test_other_returns_base_only():
    """``other`` is the escape hatch — base prompts apply."""
    out = reflection_prompts("other")
    assert out == list(BASE_PROMPTS)


def test_known_role_extends_base():
    out = reflection_prompts("software-engineering")
    assert out[: len(BASE_PROMPTS)] == list(BASE_PROMPTS)
    assert len(out) > len(BASE_PROMPTS)


def test_every_keyed_role_has_at_least_three_prompts():
    """Curated banks should give the user real signal."""
    for role_id, prompts in ROLE_PROMPTS.items():
        assert len(prompts) >= 3, f"too few prompts for {role_id}: {len(prompts)}"


def test_every_keyed_role_is_a_known_family():
    """Catch typos that would silently fall through to base-only."""
    known = {rf.id for rf in role_families.all_families()}
    for role_id in ROLE_PROMPTS:
        assert role_id in known, f"unknown role family in ROLE_PROMPTS: {role_id}"


def test_prompts_are_questions():
    """Lint: every base prompt should contain a question mark (forcing thought)."""
    for prompt in BASE_PROMPTS:
        assert "?" in prompt, f"not a question: {prompt!r}"


def test_role_prompts_for_distinct_families_differ():
    """A salesperson and a software engineer should get visibly different prompts."""
    swe = set(reflection_prompts("software-engineering"))
    sales = set(reflection_prompts("sales-business-dev"))
    # The base set is shared; the role-specific portion must not be empty.
    swe_only = swe - sales
    sales_only = sales - swe
    assert swe_only, "software-engineering should add at least one unique prompt"
    assert sales_only, "sales-business-dev should add at least one unique prompt"
