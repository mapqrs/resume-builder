"""End-to-end tests for the wizard blueprint via Flask's test client."""

from __future__ import annotations

import io
from pathlib import Path

import pytest

from resume_builder.session_store import load


@pytest.fixture
def client(tmp_path):
    """Test client with sessions root pinned to a temp directory."""
    from resume_builder.web import app

    app.config["TESTING"] = True
    app.config["WIZARD_SESSIONS_ROOT"] = str(tmp_path)
    with app.test_client() as c:
        yield c


# ---------- create / get / patch ----------


def test_create_returns_new_session_payload(client, tmp_path):
    res = client.post("/api/wizard")
    assert res.status_code == 201
    data = res.get_json()
    assert data["id"]
    assert data["role_family"] is None
    assert data["chunks"] == []
    assert "prompts" in data
    assert "role_families" in data
    assert isinstance(data["role_families"], list)
    assert len(data["role_families"]) >= 10

    # The session is persisted to disk.
    state_file = Path(tmp_path) / data["id"] / "state.yaml"
    assert state_file.exists()


def test_get_returns_404_for_unknown(client):
    res = client.get("/api/wizard/does-not-exist")
    assert res.status_code == 404


def test_patch_role_family_and_get_back_role_specific_prompts(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(
        f"/api/wizard/{sid}",
        json={"role_family": "software-engineering"},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["role_family"] == "software-engineering"
    # Prompts list should now include role-specific additions.
    assert any("latency" in p.lower() or "service" in p.lower()
               for p in data["prompts"])


def test_patch_rejects_unknown_role_family(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(
        f"/api/wizard/{sid}",
        json={"role_family": "wizard-of-oz"},
    )
    assert res.status_code == 400


def test_patch_career_start_shape_check(client):
    sid = client.post("/api/wizard").get_json()["id"]
    bad = client.patch(f"/api/wizard/{sid}", json={"career_start": "not-a-date"})
    assert bad.status_code == 400
    ok = client.patch(f"/api/wizard/{sid}", json={"career_start": "2020-01"})
    assert ok.status_code == 200
    assert ok.get_json()["career_start"] == "2020-01"


def test_patch_chunks_replaces_full_list(client):
    sid = client.post("/api/wizard").get_json()["id"]
    new_chunks = [
        {"id": "c-1", "label": "Stretch 1",
         "start": "2022-01", "end": "2022-07", "raw_notes": "shipped X"},
        {"id": "c-2", "label": "Stretch 2",
         "start": "2022-07", "end": "2023-01", "raw_notes": "shipped Y"},
    ]
    res = client.patch(f"/api/wizard/{sid}", json={"chunks": new_chunks})
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["chunks"]) == 2
    assert data["chunks"][0]["raw_notes"] == "shipped X"


def test_patch_chunks_rejects_invalid_shape(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(f"/api/wizard/{sid}", json={"chunks": [{"label": "no id"}]})
    assert res.status_code == 400


def test_patch_unknown_keys_are_ignored(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(f"/api/wizard/{sid}", json={"nonsense_field": "x"})
    assert res.status_code == 200


# ---------- regenerate ----------


def test_regenerate_requires_career_start(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.post(f"/api/wizard/{sid}/regenerate-chunks")
    assert res.status_code == 400


def test_regenerate_produces_chunks(client):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"career_start": "2020-01"})
    res = client.post(f"/api/wizard/{sid}/regenerate-chunks")
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["chunks"]) >= 2


def test_regenerate_preserves_existing_raw_notes(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"career_start": "2020-01"})
    first = client.post(f"/api/wizard/{sid}/regenerate-chunks").get_json()

    # Type into the first chunk.
    chunks = first["chunks"]
    chunks[0]["raw_notes"] = "remember: shipped the dispatch rewrite"
    client.patch(f"/api/wizard/{sid}", json={"chunks": chunks})

    # Regenerate again; the user's note must survive (same chunk id).
    second = client.post(f"/api/wizard/{sid}/regenerate-chunks").get_json()
    target = next(c for c in second["chunks"] if c["id"] == chunks[0]["id"])
    assert target["raw_notes"] == "remember: shipped the dispatch rewrite"


# ---------- import-resume ----------


def test_import_resume_returns_extracted_text(client):
    sid = client.post("/api/wizard").get_json()["id"]
    payload = b"Plain text fixture used to seed the chunk."
    res = client.post(
        f"/api/wizard/{sid}/import-resume",
        data={"file": (io.BytesIO(payload), "linkedin.txt")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 200
    data = res.get_json()
    assert "Plain text fixture" in data["text"]
    assert data["filename"] == "linkedin.txt"


def test_import_resume_rejects_missing_file(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.post(f"/api/wizard/{sid}/import-resume", data={})
    assert res.status_code == 400


def test_import_resume_unknown_session_returns_404(client):
    res = client.post(
        "/api/wizard/bogus-id/import-resume",
        data={"file": (io.BytesIO(b"text"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert res.status_code == 404


# ---------- UI page ----------


def test_wizard_page_renders(client):
    res = client.get("/wizard")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # Smoke test: each role-family label should appear in the rendered HTML.
    assert "Software Engineering" in body
    assert "Other" in body
    assert "Build my master" in body


def test_index_links_to_wizard(client):
    # Phase 9: / redirects to /wizard when no master.yaml exists in cwd.
    # This test verifies the page content, so override with ?skip-wizard=1.
    res = client.get("/?skip-wizard=1")
    assert res.status_code == 200
    assert "/wizard" in res.get_data(as_text=True)


# ---------- voice typing (Phase 1.5) ----------


def test_wizard_loads_voice_input_script(client):
    res = client.get("/wizard")
    body = res.get_data(as_text=True)
    assert "voice_input.js" in body


def test_wizard_has_mic_button_with_aria_label(client):
    res = client.get("/wizard")
    body = res.get_data(as_text=True)
    assert 'id="btn-voice"' in body
    assert 'aria-label="Start dictation"' in body


def test_wizard_has_aria_live_transcript_region(client):
    res = client.get("/wizard")
    body = res.get_data(as_text=True)
    assert 'id="voice-live"' in body
    assert 'aria-live="polite"' in body


def test_wizard_has_privacy_modal_with_disclosure_text(client):
    res = client.get("/wizard")
    body = res.get_data(as_text=True)
    assert 'id="voice-privacy-dialog"' in body
    # Both speech-engine vendors must be named — that's the disclosure.
    assert "Google" in body
    assert "Apple" in body
    assert "OK, got it" in body


def test_wizard_voice_input_js_served(client):
    res = client.get("/static/voice_input.js")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "class VoiceInput" in body
    assert "isAvailable" in body


# ---------- extract endpoint (Phase 2) ----------


import json as _json
from resume_builder import wizard as _wizard_module
from resume_builder.session_store import DraftAccomplishment


def _fake_provider_returning(response_text):
    """A throwaway provider used to short-circuit pick_provider()."""
    from resume_builder.llm import LLMProvider, ProviderChoice

    class _Fake(LLMProvider):
        name = "fake"

        @classmethod
        def is_available(cls):
            return True

        def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
            return response_text

    return ProviderChoice(_Fake(), "fake provider for tests")


def _seed_chunk_with_notes(client, notes):
    """Helper: create a session, set role + career + chunk, return ids."""
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={
        "role_family": "software-engineering",
        "career_start": "2024-01",
    })
    client.post(f"/api/wizard/{sid}/regenerate-chunks")
    sess = client.get(f"/api/wizard/{sid}").get_json()
    chunk = sess["chunks"][0]
    chunk["raw_notes"] = notes
    client.patch(f"/api/wizard/{sid}", json={"chunks": sess["chunks"]})
    return sid, chunk["id"]


_EXTRACT_RESPONSE = _json.dumps({
    "drafts": [
        {
            "raw_quote": "shipped dispatch service rewrite from Ruby to Go",
            "draft_bullet": "Led migration of dispatch service from Ruby to Go by [METHOD]",
            "impact_score_hint": 5,
            "tags_hint": ["backend", "go"],
        },
    ],
})


def test_extract_returns_drafts(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_EXTRACT_RESPONSE),
    )
    notes = (
        "shipped dispatch service rewrite from Ruby to Go in Q1; "
        "led 3-person team; mentored 2 juniors; ran on-call rotation."
    )
    sid, cid = _seed_chunk_with_notes(client, notes)
    res = client.post(f"/api/wizard/{sid}/chunks/{cid}/extract")
    assert res.status_code == 200
    data = res.get_json()
    assert data["extracted_count"] == 1
    assert data["drafts"][0]["raw_quote"].startswith("shipped dispatch")
    assert data["llm_call"]["system_prompt"]
    assert data["llm_call"]["raw_response"] == _EXTRACT_RESPONSE
    assert data["provider"]["name"] == "fake"


def test_extract_too_short_returns_422(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_EXTRACT_RESPONSE),
    )
    sid, cid = _seed_chunk_with_notes(client, "tiny")
    res = client.post(f"/api/wizard/{sid}/chunks/{cid}/extract")
    assert res.status_code == 422
    data = res.get_json()
    assert data["error"] == "chunk_too_short"
    assert data["min_chars"] >= 80


def test_extract_warns_then_replaces_preserving_confirmed(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_EXTRACT_RESPONSE),
    )
    notes = (
        "shipped dispatch service rewrite from Ruby to Go in Q1; "
        "led 3-person team; mentored 2 juniors; ran on-call rotation."
    )
    sid, cid = _seed_chunk_with_notes(client, notes)

    first = client.post(f"/api/wizard/{sid}/chunks/{cid}/extract").get_json()
    # Confirm the first draft.
    draft = first["drafts"][0]
    draft["user_confirmed"] = True
    client.patch(f"/api/wizard/{sid}", json={"drafts": [draft]})

    # Second call without `replace`: should warn (409).
    res2 = client.post(f"/api/wizard/{sid}/chunks/{cid}/extract")
    assert res2.status_code == 409
    body2 = res2.get_json()
    assert body2["error"] == "drafts_exist"
    assert body2["existing_count"] == 1
    assert body2["confirmed_count"] == 1

    # Second call with `replace=true`: confirmed draft survives, new one appended.
    res3 = client.post(
        f"/api/wizard/{sid}/chunks/{cid}/extract",
        json={"replace": True},
    )
    assert res3.status_code == 200
    body3 = res3.get_json()
    ids = [d["id"] for d in body3["drafts"]]
    assert draft["id"] in ids  # confirmed preserved
    assert len(body3["drafts"]) >= 2


def test_extract_unknown_chunk_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_EXTRACT_RESPONSE),
    )
    sid, _ = _seed_chunk_with_notes(client, "x" * 120)
    res = client.post(f"/api/wizard/{sid}/chunks/nope/extract")
    assert res.status_code == 404


def test_extract_unknown_session_returns_404(client):
    res = client.post("/api/wizard/missing-sid/chunks/c-1/extract")
    assert res.status_code == 404


def test_patch_accepts_drafts_array(client):
    sid = client.post("/api/wizard").get_json()["id"]
    drafts = [
        DraftAccomplishment(
            id="d-1", chunk_id="c-1",
            raw_quote="x", draft_bullet="Y",
        ).model_dump(mode="json"),
    ]
    res = client.patch(f"/api/wizard/{sid}", json={"drafts": drafts})
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["drafts"]) == 1
    assert data["drafts"][0]["id"] == "d-1"


def test_patch_drafts_rejects_invalid_shape(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(f"/api/wizard/{sid}", json={"drafts": [{"id": "d-1"}]})
    assert res.status_code == 400


# ---------- categorize + merge (Phase 3) ----------


def _seed_drafts(client, drafts_payload):
    """Helper: create a session and patch in a list of drafts."""
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"drafts": drafts_payload})
    return sid


