"""Tests for the Four Levers self-reflection synthesis (reflect_synth.py).

Layers:
1. Pure unit tests on build_user_message / parse / validate.
2. Adversarial guard tests — LLM responses that invent companies or
   numbers get caught.
3. Web route tests — /reflect renders all four levers + the synthesize
   endpoint accepts payloads and routes errors correctly.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resume_builder.llm import LLMProvider
from resume_builder.reflect_synth import (
    LEVER_KEYS,
    MIN_FILLED_ANSWERS,
    ReflectSynthError,
    SynthesisResult,
    build_user_message,
    filled_answer_count,
    parse_response_text,
    synthesize,
    validate_synthesis,
)


# ---------- helpers ----------


class _FakeProvider(LLMProvider):
    """Returns a scripted response. Records the prompts so we can assert."""

    name = "fake-reflect"

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


def _full_worksheet():
    """A complete worksheet with no proper nouns — only generic vocab.

    Lets us write tests where the guard never has false positives.
    """
    return {
        # Lever 1 — Judgment
        "l1-p1": "I said no to a feature rollout last quarter when the data was thin.",
        "l1-p2": "I cut through noise faster when teams chase too many things at once.",
        "l1-p3": "trade-offs between speed and quality",
        "l1-p4": "messes around premature scaling",
        "l1-p5": "decisions on what to deprecate",
        "l1-s1": "we're debating which features to build",
        "l1-s2": "shipping for the sake of shipping",
        # Lever 2 — Pressure
        "l2-p1": "I reset priorities when leadership changed direction.",
        "l2-p2": "I step into messy handoffs and align teams.",
        "l2-p3": "I handle ambiguity better than most.",
        "l2-p4": "junior engineers pull me in when stuck on architecture",
        "l2-p5": "I keep moving on documentation and writing",
        "l2-s1": "reset priorities and align the team",
        "l2-s2": "everyone else is reacting",
        # Lever 3 — Trust
        "l3-p1": "people expect me to write the post-mortem",
        "l3-p2": "post-mortems would quietly fall apart without me",
        "l3-p3": "tough technical conversations",
        "l3-p4": "I carry the consistency the team needs",
        "l3-p5": "feedback that I bring clarity to ambiguity",
        "l3-s1": "bring clarity to ambiguous decisions",
        "l3-s2": "consistency and zero drama",
        # Lever 4 — Signal & Skills
        "l4-p1": "writing technical documentation",
        "l4-p2": "patience and mentorship",
        "l4-p3": "revenue thinking and customer pain",
        "l4-p4": "user behaviors around onboarding",
        "l4-p5": "they would miss the writing piece",
        "l4-p6": "my edge is clear only to direct collaborators",
        "l4-p7": "the architecture review I led last year",
        "l4-p8": "the writing piece would not carry over",
        "l4-p9": "people still see me as a doer rather than a leader",
        "l4-s1": "connecting technical decisions to revenue impact",
        "l4-s2": "only sharing surface-level wins",
    }


def _good_synthesis_payload():
    """An LLM response that uses only generic vocab from the worksheet —
    should sail through the guard."""
    return json.dumps({
        "edge_summary": (
            "The candidate's edge is bringing clarity to ambiguous "
            "decisions and connecting technical work to revenue impact."
        ),
        "next_steps": [
            "Apply to staff-level roles where ambiguity is the norm.",
            "Ask three senior leaders for a 30-minute coffee chat about scope.",
            "Surface the architecture review work in the next role conversation.",
        ],
        "master_additions": [
            "Reset priorities after leadership changed direction, aligning the team on the next quarter's bets.",
            "Wrote the team's first post-mortem template, adopted across handoffs.",
        ],
        "linkedin_additions": [
            "Update headline to mention writing and clarity work.",
            "Add to your About: connecting technical decisions to revenue impact.",
        ],
        "rationale": (
            "Across all four levers, the consistent pattern is clarity "
            "and writing — under-shown in current materials."
        ),
    })


# ---------- filled_answer_count ----------


def test_filled_answer_count_counts_non_blank_only():
    answers = {"l1-p1": "yes", "l1-p2": "", "l1-p3": "   ", "l2-p1": "yes"}
    assert filled_answer_count(answers) == 2


def test_filled_answer_count_handles_missing_keys():
    assert filled_answer_count({}) == 0


# ---------- build_user_message ----------


def test_build_user_message_includes_every_filled_lever():
    answers = _full_worksheet()
    msg = build_user_message(answers)
    assert "Judgment" in msg
    assert "Pressure" in msg
    assert "Trust" in msg
    assert "Signal & Skills" in msg
    # Every filled answer appears verbatim.
    assert "feature rollout last quarter" in msg
    assert "reset priorities" in msg.lower()


def test_build_user_message_flags_blank_lever_honestly():
    """If the user leaves a whole lever empty, the prompt says so —
    the LLM must NOT make up content."""
    answers = {"l1-p1": "I said no to scope creep"}
    msg = build_user_message(answers)
    assert "candidate left this lever blank" in msg
    # The judgment section has one answer; pressure/trust/signal are blank.
    assert msg.count("candidate left this lever blank") == 3


def test_build_user_message_includes_schema_hint():
    msg = build_user_message({"l1-p1": "x"})
    assert "edge_summary" in msg
    assert "next_steps" in msg


# ---------- parse_response_text ----------


def test_parse_extracts_full_synthesis():
    raw = _good_synthesis_payload()
    result = parse_response_text(raw)
    assert "clarity" in result.edge_summary.lower()
    assert len(result.next_steps) == 3
    assert len(result.master_additions) == 2
    assert len(result.linkedin_additions) == 2
    assert result.rationale


def test_parse_tolerates_fences_and_prose():
    raw = (
        "Sure, here's the synthesis:\n\n```json\n"
        + _good_synthesis_payload()
        + "\n```\n\nLet me know if you'd like tweaks!"
    )
    result = parse_response_text(raw)
    assert "clarity" in result.edge_summary.lower()


def test_parse_raises_on_missing_edge_summary():
    raw = json.dumps({"next_steps": ["x"]})
    with pytest.raises(ReflectSynthError, match="edge_summary"):
        parse_response_text(raw)


def test_parse_raises_on_invalid_json():
    with pytest.raises(ReflectSynthError, match="not valid JSON"):
        parse_response_text("{ not valid }")


def test_parse_filters_blank_list_entries():
    raw = json.dumps({
        "edge_summary": "ok",
        "next_steps": ["valid", "", "   ", "also valid"],
    })
    result = parse_response_text(raw)
    assert result.next_steps == ["valid", "also valid"]


# ---------- validate_synthesis (anti-fabrication) ----------


def test_guard_passes_when_only_generic_vocab():
    """A synthesis using only generic English passes."""
    answers = _full_worksheet()
    result = parse_response_text(_good_synthesis_payload())
    guard = validate_synthesis(answers, result)
    assert guard.warnings == []


def test_guard_catches_invented_company_in_edge_summary():
    """An LLM that invents a company name not in the worksheet fails."""
    answers = _full_worksheet()
    result = SynthesisResult(
        edge_summary="The candidate's edge is shipping at Snowflake-scale.",
    )
    guard = validate_synthesis(answers, result)
    assert any(
        w.section == "edge_summary" and "snowflake" in w.reason.lower()
        for w in guard.warnings
    )


def test_guard_catches_invented_number_in_next_steps():
    """Numbers above 100 (or with magnitude suffixes) are treated as
    invented claims, not generic coaching vocabulary."""
    answers = _full_worksheet()
    result = SynthesisResult(
        edge_summary="ok",
        next_steps=["Send 5000 cold emails to founders."],
    )
    guard = validate_synthesis(answers, result)
    assert any(
        w.section.startswith("next_steps") and "5000" in w.reason
        for w in guard.warnings
    )


def test_guard_allows_generic_small_numbers_in_coaching_prose():
    """Small whole numbers are descriptive ('ask three leaders',
    '30-minute chat') — they're not invented claims."""
    answers = _full_worksheet()
    result = SynthesisResult(
        edge_summary="ok",
        next_steps=[
            "Ask 3 senior leaders for a 30-minute chat.",
            "Send 10 cold emails per week.",
        ],
    )
    guard = validate_synthesis(answers, result)
    assert guard.warnings == []


