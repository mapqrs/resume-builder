"""Tests for session_store: BootstrapSession, atomic write, chunk math."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from resume_builder.schema import Award, Education, TargetRole
from resume_builder.session_store import (
    BUCKETS,
    BootstrapSession,
    DraftAccomplishment,
    TimeChunk,
    chunk_size_months,
    default_chunks_for,
    delete_session,
    list_sessions,
    load,
    new_session,
    save,
)


# ---------- session round-trip ----------


def test_new_session_has_uuid_and_timestamps():
    s = new_session()
    assert isinstance(s.id, str) and len(s.id) >= 8
    assert s.created_at == s.updated_at
    assert s.created_at.endswith("Z")
    assert s.chunks == []
    assert s.drafts == []


def test_save_and_load_round_trip(tmp_path):
    s = new_session()
    s.career_start = "2020-01"
    s.chunks = [
        TimeChunk(id="chunk-2020-01", label="H1 2020",
                  start="2020-01", end="2020-07", raw_notes="shipped X"),
    ]
    s.drafts = [
        DraftAccomplishment(
            id="d1", chunk_id="chunk-2020-01",
            raw_quote="shipped X", draft_bullet="Shipped X by [METHOD]",
            tier="better", missing=["z_method"],
            tags_hint=["go", "backend"],
            impact_score_hint=4,
        ),
    ]
    s.education = [
        Education(
            id="edu-1", school="MIT", degree="BSc CS", year="2018",
            status="graduated", gpa="3.9",
            awards=[Award(name="Dean's List", criteria="top 10%")],
        ),
    ]
    s.target_role = TargetRole(role="Staff Backend Engineer", seniority="staff")
    s.notes = "scratch"

    path = save(s, sessions_root=tmp_path)
    assert path.exists()
    loaded = load(s.id, sessions_root=tmp_path)
    assert loaded.id == s.id
    assert loaded.career_start == "2020-01"
    assert len(loaded.chunks) == 1
    assert loaded.chunks[0].raw_notes == "shipped X"
    assert loaded.drafts[0].tier == "better"
    assert loaded.drafts[0].missing == ["z_method"]
    assert loaded.education[0].school == "MIT"
    assert loaded.education[0].awards[0].name == "Dean's List"
    assert loaded.target_role.role == "Staff Backend Engineer"


def test_save_is_atomic_no_partial_file(tmp_path):
    """After save, the .tmp file should not remain."""
    s = new_session()
    save(s, sessions_root=tmp_path)
    tmp_files = list((tmp_path / s.id).glob("*.tmp"))
    assert tmp_files == []


def test_save_updates_updated_at(tmp_path):
    s = new_session()
    original_updated = s.updated_at
    # Force the timestamp to differ by writing through ``save`` which calls touch().
    s.created_at = "2020-01-01T00:00:00Z"
    s.updated_at = "2020-01-01T00:00:00Z"
    save(s, sessions_root=tmp_path)
    assert s.updated_at != "2020-01-01T00:00:00Z"
    # Re-loaded session reflects the new timestamp.
    loaded = load(s.id, sessions_root=tmp_path)
    assert loaded.updated_at == s.updated_at


def test_load_missing_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load("nonexistent", sessions_root=tmp_path)


def test_list_sessions_empty_root(tmp_path):
    assert list_sessions(sessions_root=tmp_path) == []
    assert list_sessions(sessions_root=tmp_path / "missing") == []


def test_list_sessions_orders_newest_first(tmp_path):
    import yaml as _yaml

    # Write a stale session file directly so we can pin its updated_at —
    # save() always refreshes updated_at to now via touch().
    stale = new_session()
    stale_dir = tmp_path / stale.id
    stale_dir.mkdir(parents=True)
    payload = stale.model_dump(mode="json")
    payload["updated_at"] = "2020-01-01T00:00:00Z"
    (stale_dir / "state.yaml").write_text(_yaml.safe_dump(payload), encoding="utf-8")

    fresh = new_session()
    save(fresh, sessions_root=tmp_path)

    ids = list_sessions(sessions_root=tmp_path)
    assert ids[0] == fresh.id
    assert stale.id in ids


def test_delete_session_removes_dir(tmp_path):
    s = new_session()
    save(s, sessions_root=tmp_path)
    assert delete_session(s.id, sessions_root=tmp_path) is True
    assert not (tmp_path / s.id).exists()
    assert delete_session(s.id, sessions_root=tmp_path) is False


# ---------- chunk math ----------


def test_chunk_size_six_months_when_under_5y():
    today = datetime(2023, 6, 1, tzinfo=timezone.utc)
    assert chunk_size_months("2022-01", today=today) == 6
    assert chunk_size_months("2020-01", today=today) == 6


def test_chunk_size_twelve_months_when_5y_or_more():
    today = datetime(2025, 1, 1, tzinfo=timezone.utc)
    assert chunk_size_months("2020-01", today=today) == 12
    assert chunk_size_months("2010-01", today=today) == 12


def test_default_chunks_for_short_career(tmp_path):
    today = datetime(2023, 1, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2022-01", today=today)
    # 12 months / 6mo chunks = 2 chunks
    assert len(chunks) == 2
    assert chunks[0].label == "H1 2022"
    assert chunks[1].label == "H2 2022"
    assert chunks[0].start == "2022-01"
    assert chunks[0].end == "2022-07"
    assert chunks[1].end == "2023-01"


def test_default_chunks_for_long_career_uses_annual():
    today = datetime(2025, 6, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2015-01", today=today)
    # 10+ years should use annual; ~11 chunks
    assert all(c.label.isdigit() for c in chunks)
    assert chunks[0].label == "2015"
    assert chunks[-1].end == "2025-06"  # final chunk clamped to today


def test_default_chunks_truncates_final_chunk():
    today = datetime(2023, 4, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2022-01", today=today)
    assert chunks[-1].end == "2023-04"


def test_default_chunks_invalid_start():
    with pytest.raises(ValueError, match="YYYY-MM"):
        default_chunks_for("not-a-date")
    with pytest.raises(ValueError, match="YYYY-MM"):
        chunk_size_months("bad")


# ---------- constants ----------


def test_buckets_constant_is_the_seven_we_expect():
    assert set(BUCKETS) == {
        "experience", "projects", "education", "extracurricular",
        "skills", "awards", "certifications",
    }