def _draft_payload(id, bullet, *, bucket=None, confirmed=False):
    return {
        "id": id, "chunk_id": "c-1",
        "raw_quote": "quote", "draft_bullet": bullet,
        "tier": "awesome", "missing": [],
        "tags_hint": [], "user_followups": [],
        "where_to_look": [],
        "bucket": bucket,
        "user_confirmed": confirmed,
    }


_CATEGORIZE_RESPONSE = _json.dumps({
    "assignments": [
        {"draft_id": "d-1", "bucket": "experience", "confidence": 5, "rationale": "paid role"},
        {"draft_id": "d-2", "bucket": "projects", "confidence": 4, "rationale": "side project"},
    ],
})


def test_categorize_assigns_unbucketed_drafts(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_CATEGORIZE_RESPONSE),
    )
    sid = _seed_drafts(client, [
        _draft_payload("d-1", "Led migration of dispatch service"),
        _draft_payload("d-2", "Built personal blog generator"),
    ])
    res = client.post(f"/api/wizard/{sid}/categorize")
    assert res.status_code == 200
    data = res.get_json()
    assert data["assigned_count"] == 2
    by_id = {d["id"]: d for d in data["session"]["drafts"]}
    assert by_id["d-1"]["bucket"] == "experience"
    assert by_id["d-2"]["bucket"] == "projects"
    assert data["rationales"]["d-1"].lower().startswith("paid")