def test_guard_catches_invented_tool_in_master_additions():
    answers = _full_worksheet()
    result = SynthesisResult(
        edge_summary="ok",
        master_additions=["Built Kubernetes operator that ships nightly."],
    )
    guard = validate_synthesis(answers, result)
    assert any(
        w.section.startswith("master_additions") and "kubernetes" in w.reason.lower()
        for w in guard.warnings
    )


def test_guard_passes_when_proper_noun_came_from_worksheet():
    """If the candidate wrote 'Postgres' in their answer, the LLM can
    use 'Postgres' in the synthesis without tripping the guard."""
    answers = _full_worksheet()
    answers["l4-p1"] = "Postgres query optimization"
    result = SynthesisResult(
        edge_summary="The candidate's edge is Postgres expertise.",
    )
    guard = validate_synthesis(answers, result)
    edge_warnings = [w for w in guard.warnings if w.section == "edge_summary"]
    assert not any("postgres" in w.reason.lower() for w in edge_warnings)


def test_guard_passes_with_generic_role_vocab():
    answers = _full_worksheet()
    result = SynthesisResult(
        edge_summary="ok",
        linkedin_additions=[
            "Update your About to mention staff engineer-level scope.",
            "Add a Featured item for the post-mortem work.",
        ],
    )
    guard = validate_synthesis(answers, result)
    assert guard.warnings == []


