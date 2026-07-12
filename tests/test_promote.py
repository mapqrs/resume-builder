"""Tests for promote.promote_to_master + slugify + helpers."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from resume_builder.loaders import load_master
from resume_builder.promote import (
    PromoteResult,
    PromoteWarning,
    _slugify,
    promote_to_master,
)
from resume_builder.render import render_docx
from resume_builder.schema import (
    Award,
    Basics,
    Education,
    Master,
    Template,
)
from resume_builder.session_store import (
    BootstrapSession,
    ChunkEmployment,
    DraftAccomplishment,
    TimeChunk,
    new_session,
)


def _draft(
    *, id, chunk_id, bucket, bullet,
    raw_quote="raw notes here", followups=(), tags=(),
):
    return DraftAccomplishment(
        id=id, chunk_id=chunk_id,
        raw_quote=raw_quote, draft_bullet=bullet,
        tier="awesome", missing=[],
        user_confirmed=True,
        tags_hint=list(tags),
        bucket=bucket,
        user_followups=list(followups),
    )


def _session_with(
    *, basics=None, chunks=(), drafts=(), employment=(),
    education=(), summary=None,
):
    s = new_session()
    s.basics = basics
    s.chunks = list(chunks)
    s.drafts = list(drafts)
    s.employment = list(employment)
    s.education = list(education)
    s.summary = summary
    return s


# ---------- slugify ----------


def test_slugify_basic():
    assert _slugify("Acme Corp") == "acme-corp"


def test_slugify_collapses_punctuation():
    assert _slugify("Acme, Inc. (Bombay)") == "acme-inc-bombay"


def test_slugify_strips_diacritics():
    assert _slugify("São Paulo Café") == "sao-paulo-cafe"


def test_slugify_empty_returns_fallback():
    assert _slugify("") == "untitled"
    assert _slugify("!!!") == "untitled"


def test_slugify_handles_unicode_devanagari():
    """Devanagari has no ASCII equivalent — slug becomes 'untitled' rather than crashing."""
    out = _slugify("राज्य")
    assert out == "untitled"


# ---------- happy-path end-to-end round-trip ----------


def test_end_to_end_round_trip_produces_loadable_master(tmp_path):
    """Seed a session with drafts in every bucket; promote → load → render → no errors."""
    chunk = TimeChunk(
        id="chunk-2023-h1", label="H1 2023",
        start="2023-01", end="2023-07",
        raw_notes="shipped dispatch rewrite at Acme",
    )
    session = _session_with(
        basics=Basics(name="Test Person", email="t@example.com", phone="+91 98XXX 12345"),
        summary="Backend engineer with 6 years in distributed systems.",
        chunks=[chunk],
        employment=[ChunkEmployment(
            chunk_id="chunk-2023-h1",
            company="Acme", role="Senior Backend Engineer",
            location="Bengaluru",
        )],
        drafts=[
            _draft(id="d-1", chunk_id="chunk-2023-h1", bucket="experience",
                   bullet="Cut p99 latency by 80% by rewriting worker pool in Go",
                   tags=("backend", "go")),
            _draft(id="d-2", chunk_id="chunk-2023-h1", bucket="projects",
                   bullet="Built DispatchKit, an OSS scheduling library"),
            _draft(id="d-3", chunk_id="chunk-2023-h1", bucket="extracurricular",
                   bullet="Mentored 4 juniors via the company's grad programme"),
            _draft(id="d-4", chunk_id="chunk-2023-h1", bucket="awards",
                   bullet="Top engineer of the quarter, Q2 2023"),
            _draft(id="d-5", chunk_id="chunk-2023-h1", bucket="certifications",
                   bullet="AWS Solutions Architect — Associate",
                   tags=("AWS",)),
            _draft(id="d-6", chunk_id="chunk-2023-h1", bucket="skills",
                   bullet="Go, Python, PostgreSQL, Kafka"),
        ],
        education=[Education(
            id="e-1", school="IIT Bombay", degree="BTech CS",
            year="2018", status="graduated", gpa="CGPA 8.5/10",
        )],
    )

    result = promote_to_master(session)
    assert isinstance(result, PromoteResult)
    # Warnings empty because everything was set up cleanly.
    assert not any(w.kind in ("no_basics", "no_bucket", "missing_employment")
                   for w in result.warnings)

    master = result.master
    assert master.basics.name == "Test Person"
    assert master.summary.startswith("Backend engineer")
    assert len(master.experience) == 1
    assert master.experience[0].company == "Acme"
    assert master.experience[0].id == "exp-acme-1"
    assert len(master.experience[0].bullets) == 1
    assert master.experience[0].bullets[0].text.startswith("Cut p99 latency")
    assert len(master.projects) == 1
    assert len(master.extracurricular) == 1
    assert len(master.awards) == 1
    assert master.awards[0].name.startswith("Top engineer")
    assert len(master.skills) == 1
    # Education = base + certification
    assert len(master.education) == 2
    cert_entries = [e for e in master.education if e.status == "certification_only"]
    assert len(cert_entries) == 1

    # Write to disk and load back via the existing loaders.
    yaml_path = tmp_path / "master.yaml"
    yaml_path.write_text(
        yaml.safe_dump(master.model_dump(mode="json", exclude_none=True)),
        encoding="utf-8",
    )
    reloaded = load_master(yaml_path)
    assert reloaded.experience[0].id == "exp-acme-1"
    assert reloaded.awards[0].name.startswith("Top engineer")

    # Render to .docx without crashing.
    docx_path = tmp_path / "out.docx"
    render_docx(reloaded, Template(), docx_path)
    assert docx_path.exists()


# ---------- missing-data warnings ----------


def test_no_basics_warning_when_basics_missing():
    s = _session_with(basics=None, chunks=[], drafts=[])
    result = promote_to_master(s)
    assert any(w.kind == "no_basics" for w in result.warnings)
    # Even without basics we produce a usable Master with a placeholder name.
    assert result.master.basics.name


def test_missing_employment_warns_and_parks_under_placeholder():
    chunk = TimeChunk(id="c1", label="H1 2024", start="2024-01", end="2024-07")
    s = _session_with(
        basics=Basics(name="X"),
        chunks=[chunk],
        drafts=[_draft(id="d-1", chunk_id="c1", bucket="experience",
                       bullet="Cut latency by 50% by rewriting in Go")],
        # employment intentionally empty
    )
    result = promote_to_master(s)
    assert any(w.kind == "missing_employment" for w in result.warnings)
    assert len(result.master.experience) == 1
    assert "Independent" in result.master.experience[0].company or \
           "Various" in result.master.experience[0].company


def test_unbucketed_draft_warns_and_skips():
    s = _session_with(
        basics=Basics(name="X"),
        drafts=[DraftAccomplishment(
            id="d-1", chunk_id="c-1",
            raw_quote="x", draft_bullet="Some bullet",
            tier="better", missing=[], bucket=None,
        )],
    )
    result = promote_to_master(s)
    assert any(w.kind == "no_bucket" for w in result.warnings)
    assert result.master.experience == []
    assert result.master.projects == []


def test_award_without_raw_quote_warns():
    s = _session_with(
        basics=Basics(name="X"),
        drafts=[DraftAccomplishment(
            id="d-1", chunk_id="c-1",
            raw_quote="",  # empty — triggers the criteria warning
            draft_bullet="Some prize",
            tier="awesome", missing=[],
            user_confirmed=True, bucket="awards",
        )],
    )
    result = promote_to_master(s)
    assert any(w.kind == "award_no_criteria" for w in result.warnings)


def test_education_drafts_orphaned_when_no_education_entries():
    s = _session_with(
        basics=Basics(name="X"),
        drafts=[_draft(id="d-1", chunk_id="c-1", bucket="education",
                       bullet="Completed dissertation on X")],
    )
    result = promote_to_master(s)
    assert any(w.kind == "education_drafts_orphaned" for w in result.warnings)


# ---------- stable IDs + provenance ----------


def test_bullet_ids_are_stable_under_re_promote():
    """Same input should always produce the same IDs."""
    chunk = TimeChunk(id="c1", label="H1 2024", start="2024-01", end="2024-07")
    s = _session_with(
        basics=Basics(name="X"),
        chunks=[chunk],
        employment=[ChunkEmployment(chunk_id="c1", company="Acme", role="Eng")],
        drafts=[_draft(id="d-1", chunk_id="c1", bucket="experience", bullet="A"),
                _draft(id="d-2", chunk_id="c1", bucket="experience", bullet="B")],
    )
    a = promote_to_master(s).master
    b = promote_to_master(s).master
    a_ids = [bl.id for ex in a.experience for bl in ex.bullets]
    b_ids = [bl.id for ex in b.experience for bl in ex.bullets]
    assert a_ids == b_ids
    assert a_ids[0] == "exp-acme-1-bullet-1"


def test_bullet_variants_include_raw_quote_and_followups():
    """Promoted bullets carry raw_quote + user_followups in variants so the
    tailor's no-invention guard treats them as legal source vocabulary."""
    chunk = TimeChunk(id="c1", label="H1 2024", start="2024-01", end="2024-07")
    s = _session_with(
        basics=Basics(name="X"),
        chunks=[chunk],
        employment=[ChunkEmployment(chunk_id="c1", company="Acme", role="Eng")],
        drafts=[_draft(
            id="d-1", chunk_id="c1", bucket="experience",
            bullet="Cut p99 by 80% by rewriting in Go",
            raw_quote="p99 dropped from 480ms to 95ms",
            followups=["80%", "by rewriting in Go"],
        )],
    )
    master = promote_to_master(s).master
    bullet = master.experience[0].bullets[0]
    assert "p99 dropped from 480ms to 95ms" in bullet.variants
    assert "80%" in bullet.variants
    assert "by rewriting in Go" in bullet.variants