def test_categorize_skips_already_bucketed(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_CATEGORIZE_RESPONSE),
    )
    sid = _seed_drafts(client, [
        _draft_payload("d-1", "x", bucket="experience"),
        _draft_payload("d-2", "y", bucket="projects"),
    ])
    res = client.post(f"/api/wizard/{sid}/categorize")
    assert res.status_code == 200
    data = res.get_json()
    assert data["assigned_count"] == 0
    assert data["llm_call"] is None
    assert "already" in data["hint"].lower()


def test_categorize_empty_session(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_CATEGORIZE_RESPONSE),
    )
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.post(f"/api/wizard/{sid}/categorize")
    assert res.status_code == 200
    data = res.get_json()
    assert data["assigned_count"] == 0
    assert "extract" in data["hint"].lower()


def test_merge_two_drafts_replaces_originals(client):
    sid = _seed_drafts(client, [
        _draft_payload("d-1", "Shipped dispatch rewrite"),
        _draft_payload("d-2", "Cut p99 latency"),
    ])
    res = client.post(
        f"/api/wizard/{sid}/drafts/d-1/merge",
        json={"with": "d-2"},
    )
    assert res.status_code == 200
    data = res.get_json()
    ids = {d["id"] for d in data["session"]["drafts"]}
    assert "d-1" not in ids
    assert "d-2" not in ids
    assert data["merged_draft"]["id"] in ids
    assert "Shipped dispatch rewrite" in data["merged_draft"]["draft_bullet"]
    assert "Cut p99 latency" in data["merged_draft"]["draft_bullet"]


