"""Tests for ats.py — keyword-coverage scoring of the rendered resume."""

from __future__ import annotations

from resume_builder.ats import score_ats
from resume_builder.jd_signals import JDSignals
from resume_builder.schema import Pointers


def _signals(**kwargs) -> JDSignals:
    return JDSignals(**kwargs)


def test_full_match_scores_one():
    text = "Built a Python service backed by Postgres, deployed on Kubernetes."
    sig = _signals(top_keywords=["Python", "Postgres", "Kubernetes"])
    r = score_ats(text, signals=sig)
    assert r.score == 1.0
    assert r.matched == ["Python", "Postgres", "Kubernetes"]
    assert r.missing == []
    assert r.warnings == []


def test_partial_match():
    text = "Built a Python service. No databases mentioned."
    sig = _signals(top_keywords=["Python", "Postgres", "Kubernetes", "Kafka"])
    r = score_ats(text, signals=sig)
    assert r.matched == ["Python"]
    assert sorted(r.missing) == sorted(["Postgres", "Kubernetes", "Kafka"])
    assert r.score == 0.25
    # 25% < 60% → critical warning
    assert any(w.rule == "score-critical" for w in r.warnings)


def test_zero_match():
    text = "Nothing relevant here at all."
    sig = _signals(top_keywords=["Python", "Postgres"])
    r = score_ats(text, signals=sig)
    assert r.score == 0.0
    assert r.matched == []
    assert any(w.rule == "score-critical" for w in r.warnings)


def test_case_insensitive():
    text = "Worked extensively with POSTGRES and built apis in python."
    sig = _signals(top_keywords=["Postgres", "Python", "APIs"])
    r = score_ats(text, signals=sig)
    assert sorted(r.matched) == sorted(["Postgres", "Python", "APIs"])
    assert r.score == 1.0


def test_word_boundary_blocks_substring_false_positives():
    """'Postgres' must not match 'PostgreSQL', and 'Go' must not match 'going'."""
    text = "We use PostgreSQL and were going to deploy to AWS soon."
    sig = _signals(top_keywords=["Postgres", "Go", "AWS"])
    r = score_ats(text, signals=sig)
    assert "AWS" in r.matched
    assert "Postgres" in r.missing
    assert "Go" in r.missing


def test_multiword_keyword_matches_phrase():
    text = "Built a machine learning pipeline for image classification."
    sig = _signals(top_keywords=["machine learning"])
    r = score_ats(text, signals=sig)
    assert r.matched == ["machine learning"]


def test_must_include_pulled_in_as_keywords():
    text = "Built a Python service."
    sig = _signals(top_keywords=["Python"])
    pointers = Pointers(must_include=["Postgres", "Kubernetes"])
    r = score_ats(text, signals=sig, pointers=pointers)
    assert r.total_checked == 3
    assert r.matched == ["Python"]
    assert sorted(r.missing) == sorted(["Postgres", "Kubernetes"])


def test_dedupe_across_top_keywords_and_must_include():
    """Same keyword in both top_keywords and must_include shouldn't double-count."""
    text = "Built a Python service."
    sig = _signals(top_keywords=["Python", "Postgres"])
    pointers = Pointers(must_include=["Python"])  # duplicate
    r = score_ats(text, signals=sig, pointers=pointers)
    assert r.total_checked == 2
    assert r.matched == ["Python"]
    assert r.missing == ["Postgres"]


def test_archetype_and_seniority_included():
    text = "Senior staff role, building backend services."
    sig = _signals(
        top_keywords=["services"],
        role_archetype="backend",
        inferred_seniority="staff",
    )
    r = score_ats(text, signals=sig)
    assert "backend" in r.matched
    assert "staff" in r.matched


def test_noise_words_filtered():
    """'IC' / country codes shouldn't pollute the score."""
    text = "Built things at companies in the US."
    sig = _signals(top_keywords=["IC", "US", "things"])
    r = score_ats(text, signals=sig)
    # Only "things" was checked
    assert r.total_checked == 1
    assert r.matched == ["things"]


def test_no_keywords_returns_full_score_no_warnings():
    """Defensive: no keywords to check shouldn't blow up; counts as 100%."""
    text = "Some resume text."
    r = score_ats(text)
    assert r.score == 1.0
    assert r.total_checked == 0
    assert r.warnings == []


def test_midrange_score_triggers_low_warning_not_critical():
    text = "Python Postgres Kubernetes only three matched here."
    sig = _signals(top_keywords=["Python", "Postgres", "Kubernetes", "Kafka", "AWS"])
    r = score_ats(text, signals=sig)
    # 3/5 = 60% exactly — should NOT trigger critical (which is < 60), but might trigger low (< 80)
    assert r.score == 0.6
    rules = {w.rule for w in r.warnings}
    assert "score-critical" not in rules
    assert "score-low" in rules


def test_word_count_reported():
    text = "one two three four five"
    sig = _signals(top_keywords=["six"])
    r = score_ats(text, signals=sig)
    assert r.word_count == 5


def test_acronym_long_form_reported():
    text = "Built SEO, SQL, and LLM workflows. Used Continuous Integration (CI)."
    r = score_ats(text)
    assert r.acronyms_missing_long_form == ["SEO", "SQL", "LLM"]
    assert any(w.rule == "acronym-long-form" for w in r.warnings)
