"""Tests for the style lints (lints.py)."""

from __future__ import annotations

import pytest

from resume_builder.lints import (
    LintWarning,
    lint,
    lint_cliches,
    lint_impact_density,
    lint_length_target,
    lint_verb_diversity,
)
from resume_builder.schema import (
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


def _make(summary=None, items=None):
    items = items or []
    return TailoredResume(
        summary=summary,
        sections=[TailoredSection(name="experience", items=items)],
    )


def _bullet(sid, text):
    return TailoredBullet(source_id=sid, rewritten_text=text)


def _item(container_id, bullets):
    return TailoredItem(source_id=container_id, bullets=bullets)


# ---------- clichés ----------


def test_cliches_caught():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Results-driven engineer who is a team player and wears many hats."),
            _bullet("b2", "Shipped the streaming pipeline; processes 2B events/day."),
        ]),
    ])
    out = lint_cliches(t)
    rules = {w.rule for w in out}
    assert rules == {"cliche"}
    # one warning per offending bullet (not per cliché in that bullet)
    sids = {w.source_id for w in out}
    assert sids == {"b1"}


def test_cliches_in_summary():
    t = _make(
        summary="Passionate, dynamic, hardworking engineer.",
        items=[_item("exp-1", [_bullet("b1", "Built a thing.")])],
    )
    out = lint_cliches(t)
    assert any(w.source_id == "<summary>" for w in out)


def test_clean_text_no_cliche_warning():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Built a streaming ingest pipeline that processes 2B events/day."),
            _bullet("b2", "Shipped the audit log feature for 300 enterprise customers."),
        ]),
    ])
    assert lint_cliches(t) == []


# ---------- verb diversity ----------


def test_verb_diversity_repeat_caught():
    t = _make(items=[
        _item("exp-1", [
            _bullet(f"b{i}", f"Led project {i} successfully.")
            for i in range(4)
        ]),
    ])
    out = lint_verb_diversity(t)
    assert len(out) == 1
    assert out[0].rule == "verb-diversity"
    assert "led" in out[0].message.lower()


def test_verb_diversity_below_threshold_silent():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Built a thing"),
            _bullet("b2", "Shipped a thing"),
            _bullet("b3", "Owned a thing"),
            _bullet("b4", "Drove a thing"),
        ]),
    ])
    assert lint_verb_diversity(t) == []


def test_verb_diversity_skips_tiny_resume():
    """No nagging on a 2-bullet resume even if verbs match."""
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Led one thing"),
            _bullet("b2", "Led another"),
        ]),
    ])
    assert lint_verb_diversity(t) == []


# ---------- impact density ----------


def test_impact_density_warns_on_outlier():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Cut p99 latency from 480ms to 95ms"),
            _bullet("b2", "Saved $40K/year in storage"),
            _bullet("b3", "Mentored 4 junior engineers"),
            _bullet("b4", "Reduced deploy times from 18 minutes to 4 minutes"),
            _bullet("b5", "Built a thing"),
        ]),
    ])
    out = lint_impact_density(t)
    assert len(out) == 1
    assert out[0].source_id == "b5"
    assert out[0].rule == "impact-density"


def test_impact_density_silent_when_no_pattern():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Built a thing"),
            _bullet("b2", "Shipped a thing"),
            _bullet("b3", "Drove a thing"),
            _bullet("b4", "Owned a thing"),
            _bullet("b5", "Mentored 4 engineers"),
        ]),
    ])
    assert lint_impact_density(t) == []


# ---------- length target ----------


def test_length_one_pager_overflow():
    long_text = " ".join(["word"] * 80)  # 80 words per bullet
    t = _make(items=[
        _item("exp-1", [_bullet(f"b{i}", long_text) for i in range(10)]),
    ])
    out = lint_length_target(t, Pointers(length="1page"))
    assert len(out) == 1
    assert "words" in out[0].message
    assert out[0].rule == "length"


def test_length_no_pointer_silent():
    t = _make(items=[_item("exp-1", [_bullet("b1", "Built a thing")])])
    assert lint_length_target(t, None) == []
    assert lint_length_target(t, Pointers()) == []


def test_length_custom_word_count_within_band_ok():
    t = _make(items=[
        _item("exp-1", [_bullet(f"b{i}", " ".join(["word"] * 50)) for i in range(9)]),
    ])
    # 450 words, target = 450 ± 15% = [382, 517]. 450 is in band.
    assert lint_length_target(t, Pointers(length="450")) == []


# ---------- combined lint() runs all rules ----------


def test_lint_combines_rules():
    t = _make(items=[
        _item("exp-1", [
            _bullet("b1", "Led the rewrite of dispatch service, cutting latency."),
            _bullet("b2", "Led the migration to Postgres."),
            _bullet("b3", "Led the rollout of K8s operator. Team player effort."),
        ]),
    ])
    out = lint(t, pointers=Pointers())
    rules = {w.rule for w in out}
    assert "verb-diversity" in rules
    assert "cliche" in rules
