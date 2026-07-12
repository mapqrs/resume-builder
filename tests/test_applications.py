"""Applications tracker (roadmap): record/list/delete + the generation hook."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resume_builder import applications as A
from resume_builder.ats import ATSReport
from resume_builder.jd_signals import JDSignals
from resume_builder.schema import (
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------- label derivation ----------


def test_label_prefers_jd_title():
    sig = JDSignals(title="Staff Backend Engineer", role_archetype="backend")
    assert A._derive_label(sig, "irrelevant") == "Staff Backend Engineer"


def test_label_from_seniority_and_archetype():
    sig = JDSignals(inferred_seniority="staff", role_archetype="data-science")
    assert A._derive_label(sig, "") == "Staff Data Science"


def test_label_from_first_jd_line_when_no_signals():
    assert A._derive_label(None, "  Senior SRE, Fintech\nmore text") == "Senior SRE, Fintech"


def test_label_fallback_when_nothing_usable():
    assert A._derive_label(None, "\n\n   \n") == "Untitled application"


# ---------- record / load / delete ----------


def test_record_captures_ats_and_pointers(tmp_path):
    path = tmp_path / "applications.json"
    rep = ATSReport(score=0.82, matched=["go", "postgres"], missing=["kafka"], total_checked=3)
    app = A.record(
        path,
        signals=JDSignals(title="Backend Engineer"),
        pointers=Pointers(length="1page", seniority="senior", context="startup"),
        jd_text="We need a backend engineer.  Go, Postgres.",
        ats_report=rep,
        guard_dropped=2,
    )
    assert app.label == "Backend Engineer"
    assert app.ats_score == 0.82
    assert app.ats_matched == 2 and app.ats_total == 3
    assert app.length == "1page" and app.seniority == "senior" and app.context == "startup"
    assert app.guard_dropped == 2
    assert app.jd_snippet == "We need a backend engineer. Go, Postgres."  # ws collapsed
    assert A.load(path)[0].id == app.id


def test_record_without_ats_leaves_score_none(tmp_path):
    path = tmp_path / "applications.json"
    app = A.record(path, jd_text="Some JD text here")
    assert app.ats_score is None
    assert app.ats_matched == 0 and app.ats_total == 0


def test_load_newest_first_deterministic(tmp_path):
    path = tmp_path / "applications.json"
    A.record(path, jd_text="Older role\nx")
    A.record(path, jd_text="Newer role\ny")
    labels = [a.label for a in A.load(path)]
    assert labels == ["Newer role", "Older role"]


def test_record_caps_to_max(tmp_path, monkeypatch):
    monkeypatch.setattr(A, "MAX_RECORDS", 3)
    path = tmp_path / "applications.json"
    for i in range(6):
        A.record(path, jd_text=f"Role {i}\nbody")
    got = A.load(path)
    assert len(got) == 3
    # kept the most recent three (5, 4, 3)
    assert [a.label for a in got] == ["Role 5", "Role 4", "Role 3"]


def test_delete_removes_by_id(tmp_path):
    path = tmp_path / "applications.json"
    a1 = A.record(path, jd_text="Keep me\nx")
    a2 = A.record(path, jd_text="Delete me\ny")
    assert A.delete(a2.id, path) is True
    remaining = A.load(path)
    assert [a.id for a in remaining] == [a1.id]
    assert A.delete("does-not-exist", path) is False


def test_load_tolerates_missing_and_corrupt(tmp_path):
    assert A.load(tmp_path / "nope.json") == []
    bad = tmp_path / "applications.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert A.load(bad) == []
    bad.write_text('{"not": "a list"}', encoding="utf-8")
    assert A.load(bad) == []


# ---------- endpoints ----------


@pytest.fixture
def client(tmp_path):
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["APPLICATIONS_PATH"] = str(tmp_path / "applications.json")
    app.config["DELETE_MY_DATA_ROOT"] = str(tmp_path)
    app.config["WIZARD_SESSIONS_ROOT"] = str(tmp_path / "sessions")
    with app.test_client() as c:
        yield c, tmp_path


def test_api_applications_empty(client):
    c, _ = client
    assert c.get("/api/applications").get_json() == {"applications": []}


def test_api_applications_lists_and_deletes(client):
    c, tmp_path = client
    path = tmp_path / "applications.json"
    a1 = A.record(path, jd_text="First\nx")
    a2 = A.record(path, jd_text="Second\ny")

    body = c.get("/api/applications").get_json()["applications"]
    assert [a["label"] for a in body] == ["Second", "First"]

    assert c.delete(f"/api/applications/{a1.id}").status_code == 200
    left = c.get("/api/applications").get_json()["applications"]
    assert [a["id"] for a in left] == [a2.id]

    assert c.delete(f"/api/applications/{a1.id}").status_code == 404


def test_delete_my_data_wipes_applications(client):
    c, tmp_path = client
    path = tmp_path / "applications.json"
    A.record(path, jd_text="Something\nx")
    assert path.exists()
    res = c.post("/api/delete-my-data")
    assert res.status_code in (200, 207)
    assert not path.exists()
    assert str(path) in res.get_json()["removed_paths"]


def test_index_has_history_tab(client):
    c, tmp_path = client
    (tmp_path / "master.yaml").write_text("schema_version: 1\nbasics:\n  name: X\n", encoding="utf-8")
    import os
    cwd = os.getcwd()
    try:
        os.chdir(tmp_path)
        body = c.get("/?skip-wizard=1").get_data(as_text=True)
    finally:
        os.chdir(cwd)
    assert 'data-tab="history"' in body
    assert 'data-pane="history"' in body


# ---------- generation hook (end-to-end render → record) ----------


def _canned_tailored() -> TailoredResume:
    # Reuse verbatim source text so the no-invention guard keeps the bullet.
    return TailoredResume(
        summary=None,
        sections=[
            TailoredSection(
                name="experience",
                items=[
                    TailoredItem(
                        source_id="exp-acme",
                        bullets=[
                            TailoredBullet(
                                source_id="exp-acme-4",
                                rewritten_text=(
                                    "Built an internal Kubernetes operator that "
                                    "reduced deploy times from 18 minutes to 4 minutes."
                                ),
                            ),
                        ],
                    ),
                ],
            ),
        ],
        dropped_source_ids=[],
        rationale="test",
    )


def test_generate_records_application(client, monkeypatch):
    c, tmp_path = client
    monkeypatch.setattr(
        "resume_builder.web.tailor_auto",
        lambda *a, **k: (_canned_tailored(), "test-provider"),
    )
    master_yaml = (FIXTURES / "sample-master.yaml").read_text(encoding="utf-8")
    res = c.post("/api/generate", data={
        "master_yaml": master_yaml,
        "jd_text": "Kubernetes platform engineer. We run Go and Postgres at scale.",
        "length": "1page",
        "seniority": "senior",
    })
    assert res.status_code == 200
    assert res.headers.get("X-ATS-Report")  # ATS was scored

    apps = A.load(tmp_path / "applications.json")
    assert len(apps) == 1
    rec = apps[0]
    assert rec.label  # some non-empty label derived from the JD/signals
    assert rec.length == "1page" and rec.seniority == "senior"
    assert rec.ats_score is not None  # ATS captured into the log
    assert "Kubernetes platform engineer" in rec.jd_snippet
    assert rec.from_target_role is False
