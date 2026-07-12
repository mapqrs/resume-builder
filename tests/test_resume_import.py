"""Résumé import (roadmap #2) + sessions gallery (roadmap #3)."""

from __future__ import annotations

import json

import pytest

from resume_builder import resume_import
from resume_builder.llm import CopyPasteRequired, LLMProvider, ProviderChoice
from resume_builder.resume_import import (
    ImportParseError,
    apply_import,
    parse_import_response,
    session_has_content,
)
from resume_builder.session_store import BootstrapSession, new_session


VALID_PAYLOAD = {
    "basics": {
        "name": "Jane Doe",
        "email": "jane@example.com",
        "phone": None,
        "location": "Bengaluru",
        "links": [{"label": "GitHub", "url": "https://github.com/jane"}],
    },
    "summary": "Backend engineer with payments experience.",
    "employment": [
        {
            "company": "Acme",
            "role": "Senior Engineer",
            "start": "2022-03",
            "end": None,
            "location": "Bengaluru",
            "bullets": ["Led the dispatch rewrite", "Cut p99 from 480ms to 95ms"],
        },
        {
            "company": "Beta Corp",
            "role": "Engineer",
            "start": "2019",
            "end": "2022-02",
            "location": None,
            "bullets": ["Built the billing service"],
        },
    ],
    "education": [
        {
            "school": "IIT Bombay",
            "degree": "B.Tech CS",
            "year": "2019",
            "status": "graduated",
            "location": None,
            "gpa": "8.9/10",
        }
    ],
    "skills": ["Python", "Go", "Postgres"],
    "warnings": [],
}


SAMPLE_RESUME_TEXT = (
    "Jane Doe — jane@example.com — Bengaluru\n\n"
    "Senior Engineer, Acme (Mar 2022 – Present)\n"
    "- Led the dispatch rewrite\n"
    "- Cut p99 from 480ms to 95ms\n\n"
    "Engineer, Beta Corp (2019 – Feb 2022)\n"
    "- Built the billing service\n\n"
    "Education: B.Tech CS, IIT Bombay, 2019 (8.9/10)\n"
    "Skills: Python, Go, Postgres\n"
)


# ---------- parse ----------


def test_parse_tolerates_fences_and_prose():
    fenced = "```json\n" + json.dumps(VALID_PAYLOAD) + "\n```"
    assert parse_import_response(fenced).basics.name == "Jane Doe"
    prosey = "Here is the parse:\n" + json.dumps(VALID_PAYLOAD)
    assert parse_import_response(prosey).employment[0].company == "Acme"


def test_parse_rejects_non_json():
    with pytest.raises(ImportParseError):
        parse_import_response("I could not parse this resume, sorry!")


# ---------- apply ----------


def _apply(payload) -> tuple[BootstrapSession, resume_import.ImportSummary]:
    s = new_session()
    parsed = parse_import_response(json.dumps(payload))
    return s, apply_import(s, parsed)


def test_apply_builds_chunks_ascending_with_employment():
    s, summary = _apply(VALID_PAYLOAD)
    assert [c.id for c in s.chunks] == ["chunk-2019-01", "chunk-2022-03"]
    assert s.chunks[0].label == "Beta Corp — Engineer"
    assert s.chunks[0].end == "2022-02"
    assert "Led the dispatch rewrite" in s.chunks[1].raw_notes
    assert s.career_start == "2019-01"
    assert [e.company for e in s.employment] == ["Beta Corp", "Acme"]
    assert summary.employment_chunks == 2


def test_apply_current_job_ends_now():
    s, _ = _apply(VALID_PAYLOAD)
    # end=None normalises to the current YYYY-MM (never null).
    assert s.chunks[-1].end == resume_import._now_ym()


def test_apply_skips_undated_job_with_warning():
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["employment"].append(
        {"company": "NoDates Inc", "role": "Intern", "start": None,
         "end": None, "location": None, "bullets": ["Did things"]}
    )
    s, summary = _apply(payload)
    assert len(s.chunks) == 2
    assert any("NoDates Inc" in w for w in summary.warnings)