def test_merge_rejects_self_merge(client):
    sid = _seed_drafts(client, [_draft_payload("d-1", "x")])
    res = client.post(
        f"/api/wizard/{sid}/drafts/d-1/merge",
        json={"with": "d-1"},
    )
    assert res.status_code == 400


def test_merge_requires_with_field(client):
    sid = _seed_drafts(client, [_draft_payload("d-1", "x")])
    res = client.post(f"/api/wizard/{sid}/drafts/d-1/merge", json={})
    assert res.status_code == 400


def test_merge_unknown_draft_returns_404(client):
    sid = _seed_drafts(client, [_draft_payload("d-1", "x")])
    res = client.post(
        f"/api/wizard/{sid}/drafts/d-1/merge",
        json={"with": "d-missing"},
    )
    assert res.status_code == 404


# ---------- education PATCH (Phase 4) ----------


def test_patch_accepts_education_array(client):
    sid = client.post("/api/wizard").get_json()["id"]
    edu = [{
        "id": "e1", "school": "IIT Bombay", "degree": "BSc CS",
        "year": "2020", "status": "graduated",
        "gpa": "CGPA 9.2/10",
        "awards": [{"name": "Dean's List", "criteria": "top 10%", "year": "2020"}],
    }]
    res = client.patch(f"/api/wizard/{sid}", json={"education": edu})
    assert res.status_code == 200
    data = res.get_json()
    assert len(data["education"]) == 1
    assert data["education"][0]["status"] == "graduated"
    assert data["education"][0]["gpa"] == "CGPA 9.2/10"
    assert data["education"][0]["awards"][0]["criteria"] == "top 10%"


def test_patch_education_accepts_each_status(client):
    sid = client.post("/api/wizard").get_json()["id"]
    statuses = [
        "graduated", "in_progress", "dropout", "deferred_admit",
        "rejected_admit", "on_leave", "certification_only", "online_only",
    ]
    edu = [
        {"id": f"e-{s}", "school": "X", "degree": "Y", "year": "2020", "status": s}
        for s in statuses
    ]
    res = client.patch(f"/api/wizard/{sid}", json={"education": edu})
    assert res.status_code == 200
    got = res.get_json()["education"]
    assert {e["status"] for e in got} == set(statuses)