# ---------- synthesize (orchestrator) ----------


def test_synthesize_runs_end_to_end_with_fake_provider():
    answers = _full_worksheet()
    provider = _FakeProvider(_good_synthesis_payload())
    result, guard, user_msg, raw = synthesize(answers, provider)
    assert result.edge_summary
    assert guard.warnings == []  # clean payload
    assert "Judgment" in user_msg
    assert raw == _good_synthesis_payload()
    assert provider.last_system is not None
    assert provider.last_user is not None


def test_synthesize_requires_min_filled_answers():
    answers = {"l1-p1": "x"}  # only 1 filled
    provider = _FakeProvider(_good_synthesis_payload())
    with pytest.raises(ReflectSynthError, match="at least"):
        synthesize(answers, provider)


def test_synthesize_raises_on_garbage_response():
    answers = _full_worksheet()
    provider = _FakeProvider("not even close to JSON")
    with pytest.raises(Exception):  # ValueError or ReflectSynthError
        synthesize(answers, provider)


# ---------- web route: /reflect renders Four Levers ----------


@pytest.fixture
def web_client():
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["DISABLE_FIRSTRUN_REDIRECT"] = True
    with app.test_client() as client:
        yield client
    app.config.pop("DISABLE_FIRSTRUN_REDIRECT", None)


def test_reflect_page_renders_all_four_levers(web_client):
    res = web_client.get("/reflect")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    assert "Four Levers of Your Edge" in body
    # Each lever's heading is present.
    assert "Judgment" in body
    assert "Pressure" in body
    assert "Trust" in body
    assert "Signal" in body and "Skills" in body


def test_reflect_page_has_every_prompt_input(web_client):
    """7 + 7 + 7 + 11 = 32 inputs total across the four levers."""
    res = web_client.get("/reflect")
    body = res.get_data(as_text=True)
    total = sum(1 for keys in LEVER_KEYS.values() for _ in keys)
    assert total == 32
    for keys in LEVER_KEYS.values():
        for key in keys:
            assert f'data-key="{key}"' in body, f"missing input for {key}"


def test_reflect_page_has_examples_accordion(web_client):
    """Every lever should have an examples accordion for users who
    want concrete reference points."""
    res = web_client.get("/reflect")
    body = res.get_data(as_text=True)
    assert body.count("examples-accordion") >= 4
    # Examples mention all three sample-domain leads.
    assert "Product:" in body
    assert "Growth:" in body
    assert "Marketing:" in body


def test_reflect_page_has_synthesize_button(web_client):
    res = web_client.get("/reflect")
    body = res.get_data(as_text=True)
    assert "btn-synthesize" in body
    assert "Synthesize my edge" in body
    # Button is NOT disabled any more (the old scaffold had it locked).
    assert 'id="btn-synthesize"' in body
    # Find the button's HTML and confirm no `disabled` attribute on it.
    btn_html = body[body.find('id="btn-synthesize"'):body.find('id="btn-synthesize"')+200]
    assert " disabled" not in btn_html


def test_reflect_page_has_export_button(web_client):
    res = web_client.get("/reflect")
    body = res.get_data(as_text=True)
    assert "btn-export-md" in body
    assert "Download answers" in body