def test_apply_chunk_id_collision_gets_suffix():
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["employment"].append(
        {"company": "Moonlight LLC", "role": "Advisor", "start": "2022-03",
         "end": "2023-01", "location": None, "bullets": ["Advised"]}
    )
    s, _ = _apply(payload)
    ids = [c.id for c in s.chunks]
    assert "chunk-2022-03" in ids and "chunk-2022-03-2" in ids


def test_apply_skills_become_drafts_on_newest_chunk():
    s, summary = _apply(VALID_PAYLOAD)
    skills = [d for d in s.drafts if d.bucket == "skills"]
    assert [d.draft_bullet for d in skills] == ["Python", "Go", "Postgres"]
    assert all(d.chunk_id == "chunk-2022-03" for d in skills)
    assert summary.skills == 3


def test_apply_skills_without_employment_warns():
    payload = {"skills": ["Python"], "employment": []}
    s, summary = _apply(payload)
    assert not s.drafts
    assert any("skills" in w.lower() for w in summary.warnings)


def test_apply_basics_and_education():
    s, summary = _apply(VALID_PAYLOAD)
    assert s.basics.name == "Jane Doe"
    assert s.basics.links[0].url == "https://github.com/jane"
    assert summary.basics_filled
    assert s.education[0].id == "edu-iit-bombay"
    assert s.education[0].gpa == "8.9/10"


def test_apply_missing_name_warns_and_skips_basics():
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["basics"]["name"] = None
    s, summary = _apply(payload)
    assert s.basics is None
    assert not summary.basics_filled
    assert any("name" in w.lower() for w in summary.warnings)


def test_apply_bad_education_status_falls_back_to_graduated():
    payload = json.loads(json.dumps(VALID_PAYLOAD))
    payload["education"][0]["status"] = "did-great-honestly"
    s, _ = _apply(payload)
    assert s.education[0].status == "graduated"


def test_session_has_content_flips_after_apply():
    s = new_session()
    assert not session_has_content(s)
    apply_import(s, parse_import_response(json.dumps(VALID_PAYLOAD)))
    assert session_has_content(s)


# ---------- endpoints ----------


class FakeProvider(LLMProvider):
    name = "fake"

    def __init__(self, reply: str):
        self.reply = reply

    @classmethod
    def is_available(cls) -> bool:  # pragma: no cover
        return True

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        return self.reply


class RaisingCopyPasteProvider(LLMProvider):
    name = "copy-paste"

    @classmethod
    def is_available(cls) -> bool:  # pragma: no cover
        return True

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        raise CopyPasteRequired(system_prompt, user_message)


@pytest.fixture
def client(tmp_path):
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["WIZARD_SESSIONS_ROOT"] = str(tmp_path)
    with app.test_client() as c:
        yield c


def _new_session_id(client) -> str:
    return client.post("/api/wizard").get_json()["id"]


def _fake_pick(monkeypatch, provider):
    monkeypatch.setattr(
        "resume_builder.wizard.pick_provider",
        lambda **kw: ProviderChoice(provider, "test"),
    )


def test_import_apply_rejects_short_text(client):
    sid = _new_session_id(client)
    res = client.post(f"/api/wizard/{sid}/import-apply", json={"text": "too short"})
    assert res.status_code == 422
    assert res.get_json()["error"] == "text_too_short"


