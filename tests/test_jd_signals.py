"""Tests for the heuristic JD signal extractor."""

from __future__ import annotations

import textwrap

from resume_builder.jd_signals import extract


def _jd(text: str) -> str:
    return textwrap.dedent(text).strip()


def test_seniority_from_title():
    jd = _jd("""
        Senior Backend Engineer — Acme

        Acme is hiring. Help us scale.
    """)
    s = extract(jd)
    assert s.inferred_seniority == "senior"
    assert s.title is not None and "Senior Backend Engineer" in s.title


def test_seniority_staff_beats_senior_when_both_present():
    jd = _jd("""
        Staff Software Engineer, Distributed Systems

        We are looking for a senior contributor to join our staff-level team.
    """)
    s = extract(jd)
    assert s.inferred_seniority == "staff"


def test_archetype_backend():
    jd = _jd("""
        Senior Backend Engineer

        Build distributed systems, REST APIs, server-side services using Postgres.
    """)
    s = extract(jd)
    assert s.role_archetype == "backend"


def test_archetype_infra_via_keywords():
    jd = _jd("""
        Platform Engineer

        Own our Kubernetes infrastructure, Terraform modules, and observability stack.
    """)
    s = extract(jd)
    assert s.role_archetype == "infra"


def test_sections_split():
    jd = _jd("""
        Senior Software Engineer

        Responsibilities:
        - Build the dispatch service
        - Mentor 2 junior engineers

        Requirements:
        - 5+ years building distributed systems
        - Deep Postgres experience
        - Production Kubernetes ops

        Nice to have:
        - Kafka or other streaming systems
        - Open-source contributions
    """)
    s = extract(jd)
    assert len(s.responsibilities) == 2
    assert len(s.must_haves) == 3
    assert any("5+ years" in m for m in s.must_haves)
    assert len(s.nice_to_haves) == 2
    assert any("Kafka" in n for n in s.nice_to_haves)


def test_top_keywords_picks_tech_names():
    jd = _jd("""
        Backend Engineer

        Requirements:
        - Python or Go
        - Postgres
        - Kubernetes
        - Kafka
        - AWS

        We use Postgres heavily. Postgres knowledge is essential.
    """)
    s = extract(jd)
    # Postgres should top the list (frequency + bullet bonus)
    assert "Postgres" in s.top_keywords[:5]
    # Tech names present
    for kw in ["Python", "Go", "Kubernetes", "Kafka", "AWS"]:
        assert kw in s.top_keywords


def test_top_keywords_excludes_filler_capitals():
    jd = _jd("""
        Senior Engineer

        We are looking for a Senior Backend Engineer.
        You will work with the Team on the Platform.
    """)
    s = extract(jd)
    # These shouldn't show up as "top keywords"
    for bad in ["We", "You", "Senior", "Engineer", "Team", "Platform", "The"]:
        assert bad not in s.top_keywords


def test_years_required():
    jd = _jd("""
        Senior Engineer

        Requirements:
        - 5+ years of backend experience
        - 3 years of Python preferred
    """)
    s = extract(jd)
    # Smallest = the floor (3 years here)
    assert s.years_required == 3


def test_soft_skills_extracted():
    jd = _jd("""
        Senior Engineer

        Strong written communication.
        Comfort mentoring junior engineers and working cross-functionally with stakeholders.
        We value ownership.
    """)
    s = extract(jd)
    skills = set(s.soft_skills)
    assert "written communication" in skills
    assert "mentorship" in skills
    assert "cross-functional" in skills
    assert "stakeholder management" in skills
    assert "ownership" in skills


def test_scope_signals_extracted():
    jd = _jd("""
        Engineer

        Our platform handles 100M users and processes billions of events
        across a multi-region deployment.
    """)
    s = extract(jd)
    joined = " ".join(s.scope_signals).lower()
    assert "100m users" in joined
    assert any("billions of" in p.lower() for p in s.scope_signals)
    assert any("multi-region" in p.lower() for p in s.scope_signals)


def test_unscoped_jd_falls_back_to_must_have():
    """A JD without explicit section headers — every bullet is a 'must have' candidate."""
    jd = _jd("""
        Engineer

        - Python expertise
        - Postgres expertise
        - Kubernetes expertise
    """)
    s = extract(jd)
    assert len(s.must_haves) == 3


def test_empty_jd_returns_empty_signals():
    s = extract("")
    assert s.must_haves == []
    assert s.top_keywords == []
    assert s.inferred_seniority is None


def test_for_prompt_drops_none_keeps_empty_lists():
    """for_prompt should be compact: drop None fields, but keep present-but-empty
    lists so the LLM knows we looked and found nothing.
    """
    jd = _jd("Just a Job Title\n\nNo structure here.")
    s = extract(jd)
    payload = s.for_prompt()
    # title is present (it found one), seniority/archetype are None and should drop
    assert "title" in payload
    # Empty lists are kept (the heuristic ran; absence is informative)
    assert "must_haves" in payload