# ---------- backwards compat for the new Master fields ----------


def test_legacy_master_yaml_without_awards_and_extracurricular_still_loads(tmp_path):
    """Pre-Phase-6 master.yaml files (no awards/extracurricular) still load."""
    data = {
        "basics": {"name": "Old User"},
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
    }
    p = tmp_path / "legacy.yaml"
    p.write_text(yaml.safe_dump(data), encoding="utf-8")
    m = load_master(p)
    assert m.awards == []
    assert m.extracurricular == []


def test_master_with_new_fields_round_trips(tmp_path):
    m = Master(
        basics=Basics(name="X"),
        awards=[Award(name="ICPC", criteria="top 12 globally")],
        extracurricular=[],
    )
    p = tmp_path / "with-awards.yaml"
    p.write_text(yaml.safe_dump(m.model_dump(mode="json", exclude_none=True)),
                 encoding="utf-8")
    reloaded = load_master(p)
    assert reloaded.awards[0].name == "ICPC"
    assert reloaded.awards[0].criteria == "top 12 globally"


# ---------- chronological ordering ----------


def test_experience_sorted_most_recent_first():
    chunk1 = TimeChunk(id="c1", label="H1 2021", start="2021-01", end="2021-07")
    chunk2 = TimeChunk(id="c2", label="H1 2024", start="2024-01", end="2024-07")
    s = _session_with(
        basics=Basics(name="X"),
        chunks=[chunk1, chunk2],
        employment=[
            ChunkEmployment(chunk_id="c1", company="Older", role="X"),
            ChunkEmployment(chunk_id="c2", company="Newer", role="Y"),
        ],
        drafts=[
            _draft(id="d-1", chunk_id="c1", bucket="experience", bullet="A"),
            _draft(id="d-2", chunk_id="c2", bucket="experience", bullet="B"),
        ],
    )
    master = promote_to_master(s).master
    assert master.experience[0].company == "Newer"
    assert master.experience[1].company == "Older"