def test_patch_education_rejects_unknown_status(client):
    sid = client.post("/api/wizard").get_json()["id"]
    bad = [{"id": "e1", "school": "x", "degree": "y", "year": "2020", "status": "party-school"}]
    res = client.patch(f"/api/wizard/{sid}", json={"education": bad})
    assert res.status_code == 400


def test_patch_education_rejects_non_array(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(f"/api/wizard/{sid}", json={"education": "nope"})
    assert res.status_code == 400


# ---------- polish endpoint (Phase 5) ----------


_POLISH_RESPONSE = _json.dumps({
    "polished_bullet": "Cut p99 latency by 80% by rewriting the worker pool in Go",
    "rationale": "Substituted the user's metric and method.",
})


def _seed_session_with_draft(client, draft_overrides=None):
    """Helper: create a session + one chunk + one draft. Returns (sid, draft_id)."""
    sid = client.post("/api/wizard").get_json()["id"]
    payload = {
        "id": "d-test", "chunk_id": "c-1",
        "raw_quote": "shipped dispatch rewrite; p99 dropped from 480ms to 95ms",
        "draft_bullet": "Cut p99 latency by [NUMBER]% by [METHOD]",
        "tier": "better", "missing": ["y_metric", "z_method"],
        "user_confirmed": False,
        "user_followups": [],
    }
    if draft_overrides:
        payload.update(draft_overrides)
    client.patch(f"/api/wizard/{sid}", json={"drafts": [payload]})
    return sid, payload["id"]


def test_polish_happy_path(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_POLISH_RESPONSE),
    )
    sid, did = _seed_session_with_draft(client)
    res = client.post(
        f"/api/wizard/{sid}/drafts/{did}/polish",
        json={"followups": {
            "y_metric": "80%",
            "z_method": "by rewriting the worker pool in Go",
        }},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["draft"]["draft_bullet"].startswith("Cut p99 latency by 80%")
    assert data["fabrication_warnings"] == []
    assert data["llm_call"]["raw_response"] == _POLISH_RESPONSE


def test_polish_unknown_draft_returns_404(client, monkeypatch):
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(_POLISH_RESPONSE),
    )
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.post(
        f"/api/wizard/{sid}/drafts/missing/polish",
        json={"followups": {"y_metric": "80%"}},
    )
    assert res.status_code == 404


def test_polish_unknown_session_returns_404(client):
    res = client.post(
        "/api/wizard/bogus/drafts/d-1/polish",
        json={"followups": {}},
    )
    assert res.status_code == 404


def test_polish_rejects_non_object_followups(client):
    sid, did = _seed_session_with_draft(client)
    res = client.post(
        f"/api/wizard/{sid}/drafts/{did}/polish",
        json={"followups": "not an object"},
    )
    assert res.status_code == 400


def test_polish_surfaces_fabrication_warnings(client, monkeypatch):
    """Adversarial: LLM tries to invent '12M' that wasn't in the raw_quote."""
    bad = _json.dumps({
        "polished_bullet": "Cut p99 latency by 80% across 12M daily requests by rewriting in Go",
        "rationale": "polished",
    })
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _fake_provider_returning(bad),
    )
    sid, did = _seed_session_with_draft(client)
    res = client.post(
        f"/api/wizard/{sid}/drafts/{did}/polish",
        json={"followups": {
            "y_metric": "80%",
            "z_method": "by rewriting in Go",
        }},
    )
    assert res.status_code == 200
    data = res.get_json()
    assert data["fabrication_warnings"]
    assert any("12m" in w.lower() for w in data["fabrication_warnings"])


def test_where_to_look_endpoint(client):
    res = client.get("/api/wizard/where-to-look")
    assert res.status_code == 200
    data = res.get_json()
    assert "y_metric" in data
    assert "z_method" in data
    assert "x_strong_verb" in data
    assert len(data["y_metric"]) >= 3