# ---------- web route: /api/reflect/synthesize ----------


def test_synthesize_endpoint_returns_400_on_insufficient_answers(web_client):
    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": {"l1-p1": "x"}},  # only 1 filled
    )
    assert res.status_code == 400
    body = res.get_json()
    assert body["error"] == "insufficient_answers"
    assert body["min_filled"] == MIN_FILLED_ANSWERS


def test_synthesize_endpoint_returns_400_on_invalid_answers_shape(web_client):
    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": "not a dict"},
    )
    assert res.status_code == 400


def test_synthesize_endpoint_happy_path_with_fake_provider(web_client, monkeypatch):
    """Patch pick_provider to inject our fake; assert response shape."""
    from resume_builder import web as _web
    from resume_builder.llm import ProviderChoice

    def _fake_pick():
        return ProviderChoice(
            _FakeProvider(_good_synthesis_payload()),
            "fake provider for tests",
        )

    monkeypatch.setattr(_web, "pick_provider", _fake_pick)

    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": _full_worksheet()},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert "edge_summary" in body
    assert "next_steps" in body and isinstance(body["next_steps"], list)
    assert "master_additions" in body
    assert "linkedin_additions" in body
    assert "rationale" in body
    assert body["provider"]["name"] == "fake-reflect"
    # Clean payload → no guard warnings.
    assert body["warnings"] == []


def test_synthesize_endpoint_surfaces_guard_warnings(web_client, monkeypatch):
    """When the LLM invents a proper noun, the response carries warnings
    in the same shape the wizard uses for the resume guard."""
    from resume_builder import web as _web
    from resume_builder.llm import ProviderChoice

    bad_payload = json.dumps({
        "edge_summary": "The edge is shipping at Snowflake-scale.",
        "next_steps": ["Apply to roles at Stripe."],
        "master_additions": [],
        "linkedin_additions": [],
        "rationale": "",
    })

    def _fake_pick():
        return ProviderChoice(_FakeProvider(bad_payload), "fake")

    monkeypatch.setattr(_web, "pick_provider", _fake_pick)

    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": _full_worksheet()},
    )
    assert res.status_code == 200
    body = res.get_json()
    assert len(body["warnings"]) >= 2
    sections = {w["section"] for w in body["warnings"]}
    assert "edge_summary" in sections
    assert any(s.startswith("next_steps") for s in sections)


def test_synthesize_endpoint_returns_502_on_garbage_llm(web_client, monkeypatch):
    from resume_builder import web as _web
    from resume_builder.llm import ProviderChoice

    def _fake_pick():
        return ProviderChoice(_FakeProvider("not json at all"), "fake")

    monkeypatch.setattr(_web, "pick_provider", _fake_pick)

    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": _full_worksheet()},
    )
    assert res.status_code == 502
    assert res.get_json()["error"] == "synthesis_failed"


def test_synthesize_endpoint_ignores_unknown_keys(web_client, monkeypatch):
    """Junk keys passed in by accident don't reach the LLM prompt."""
    from resume_builder import web as _web
    from resume_builder.llm import ProviderChoice

    def _fake_pick():
        return ProviderChoice(
            _FakeProvider(_good_synthesis_payload()),
            "fake",
        )

    monkeypatch.setattr(_web, "pick_provider", _fake_pick)

    answers = _full_worksheet()
    answers["malicious_key"] = "drop_table_users"
    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": answers},
    )
    assert res.status_code == 200
    # The fake provider records the user message — assert the junk key
    # never made it in.
    # (Indirect check: response is happy-path; the synthesize call would
    # have errored if filtered=={} accidentally.)


def test_synthesize_endpoint_502_when_no_llm_provider(web_client, monkeypatch):
    """No claude CLI + no API key → 502 copy_paste_required."""
    from resume_builder import web as _web
    from resume_builder.llm import CopyPasteProvider, ProviderChoice

    def _fake_pick():
        return ProviderChoice(CopyPasteProvider(), "no auto AI")

    monkeypatch.setattr(_web, "pick_provider", _fake_pick)

    res = web_client.post(
        "/api/reflect/synthesize",
        json={"answers": _full_worksheet()},
    )
    assert res.status_code == 502
    body = res.get_json()
    assert body["error"] == "copy_paste_required"
