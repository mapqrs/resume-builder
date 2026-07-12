"""Adversarial tests: assert the no-invention guard catches fabrication."""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_builder.guard import GuardWarning, validate
from resume_builder.loaders import load_jd_text, load_master
from resume_builder.schema import (
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def master():
    return load_master(FIXTURES / "sample-master.yaml")


@pytest.fixture
def jd():
    return load_jd_text(FIXTURES / "sample-jd.txt")


def _bullet(source_id: str, text: str) -> TailoredBullet:
    return TailoredBullet(source_id=source_id, rewritten_text=text)


def _wrap(items: list[TailoredItem], section: str = "experience") -> TailoredResume:
    return TailoredResume(
        sections=[TailoredSection(name=section, items=items)]
    )


# ---------- Honest reword passes ----------


def test_honest_reword_passes(master, jd):
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            # Source: "Built an internal Kubernetes operator that reduced deploy times from 18 minutes to 4 minutes."
            _bullet(
                "exp-acme-4",
                "Built a Kubernetes operator that cut deploy time from 18 minutes to 4 minutes.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert result.warnings == []
    assert result.dropped_bullet_ids == []
    assert len(result.cleaned.sections[0].items[0].bullets) == 1


def test_jd_keyword_passes_when_in_jd(master, jd):
    # "observability" is in the JD body. The source bullet doesn't mention observability.
    # Source: "Wrote the company's first observability runbook; adopted across 6 teams."
    # The token "observability" is lowercase, so it won't be picked up as a proper noun.
    # But "ScyllaDB" would be — let's test that explicitly.
    item = TailoredItem(
        source_id="exp-bolt",
        bullets=[
            # Source: "Owned the migration from self-hosted Cassandra to managed ScyllaDB, eliminating 12 weekly oncall pages."
            _bullet(
                "exp-bolt-2",
                "Migrated from Cassandra to ScyllaDB, removing 12 oncall pages per week.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert result.warnings == [], f"Unexpected warnings: {result.warnings}"


# ---------- Invention is caught ----------


def test_invented_number_is_dropped(master, jd):
    # Source mentions 18 minutes → 4 minutes. The model invents a 95% improvement.
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet(
                "exp-acme-4",
                "Built a Kubernetes operator that improved deploy time by 95%.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "95" in result.warnings[0].reason
    assert "exp-acme-4" in result.dropped_bullet_ids
    # Section dropped because all its bullets failed
    assert result.cleaned.sections == []


def test_invented_magnitude_number_is_dropped(master, jd):
    """A fabricated number with a magnitude suffix ($99K, 50M, 200ms) must
    be caught — earlier regex bailed when a letter followed the digits.
    """
    # Source: "Built an internal Kubernetes operator that reduced deploy
    # times from 18 minutes to 4 minutes." — no $99K anywhere.
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet(
                "exp-acme-4",
                "Built a Kubernetes operator that saved $99K in deploy costs.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "99k" in result.warnings[0].reason.lower()
    assert "exp-acme-4" in result.dropped_bullet_ids


def test_magnitude_number_present_in_source_passes(master, jd):
    """Source already says '$40K/year' — rewrite with same '$40K' is honest, not fabrication."""
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet(
                "exp-acme-2",
                "Designed Postgres partitioning that saved $40K annually.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert result.warnings == []
    assert len(result.cleaned.sections[0].items[0].bullets) == 1


def test_magnitude_case_insensitive_match(master, jd):
    """Source has '12M daily requests' — rewriting as '12m' should pass (case-insensitive)."""
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet(
                "exp-acme-1",
                "Drove the dispatch rewrite handling 12m daily requests; cut p99 from 480ms to 95ms.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert result.warnings == []


def test_invented_proper_noun_is_dropped(master, jd):
    # Source bullet has nothing about Spanner. JD doesn't mention Spanner either.
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet(
                "exp-acme-2",
                "Designed Postgres and Spanner partitioning strategy for the events table.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "spanner" in result.warnings[0].reason.lower()
    assert "exp-acme-2" in result.dropped_bullet_ids


def test_unknown_source_id_is_dropped(master, jd):
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            _bullet("exp-acme-bogus-99", "Some completely fabricated bullet text."),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "not in master" in result.warnings[0].reason
    assert "exp-acme-bogus-99" in result.dropped_bullet_ids


def test_unknown_container_id_is_dropped(master, jd):
    item = TailoredItem(
        source_id="exp-fake-employer",
        bullets=[_bullet("exp-acme-1", "anything")],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "container" in result.warnings[0].reason.lower()
    assert result.cleaned.sections == []


# ---------- Mixed: keep good, drop bad ----------


def test_mixed_keeps_honest_drops_invented(master, jd):
    item = TailoredItem(
        source_id="exp-acme",
        bullets=[
            # Honest reword
            _bullet(
                "exp-acme-1",
                "Led the rewrite of the dispatch service from Ruby to Go, cutting p99 latency from 480ms to 95ms across 12M daily requests.",
            ),
            # Inventing a metric
            _bullet(
                "exp-acme-3",
                "Mentored 25 engineers and ran a formal coaching program.",  # source says 4
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd)
    assert len(result.warnings) == 1
    assert "exp-acme-3" in result.dropped_bullet_ids
    # Honest one survives
    surviving_ids = [
        b.source_id
        for s in result.cleaned.sections
        for it in s.items
        for b in it.bullets
    ]
    assert "exp-acme-1" in surviving_ids
    assert "exp-acme-3" not in surviving_ids


# ---------- Pointer must-include allows otherwise-foreign keywords ----------


def test_must_include_keyword_allows_proper_noun(master, jd):
    # Pretend the user forces "Datadog" via must-include even though source/JD don't mention it.
    pointers = Pointers(must_include=["Datadog"])
    item = TailoredItem(
        source_id="exp-bolt",
        bullets=[
            _bullet(
                "exp-bolt-3",
                "Wrote the company's first observability runbook with Datadog; adopted across 6 teams.",
            ),
        ],
    )
    result = validate(master, _wrap([item]), jd, pointers=pointers)
    assert result.warnings == [], f"Unexpected warnings: {result.warnings}"


# ---------- Summary fabrication is caught ----------


def test_summary_with_invented_number_falls_back(master, jd):
    tailored = TailoredResume(
        summary="Backend engineer with 25 years of experience.",  # master says 7
        sections=[],
    )
    result = validate(master, tailored, jd)
    # The "25" doesn't appear anywhere in the master, so the summary is rolled back to master's
    assert any("summary" in w.bullet_source_id for w in result.warnings)
    assert result.cleaned.summary == master.summary


def test_summary_passes_when_grounded(master, jd):
    tailored = TailoredResume(
        summary="Backend engineer with 7 years building distributed systems.",
        sections=[],
    )
    result = validate(master, tailored, jd)
    assert all("summary" not in w.bullet_source_id for w in result.warnings)
    assert result.cleaned.summary is not None
    assert "7 years" in result.cleaned.summary