def test_patch_education_persists_reason_field(client):
    """Phase 4.5: position-of-strength `reason` round-trips through PATCH."""
    sid = client.post("/api/wizard").get_json()["id"]
    edu = [{
        "id": "e1", "school": "IIT Bombay", "degree": "BTech CS",
        "year": "2016-2019", "status": "dropout",
        "reason": "Left to co-found Acme — acquired 2022",
    }]
    res = client.patch(f"/api/wizard/{sid}", json={"education": edu})
    assert res.status_code == 200
    data = res.get_json()
    assert data["education"][0]["reason"] == "Left to co-found Acme — acquired 2022"


# ---------- Phase 6: basics + employment PATCH + promote endpoints ----------


def test_patch_accepts_basics(client):
    sid = client.post("/api/wizard").get_json()["id"]
    basics = {
        "name": "Test Person", "email": "t@example.com",
        "phone": "+91 98XXX 12345", "location": "Bengaluru",
        "links": [{"label": "LinkedIn", "url": "https://linkedin.com/in/x"}],
    }
    res = client.patch(f"/api/wizard/{sid}", json={"basics": basics})
    assert res.status_code == 200
    data = res.get_json()
    assert data["basics"]["name"] == "Test Person"
    assert data["basics"]["links"][0]["label"] == "LinkedIn"


def test_patch_basics_rejects_invalid_shape(client):
    sid = client.post("/api/wizard").get_json()["id"]
    # Missing required `name` → pydantic validation fires.
    res = client.patch(f"/api/wizard/{sid}", json={"basics": {"email": "x@x"}})
    assert res.status_code == 400


def test_patch_accepts_employment(client):
    sid = client.post("/api/wizard").get_json()["id"]
    employment = [{
        "chunk_id": "c-1", "company": "Acme", "role": "Engineer",
        "location": "Bengaluru", "start_override": None, "end_override": None,
    }]
    res = client.patch(f"/api/wizard/{sid}", json={"employment": employment})
    assert res.status_code == 200
    assert res.get_json()["employment"][0]["company"] == "Acme"


def test_patch_summary(client):
    sid = client.post("/api/wizard").get_json()["id"]
    res = client.patch(f"/api/wizard/{sid}", json={"summary": "Backend engineer · 6 years"})
    assert res.status_code == 200
    assert res.get_json()["summary"].startswith("Backend engineer")


def test_promote_preview_returns_yaml_and_warnings(client):
    sid = client.post("/api/wizard").get_json()["id"]
    # Set just enough basics so the no_basics warning goes away.
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test"}})
    res = client.post(f"/api/wizard/{sid}/promote-preview")
    assert res.status_code == 200
    data = res.get_json()
    assert "yaml" in data and "basics:" in data["yaml"]
    assert "warnings" in data
    # An empty session shouldn't error out; warnings list might still be empty.
    assert isinstance(data["warnings"], list)