def test_import_apply_happy_path(client, monkeypatch):
    _fake_pick(monkeypatch, FakeProvider(json.dumps(VALID_PAYLOAD)))
    sid = _new_session_id(client)
    res = client.post(
        f"/api/wizard/{sid}/import-apply", json={"text": SAMPLE_RESUME_TEXT},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["summary"]["employment_chunks"] == 2
    assert body["session"]["basics"]["name"] == "Jane Doe"
    assert "llm_call" in body

    # Persisted: a fresh GET shows the imported chunks.
    again = client.get(f"/api/wizard/{sid}").get_json()
    assert len(again["chunks"]) == 2


def test_import_apply_409_then_force_replaces(client, monkeypatch):
    _fake_pick(monkeypatch, FakeProvider(json.dumps(VALID_PAYLOAD)))
    sid = _new_session_id(client)
    # Give the session typed content.
    client.patch(f"/api/wizard/{sid}", json={
        "career_start": "2020-01",
    })
    client.post(f"/api/wizard/{sid}/regenerate-chunks", json={})
    session = client.get(f"/api/wizard/{sid}").get_json()
    chunks = session["chunks"]
    chunks[0]["raw_notes"] = "I typed something precious here"
    client.patch(f"/api/wizard/{sid}", json={"chunks": chunks})

    res = client.post(
        f"/api/wizard/{sid}/import-apply", json={"text": SAMPLE_RESUME_TEXT},
    )
    assert res.status_code == 409

    res = client.post(
        f"/api/wizard/{sid}/import-apply",
        json={"text": SAMPLE_RESUME_TEXT, "force": True},
    )
    assert res.status_code == 200
    assert res.get_json()["summary"]["employment_chunks"] == 2


def test_import_apply_copy_paste_flow(client, monkeypatch):
    _fake_pick(monkeypatch, RaisingCopyPasteProvider())
    sid = _new_session_id(client)
    res = client.post(
        f"/api/wizard/{sid}/import-apply", json={"text": SAMPLE_RESUME_TEXT},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert body["copy_paste_required"] is True
    assert "PARSER" in body["system_prompt"]
    assert SAMPLE_RESUME_TEXT.split("\n")[0] in body["user_message"]

    # Paste the reply back.
    res = client.post(
        f"/api/wizard/{sid}/import-apply-response",
        json={"response_text": json.dumps(VALID_PAYLOAD)},
    )
    assert res.status_code == 200
    assert res.get_json()["summary"]["employment_chunks"] == 2


def test_import_apply_response_rejects_junk(client):
    sid = _new_session_id(client)
    res = client.post(
        f"/api/wizard/{sid}/import-apply-response",
        json={"response_text": "not json at all"},
    )
    assert res.status_code == 400
    assert res.get_json()["error"] == "parse_failed"


def test_import_apply_parse_failure_502(client, monkeypatch):
    _fake_pick(monkeypatch, FakeProvider("the model rambled instead of JSON"))
    sid = _new_session_id(client)
    res = client.post(
        f"/api/wizard/{sid}/import-apply", json={"text": SAMPLE_RESUME_TEXT},
    )
    assert res.status_code == 502
    assert res.get_json()["error"] == "parse_failed"


# ---------- sessions gallery ----------


def test_sessions_index_lists_newest_first_with_labels(client, monkeypatch, tmp_path):
    _fake_pick(monkeypatch, FakeProvider(json.dumps(VALID_PAYLOAD)))
    sid_old = _new_session_id(client)
    sid_new = _new_session_id(client)
    # Import into the newer session so it has a basics-name label + timestamps bump.
    client.post(f"/api/wizard/{sid_new}/import-apply", json={"text": SAMPLE_RESUME_TEXT})

    # updated_at has second resolution, so same-second writes tie and the
    # order becomes arbitrary. Backdate the old session to make it stable.
    import yaml as _yaml
    state = tmp_path / sid_old / "state.yaml"
    data = _yaml.safe_load(state.read_text(encoding="utf-8"))
    data["updated_at"] = "2020-01-01T00:00:00Z"
    state.write_text(_yaml.safe_dump(data, sort_keys=False), encoding="utf-8")

    body = client.get("/api/wizard/sessions").get_json()
    ids = [s["id"] for s in body["sessions"]]
    assert ids[0] == sid_new  # newest first
    assert sid_old in ids
    by_id = {s["id"]: s for s in body["sessions"]}
    assert by_id[sid_new]["label"] == "Jane Doe"
    assert by_id[sid_new]["chunks"] == 2
    assert by_id[sid_new]["promoted"] is False
    assert by_id[sid_old]["label"].startswith("Session ")


def test_delete_session_route(client):
    sid = _new_session_id(client)
    assert client.get(f"/api/wizard/{sid}").status_code == 200
    res = client.delete(f"/api/wizard/{sid}")
    assert res.status_code == 200
    assert client.get(f"/api/wizard/{sid}").status_code == 404
    assert client.delete(f"/api/wizard/{sid}").status_code == 404


def test_wizard_page_has_import_hero_and_gallery(client):
    body = client.get("/wizard").get_data(as_text=True)
    assert "import-hero" in body
    assert "import_hero_file" in body
    assert "sessions-gallery" in body
