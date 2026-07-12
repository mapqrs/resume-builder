"""Tests for the JD-less target-role mode (Phase 7).

Two layers:
1. Pure unit tests for ``from_target_role`` and ``target_role_to_jd_text``
   in ``jd_signals.py`` — deterministic synthesis, no LLM call.
2. End-to-end test: synthesize signals, run the existing tailor against a
   real master fixture using a fake LLM provider, validate the rendered
   .docx and ATS coverage report make sense.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resume_builder.ats import score_ats
from resume_builder.guard import validate
from resume_builder.jd_signals import (
    from_target_role,
    target_role_to_jd_text,
)
from resume_builder.llm import LLMProvider
from resume_builder.loaders import load_master
from resume_builder.render import render_docx
from resume_builder.schema import (
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
    TargetRole,
    Template,
)
from resume_builder.tailor import tailor_via_provider


FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def master():
    return load_master(FIXTURES / "sample-master.yaml")


# ---------- from_target_role: role-table matching ----------


def test_backend_role_picks_backend_archetype():
    t = TargetRole(role="Senior Backend Engineer")
    sig = from_target_role(t)
    assert sig.role_archetype == "backend"
    assert "distributed systems" in sig.top_keywords
    assert "API design" in sig.top_keywords


def test_data_engineer_beats_generic_engineer():
    """The table is order-sensitive — multi-word keys must match before
    the bare 'engineer' fallback."""
    t = TargetRole(role="Senior Data Engineer")
    sig = from_target_role(t)
    assert sig.role_archetype == "data"
    assert any("ETL" in kw or "etl" in kw.lower() for kw in sig.top_keywords)


def test_ml_engineer_picks_ml_archetype():
    t = TargetRole(role="ML Engineer")
    sig = from_target_role(t)
    assert sig.role_archetype == "ml"
    assert any("model" in kw.lower() for kw in sig.top_keywords)


def test_product_manager_no_archetype_but_has_keywords():
    t = TargetRole(role="Senior Product Manager")
    sig = from_target_role(t)
    assert sig.role_archetype is None
    assert "roadmap" in sig.top_keywords
    assert any("stakeholder" in s.lower() for s in sig.soft_skills)


def test_unknown_role_falls_back_to_generic_engineer():
    """A role string that doesn't match any specific entry but contains
    'engineer' lands on the generic-engineer fallback."""
    t = TargetRole(role="Quantum Computing Engineer")
    sig = from_target_role(t)
    assert sig.role_archetype is None  # generic-engineer fallback
    assert "software engineering" in sig.top_keywords


def test_completely_unmatched_role_still_returns_signals():
    """Even a totally unmatched role produces a usable JDSignals
    (no archetype, no table keywords, but still propagates user input)."""
    t = TargetRole(role="Tarot Card Reader", must_include=["empathy"])
    sig = from_target_role(t)
    assert sig.role_archetype is None
    assert sig.must_haves == ["empathy"]
    assert "empathy" in sig.top_keywords


# ---------- must_include + industry passthrough ----------


def test_must_include_lands_in_keywords_and_must_haves():
    t = TargetRole(
        role="Staff Backend Engineer",
        must_include=["Go", "Postgres", "Kubernetes"],
    )
    sig = from_target_role(t)
    assert sig.must_haves == ["Go", "Postgres", "Kubernetes"]
    # must_include appended after table keywords, deduped:
    for kw in ("Go", "Postgres", "Kubernetes"):
        assert kw in sig.top_keywords


def test_industry_lands_in_keywords():
    t = TargetRole(role="Backend Engineer", industry="fintech")
    sig = from_target_role(t)
    assert "fintech" in sig.top_keywords


def test_dedupes_overlap_between_table_and_must_include():
    """If the user lists a keyword the table already has, it stays exactly
    once in the top_keywords list."""
    t = TargetRole(role="Backend Engineer", must_include=["performance", "Go"])
    sig = from_target_role(t)
    # 'performance' is in the backend table; should appear once.
    assert sig.top_keywords.count("performance") == 1
    # Also no duplicate Go.
    assert sum(1 for k in sig.top_keywords if k.lower() == "go") == 1


# ---------- seniority + company-size lenses ----------


def test_staff_seniority_adds_leadership_signals():
    t = TargetRole(role="Backend Engineer", seniority="staff")
    sig = from_target_role(t)
    assert sig.inferred_seniority == "staff"
    soft_lower = [s.lower() for s in sig.soft_skills]
    assert "technical leadership" in soft_lower
    assert "mentorship" in soft_lower


def test_manager_seniority_adds_people_signals():
    t = TargetRole(role="Engineering Manager", seniority="manager")
    sig = from_target_role(t)
    soft_lower = [s.lower() for s in sig.soft_skills]
    assert any("team" in s for s in soft_lower) or any("hiring" in s for s in soft_lower)


def test_startup_company_size_adds_scope_signals():
    t = TargetRole(role="Backend Engineer", company_size="startup")
    sig = from_target_role(t)
    scope_lower = [s.lower() for s in sig.scope_signals]
    assert any("iteration" in s or "ownership" in s for s in scope_lower)


def test_faang_company_size_adds_scope_signals():
    t = TargetRole(role="Senior Engineer", company_size="faang")
    sig = from_target_role(t)
    scope_lower = [s.lower() for s in sig.scope_signals]
    assert any("scale" in s for s in scope_lower)


# ---------- target_role_to_jd_text ----------


def test_jd_text_includes_role_and_keywords():
    t = TargetRole(
        role="Staff Backend Engineer",
        seniority="staff",
        industry="fintech",
        must_include=["Go", "Postgres"],
    )
    text = target_role_to_jd_text(t)
    assert "Staff Backend Engineer" in text
    assert "fintech" in text
    assert "Go" in text
    assert "Postgres" in text
    # Marked clearly as synthesized so a reader doesn't mistake it for a real JD.
    assert "synthesized" in text.lower()


def test_jd_text_renders_with_minimal_input():
    t = TargetRole(role="Backend Engineer")
    text = target_role_to_jd_text(t)
    assert "Backend Engineer" in text
    # Doesn't crash; section headers absent gracefully.
    assert "Seniority" not in text
    assert "Industry" not in text


# ---------- end-to-end: tailor accepts synthesized signals ----------


class _FakeTailorProvider(LLMProvider):
    """Returns a scripted tailor response keyed off whatever master IDs the
    fixture has. Lets us exercise the full tailor → guard → render pipeline
    without an LLM."""

    name = "fake-tailor"

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


def test_end_to_end_target_role_produces_clean_docx(master, tmp_path):
    """Synthesize signals from a TargetRole → tailor with a fake LLM →
    guard passes → render to .docx. The whole chain works with no JD."""
    target = TargetRole(
        role="Staff Backend Engineer",
        seniority="staff",
        industry="fintech",
        must_include=["Go", "Postgres"],
    )
    jd_text = target_role_to_jd_text(target)
    signals = from_target_role(target)

    # Build a hand-crafted tailor response that references real master bullets
    # using only their source vocabulary — guaranteed to pass the guard.
    tailor_payload = {
        "summary": None,
        "sections": [
            {
                "name": "experience",
                "items": [
                    {
                        "source_id": "exp-acme",
                        "bullets": [
                            {
                                "source_id": "exp-acme-1",
                                "rewritten_text": (
                                    "Led the rewrite of the dispatch service from "
                                    "Ruby to Go, cutting p99 latency from 480ms to "
                                    "95ms across 12M daily requests."
                                ),
                            },
                            {
                                "source_id": "exp-acme-2",
                                "rewritten_text": (
                                    "Designed Postgres partitioning strategy for "
                                    "the events table, enabling retention policies "
                                    "that saved $40K/year in storage."
                                ),
                            },
                        ],
                    },
                ],
            },
        ],
        "dropped_source_ids": [],
        "rationale": "Surfaced Go + Postgres bullets per the target role.",
    }
    provider = _FakeTailorProvider(json.dumps(tailor_payload))
    pointers = Pointers(must_include=list(target.must_include))

    raw = tailor_via_provider(
        master, jd_text, pointers, provider, signals=signals,
    )
    assert raw.sections[0].name == "experience"
    assert raw.sections[0].items[0].source_id == "exp-acme"

    # Guard accepts everything because numbers + tools are grounded.
    guard = validate(master, raw, jd_text, pointers=pointers)
    assert guard.warnings == []

    # Render — verifies the docx pipeline survives a synthesized JD.
    out_path = tmp_path / "target-role.docx"
    render_docx(master, Template(), out_path, tailored=guard.cleaned)
    assert out_path.exists()
    assert out_path.stat().st_size > 1000  # sanity: not an empty file


def test_ats_score_against_synthetic_signals_makes_sense(master):
    """ATS coverage report computed against synthesized signals surfaces
    the must-include keywords the user supplied. If the synthesized signals
    are useless, the coverage report is uninformative."""
    target = TargetRole(
        role="Staff Backend Engineer",
        must_include=["Go", "Postgres"],
    )
    signals = from_target_role(target)
    pointers = Pointers(must_include=list(target.must_include))

    # Build a fake "rendered resume text" that includes one must-have term.
    resume_text = (
        "Senior Software Engineer at Acme Logistics. "
        "Led the Ruby-to-Go migration cutting p99 latency. "
        "Designed Postgres partitioning for the events table."
    )

    report = score_ats(resume_text, signals=signals, pointers=pointers)
    # The synthesized must-include "Go" + "Postgres" must show up in the
    # coverage tracking (whether covered or missing — we're checking the
    # report reasons about them, not that they all hit).
    summary = json.dumps(report.model_dump()).lower()
    assert "go" in summary
    assert "postgres" in summary


# ---------- web layer wiring ----------


@pytest.fixture
def web_client(tmp_path):
    """A Flask test client with the master output path redirected to tmp."""
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_analyze_target_role_endpoint_returns_signals(web_client):
    payload = {
        "role": "Staff Backend Engineer",
        "seniority": "staff",
        "must_include": ["Go", "Postgres"],
    }
    res = web_client.post(
        "/api/analyze-target-role",
        data={"target_role_json": json.dumps(payload)},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["inferred_seniority"] == "staff"
    assert body["role_archetype"] == "backend"
    assert "Go" in body["top_keywords"]


def test_index_exposes_no_jd_target_role_controls(web_client):
    """The documented no-JD mode needs actual controls on the home page."""
    res = web_client.get("/?skip-wizard=1")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert 'name="jd_mode" value="target"' in body
    assert 'id="target_role"' in body
    assert 'id="btn-analyze-target"' in body
    assert "target_role_json" in body


def test_analyze_target_role_rejects_missing_payload(web_client):
    res = web_client.post("/api/analyze-target-role", data={})
    assert res.status_code == 400


def test_analyze_target_role_rejects_invalid_json(web_client):
    res = web_client.post(
        "/api/analyze-target-role",
        data={"target_role_json": "not valid json"},
    )
    assert res.status_code == 400


def test_generate_rejects_when_neither_jd_nor_target(web_client):
    """The main /api/generate route rejects when neither input is set."""
    master_yaml = (FIXTURES / "sample-master.yaml").read_text()
    res = web_client.post("/api/generate", data={"master_yaml": master_yaml})
    assert res.status_code == 400
    body = res.get_json()
    assert "jd_text" in body["error"] or "target_role" in body["error"]