def test_promote_save_writes_master_yaml(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test"}})
    # Point the wizard at a temp dir so we don't touch the real master.yaml.
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    res = client.post(f"/api/wizard/{sid}/promote-save", json={})
    assert res.status_code == 200
    data = res.get_json()
    assert "saved_path" in data
    written = tmp_path / "master.yaml"
    assert written.exists()
    assert "basics:" in written.read_text(encoding="utf-8")
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


def test_promote_save_backs_up_existing_master(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test"}})
    from resume_builder.web import app
    target = tmp_path / "master.yaml"
    target.write_text("# pre-existing content\n", encoding="utf-8")
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(target)
    res = client.post(f"/api/wizard/{sid}/promote-save", json={})
    assert res.status_code == 200
    data = res.get_json()
    assert data["backup_path"]
    assert Path(data["backup_path"]).exists()
    assert "pre-existing content" in Path(data["backup_path"]).read_text(encoding="utf-8")
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


def test_promote_save_accepts_user_edited_yaml(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Original"}})
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    custom = """\
basics:
  name: User Edited
experience: []
projects: []
education: []
skills: []
"""
    res = client.post(f"/api/wizard/{sid}/promote-save", json={"yaml": custom})
    assert res.status_code == 200
    on_disk = (tmp_path / "master.yaml").read_text(encoding="utf-8")
    assert "User Edited" in on_disk
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


def test_promote_save_rejects_invalid_yaml(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    res = client.post(
        f"/api/wizard/{sid}/promote-save",
        json={"yaml": "this: is not [valid yaml"},
    )
    assert res.status_code == 400
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


def test_promote_save_records_path_on_session(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test"}})
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    res = client.post(f"/api/wizard/{sid}/promote-save", json={})
    assert res.status_code == 200
    fresh = client.get(f"/api/wizard/{sid}").get_json()
    assert fresh["promoted_master_path"] == str(tmp_path / "master.yaml")
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


# ---------- persistence ----------


def test_full_round_trip_persists_to_disk(client, tmp_path):
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={
        "role_family": "design",
        "career_start": "2021-06",
        "notes": "starting fresh",
    })
    # Read directly from disk via session_store to prove the path on-wire
    # matches the persistence layer.
    loaded = load(sid, sessions_root=tmp_path)
    assert loaded.role_family == "design"
    assert loaded.career_start == "2021-06"
    assert loaded.notes == "starting fresh"


# ---------- Phase 6.5: LinkedIn endpoint ----------


def _stub_linkedin_response(headline="Senior Backend Engineer",
                            about="I'm a backend engineer with experience.",
                            entries=None, items=None):
    """Map (system_prompt_substring -> response_text) so a single fake provider
    can answer all 4 section calls the LinkedIn builder makes.
    """
    from resume_builder.llm import LLMProvider, ProviderChoice
    from resume_builder.linkedin_builder import (
        HEADLINE_SYSTEM_PROMPT, ABOUT_SYSTEM_PROMPT,
        EXPERIENCE_SYSTEM_PROMPT, FEATURED_SYSTEM_PROMPT,
    )

    class _Fake(LLMProvider):
        name = "fake"

        @classmethod
        def is_available(cls):
            return True

        def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
            if system_prompt is HEADLINE_SYSTEM_PROMPT:
                return _json.dumps({"headline": headline})
            if system_prompt is ABOUT_SYSTEM_PROMPT:
                return _json.dumps({"about": about})
            if system_prompt is EXPERIENCE_SYSTEM_PROMPT:
                return _json.dumps({"entries": entries or []})
            if system_prompt is FEATURED_SYSTEM_PROMPT:
                return _json.dumps({"items": items or []})
            raise AssertionError("unexpected system prompt")

    return ProviderChoice(_Fake(), "fake provider for tests")


def test_linkedin_endpoint_returns_profile_and_plain_text(client, monkeypatch):
    """Happy path: seed a session with basics, call /linkedin, get back
    a profile + copy-paste plain text."""
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _stub_linkedin_response(),
    )
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test User"}})
    res = client.post(f"/api/wizard/{sid}/linkedin")
    assert res.status_code == 200
    data = res.get_json()
    assert "profile" in data
    assert data["profile"]["headline"] == "Senior Backend Engineer"
    assert data["profile"]["about"].startswith("I'm a backend engineer")
    assert "plain_text" in data and "## Headline" in data["plain_text"]
    assert data["provider"]["name"] == "fake"
    # An empty-bullet session has nothing to fabricate against; warnings list
    # should at least be present.
    assert isinstance(data["warnings"], list)


def test_linkedin_endpoint_uses_saved_master_when_available(client, monkeypatch, tmp_path):
    """If session.promoted_master_path points at a real master.yaml on disk,
    LinkedIn pulls from it (not from a fresh in-memory promote)."""
    monkeypatch.setattr(
        _wizard_module, "pick_provider",
        lambda: _stub_linkedin_response(),
    )
    sid = client.post("/api/wizard").get_json()["id"]
    client.patch(f"/api/wizard/{sid}", json={"basics": {"name": "Test User"}})
    # Save master so the LinkedIn endpoint sees a promoted path.
    from resume_builder.web import app
    app.config["WIZARD_MASTER_OUTPUT_PATH"] = str(tmp_path / "master.yaml")
    client.post(f"/api/wizard/{sid}/promote-save", json={})

    res = client.post(f"/api/wizard/{sid}/linkedin")
    assert res.status_code == 200
    data = res.get_json()
    assert data["master_source"] == "saved_yaml"
    assert data["saved_master_path"].endswith("master.yaml")
    app.config.pop("WIZARD_MASTER_OUTPUT_PATH", None)


def test_linkedin_endpoint_404_for_unknown_session(client):
    res = client.post("/api/wizard/does-not-exist/linkedin")
    assert res.status_code == 404
