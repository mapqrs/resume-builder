"""Tests for Phase 10 batch: format presets, brain-dump cadence, about page,
self-reflection page.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest

from resume_builder.session_store import (
    CADENCE_GUIDANCE,
    CADENCE_MONTHS,
    default_chunks_for,
    suggest_cadence,
)
from resume_builder.template_presets import (
    PRESETS,
    all_presets_for_ui,
    default_preset_id,
    get_preset,
    preset_for_years_experience,
)


# ---------- template presets ----------


def test_three_presets_exist():
    assert len(PRESETS) == 3
    ids = {p.id for p in PRESETS}
    assert ids == {"bock-classic-1pg", "detailed-2pg", "modern-compact-1pg"}


def test_default_preset_is_bock_classic():
    assert default_preset_id() == "bock-classic-1pg"


def test_each_preset_has_complete_guidance():
    for p in PRESETS:
        g = p.guidance
        assert g.best_for
        assert g.pages
        assert g.years_experience
        assert g.notes
        # Length pointer is one of the canonical Pointers length values.
        assert p.length_pointer in ("1page", "2page")


def test_preset_for_years_experience_routes_correctly():
    assert preset_for_years_experience(2).id == "bock-classic-1pg"
    assert preset_for_years_experience(5).id == "modern-compact-1pg"
    assert preset_for_years_experience(12).id == "detailed-2pg"


def test_get_preset_raises_on_unknown():
    with pytest.raises(KeyError):
        get_preset("not-a-real-preset")


def test_detailed_2pg_has_bigger_margins_than_classic():
    classic = get_preset("bock-classic-1pg").template
    detailed = get_preset("detailed-2pg").template
    # Margins compared as strings (both "X.Yin"); the detailed preset is
    # bigger so the float values are strictly higher.
    def _val(s):
        return float(s.replace("in", ""))
    assert _val(detailed.page.margin_top) > _val(classic.page.margin_top)
    assert _val(detailed.page.margin_left) > _val(classic.page.margin_left)


def test_modern_compact_uses_accent_heading_color():
    modern = get_preset("modern-compact-1pg").template
    # Heading + accent are tinted; body stays black for ATS friendliness.
    assert modern.colors.heading != "#000000"
    assert modern.colors.body == "#000000"


def test_all_presets_for_ui_is_json_serialisable():
    payload = all_presets_for_ui()
    # JSON round-trip should not lose fields.
    rt = json.loads(json.dumps(payload))
    assert rt == payload
    assert any(p["is_default"] for p in rt)


# ---------- web wiring for presets ----------


@pytest.fixture
def web_client():
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["DISABLE_FIRSTRUN_REDIRECT"] = True
    with app.test_client() as client:
        yield client
    app.config.pop("DISABLE_FIRSTRUN_REDIRECT", None)


def test_template_presets_endpoint_returns_all_three(web_client):
    res = web_client.get("/api/template-presets")
    assert res.status_code == 200
    body = res.get_json()
    assert body["default_id"] == "bock-classic-1pg"
    assert len(body["presets"]) == 3
    ids = {p["id"] for p in body["presets"]}
    assert ids == {"bock-classic-1pg", "detailed-2pg", "modern-compact-1pg"}


def test_index_renders_preset_select_element(web_client):
    res = web_client.get("/?skip-wizard=1")
    body = res.get_data(as_text=True)
    assert "preset-select" in body
    assert "preset-guidance" in body


# ---------- cadence math ----------


def test_cadence_table_has_four_options():
    assert set(CADENCE_MONTHS) == {"monthly", "quarterly", "six-monthly", "annual"}
    assert CADENCE_MONTHS["monthly"] == 1
    assert CADENCE_MONTHS["quarterly"] == 3
    assert CADENCE_MONTHS["six-monthly"] == 6
    assert CADENCE_MONTHS["annual"] == 12


def test_cadence_guidance_has_every_field():
    for cad, guide in CADENCE_GUIDANCE.items():
        assert guide["label"]
        assert guide["best_for"]
        assert guide["notes"]


def test_suggest_cadence_for_short_tenure_picks_quarterly():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert suggest_cadence("2023-06", today=today) == "quarterly"


def test_suggest_cadence_for_mid_tenure_picks_six_monthly():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    assert suggest_cadence("2019-06", today=today) == "six-monthly"


def test_suggest_cadence_for_long_tenure_picks_annual():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    # 12 years of experience
    assert suggest_cadence("2012-01", today=today) == "annual"


def test_default_chunks_for_monthly_cadence():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2024-01", today=today, cadence="monthly")
    # 5 monthly chunks (Jan, Feb, Mar, Apr, May; June clamped).
    assert len(chunks) == 5
    assert chunks[0].label == "2024-01"
    assert chunks[-1].label in ("2024-05", "2024-06")


def test_default_chunks_for_quarterly_cadence_uses_q_labels():
    today = datetime(2024, 12, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2024-01", today=today, cadence="quarterly")
    labels = [c.label for c in chunks]
    assert "Q1 2024" in labels
    assert "Q2 2024" in labels


def test_default_chunks_for_annual_cadence():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    chunks = default_chunks_for("2020-01", today=today, cadence="annual")
    # 2020, 2021, 2022, 2023, 2024 (the last clamped to June).
    assert len(chunks) == 5
    assert chunks[0].label == "2020"
    assert chunks[-1].label == "2024"


def test_default_chunks_for_falls_back_to_auto_when_no_cadence():
    today = datetime(2024, 6, 1, tzinfo=timezone.utc)
    # Auto-pick keeps the legacy behaviour (6 months for <5 years).
    chunks = default_chunks_for("2022-06", today=today)
    # 2 years window → 6-month chunks → 4 chunks
    assert len(chunks) == 4


def test_default_chunks_for_rejects_unknown_cadence():
    with pytest.raises(ValueError, match="unknown cadence"):
        default_chunks_for("2023-01", cadence="weekly")


# ---------- wizard endpoint: cadence pass-through ----------


@pytest.fixture
def wizard_client(tmp_path):
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["WIZARD_SESSIONS_ROOT"] = str(tmp_path)
    with app.test_client() as client:
        yield client


def test_regenerate_chunks_accepts_cadence(wizard_client):
    sid = wizard_client.post("/api/wizard").get_json()["id"]
    wizard_client.patch(f"/api/wizard/{sid}", json={"career_start": "2023-01"})
    res = wizard_client.post(
        f"/api/wizard/{sid}/regenerate-chunks",
        json={"cadence": "quarterly"},
    )
    assert res.status_code == 200
    data = res.get_json()
    # Cadence persisted on the session.
    assert data["cadence"] == "quarterly"
    # Chunks use Q1/Q2/etc labels.
    labels = [c["label"] for c in data["chunks"]]
    assert any(l.startswith("Q") for l in labels)


def test_regenerate_chunks_rejects_invalid_cadence(wizard_client):
    sid = wizard_client.post("/api/wizard").get_json()["id"]
    wizard_client.patch(f"/api/wizard/{sid}", json={"career_start": "2023-01"})
    res = wizard_client.post(
        f"/api/wizard/{sid}/regenerate-chunks",
        json={"cadence": "monthly-ish"},
    )
    assert res.status_code == 400


def test_session_payload_surfaces_cadence_options(wizard_client):
    sid = wizard_client.post("/api/wizard").get_json()["id"]
    data = wizard_client.get(f"/api/wizard/{sid}").get_json()
    options = data.get("cadence_options")
    assert isinstance(options, list) and len(options) == 4
    ids = [o["id"] for o in options]
    assert ids == ["monthly", "quarterly", "six-monthly", "annual"]


def test_session_payload_surfaces_suggested_cadence_after_career_start(
    wizard_client,
):
    sid = wizard_client.post("/api/wizard").get_json()["id"]
    # Before career_start: suggestion is null.
    data = wizard_client.get(f"/api/wizard/{sid}").get_json()
    assert data["suggested_cadence"] is None
    # After setting career_start: suggestion populated.
    wizard_client.patch(f"/api/wizard/{sid}", json={"career_start": "2023-01"})
    data = wizard_client.get(f"/api/wizard/{sid}").get_json()
    assert data["suggested_cadence"] in (
        "monthly", "quarterly", "six-monthly", "annual",
    )


# ---------- about page ----------


def test_about_page_renders(web_client):
    res = web_client.get("/about")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # Major section headings present.
    assert "Why a resume" in body or "What this tool is" in body
    assert "What to check" in body
    assert "What <em>not</em>" in body or "obsess over" in body
    assert "Where this helps" in body or "How to start" in body
    # Links into the rest of the app.
    assert "/wizard" in body
    # Privacy footer carries through.
    assert "privacy-footer" in body


def test_index_topbar_links_to_about(web_client):
    res = web_client.get("/?skip-wizard=1")
    body = res.get_data(as_text=True)
    assert 'href="/about"' in body


# ---------- self-reflection page ----------


def test_reflect_page_renders(web_client):
    """Phase 10.4 v2: reflect page renders the Four Levers worksheet."""
    res = web_client.get("/reflect")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # The four levers.
    assert "Judgment" in body
    assert "Pressure" in body
    assert "Trust" in body
    # Signal & Skills — using the &amp; entity-escaped form.
    assert "Signal" in body and "Skills" in body
    # Synthesize button now wired (not locked).
    assert "btn-synthesize" in body
    assert "Synthesize my edge" in body


def test_index_topbar_links_to_reflect(web_client):
    res = web_client.get("/?skip-wizard=1")
    body = res.get_data(as_text=True)
    assert 'href="/reflect"' in body


# ---------- CLI --preset flag ----------


def test_cli_preset_flag_loads_template(tmp_path, monkeypatch):
    """--preset overrides --template and seeds length pointer."""
    from resume_builder import cli as _cli

    sample = Path(__file__).parent / "fixtures" / "sample-master.yaml"
    out_docx = tmp_path / "out.docx"
    rc = _cli.main([
        "--master", str(sample),
        "--out", str(out_docx),
        "--no-tailor",
        "--preset", "modern-compact-1pg",
    ])
    assert rc == 0
    assert out_docx.exists()


def test_cli_unknown_preset_errors(tmp_path):
    """argparse rejects unknown preset names at the parse stage."""
    from resume_builder import cli as _cli

    sample = Path(__file__).parent / "fixtures" / "sample-master.yaml"
    with pytest.raises(SystemExit):
        _cli.main([
            "--master", str(sample),
            "--out", str(tmp_path / "out.docx"),
            "--no-tailor",
            "--preset", "fancy-pants",
        ])


# ---------- preset wires into /api/generate ----------


def test_generate_accepts_preset_field(web_client, monkeypatch):
    """The /api/generate route honors the preset form field.

    We can't actually generate without an LLM in tests, but we can check
    the request validation accepts the preset and the bad-preset case
    is surfaced as a parse error (400).
    """
    sample = (Path(__file__).parent / "fixtures" / "sample-master.yaml").read_text()
    # Missing JD + missing target — should 400 (preset is fine, but the
    # endpoint requires one of the two inputs).
    res = web_client.post("/api/generate", data={
        "master_yaml": sample,
        "preset": "modern-compact-1pg",
    })
    assert res.status_code == 400
    body = res.get_json()
    assert "jd_text" in body["error"] or "target_role" in body["error"]


def test_generate_rejects_unknown_preset(web_client):
    sample = (Path(__file__).parent / "fixtures" / "sample-master.yaml").read_text()
    res = web_client.post("/api/generate", data={
        "master_yaml": sample,
        "preset": "fancy-pants",
        "jd_text": "Senior Backend Engineer",
    })
    assert res.status_code == 400
    body = res.get_json()
    assert "preset" in body["error"] or "unknown" in body["error"]
