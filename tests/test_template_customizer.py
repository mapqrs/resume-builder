"""Template customizer (adoption A1): /api/template-values, /api/save-template,
and the `custom` preset that loads the saved template.yaml.
"""

from __future__ import annotations

import yaml

import pytest

from resume_builder.schema import Template
from resume_builder.template_presets import default_preset_id, get_preset


@pytest.fixture
def client(tmp_path, monkeypatch):
    """Test client chdir'd to an empty tmp dir (no template.yaml yet)."""
    monkeypatch.chdir(tmp_path)
    from resume_builder.web import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


def _valid_payload(**overrides):
    data = Template().model_dump()
    data.update(overrides)
    return {"template": data}


# ---------- /api/template-values ----------


def test_values_default_when_no_file(client):
    res = client.get("/api/template-values")
    assert res.status_code == 200
    body = res.get_json()
    assert body["preset"] == "custom"
    assert body["template"]["fonts"]["body"]["name"] == "Calibri"
    assert body["template"]["fonts"]["body"]["size"] == 10.5


def test_values_for_named_preset(client):
    pid = default_preset_id()
    res = client.get(f"/api/template-values?preset={pid}")
    assert res.status_code == 200
    body = res.get_json()
    assert body["preset"] == pid
    assert body["template"] == get_preset(pid).template.model_dump()


def test_values_unknown_preset_404(client):
    assert client.get("/api/template-values?preset=nope").status_code == 404


# ---------- /api/save-template ----------


def test_save_template_writes_valid_yaml(client, tmp_path):
    payload = _valid_payload()
    payload["template"]["fonts"]["body"]["name"] = "Georgia"
    payload["template"]["colors"]["accent"] = "#AA3300"
    res = client.post("/api/save-template", json=payload)
    assert res.status_code == 200
    saved = Template.model_validate(
        yaml.safe_load((tmp_path / "template.yaml").read_text(encoding="utf-8"))
    )
    assert saved.fonts.body.name == "Georgia"
    assert saved.colors.accent == "#AA3300"

    # And the presets endpoint now reports the custom file.
    body = client.get("/api/template-presets").get_json()
    assert body["custom_exists"] is True


def test_save_template_rejects_invalid(client, tmp_path):
    payload = _valid_payload()
    payload["template"]["page"]["size"] = "tabloid"  # not letter|a4
    res = client.post("/api/save-template", json=payload)
    assert res.status_code == 400
    assert not (tmp_path / "template.yaml").exists()


def test_save_template_rejects_missing_body(client):
    assert client.post("/api/save-template", json={}).status_code == 400
    assert client.post("/api/save-template", data="junk").status_code == 400


def test_save_template_persists_section_order(client, tmp_path):
    payload = _valid_payload(
        section_order=["experience", "skills", "education"]  # summary+projects hidden
    )
    assert client.post("/api/save-template", json=payload).status_code == 200
    saved = yaml.safe_load((tmp_path / "template.yaml").read_text(encoding="utf-8"))
    assert saved["section_order"] == ["experience", "skills", "education"]


# ---------- the `custom` preset in the generate flow ----------


def test_resolve_custom_preset_loads_saved_file(client, tmp_path):
    payload = _valid_payload()
    payload["template"]["fonts"]["body"]["name"] = "Garamond"
    client.post("/api/save-template", json=payload)

    from resume_builder.web import _resolve_template_and_length
    template, length_override = _resolve_template_and_length({"preset": "custom"})
    assert template.fonts.body.name == "Garamond"
    assert length_override is None


def test_resolve_custom_preset_missing_file_falls_back(client):
    from resume_builder.web import _resolve_template_and_length
    template, _ = _resolve_template_and_length({"preset": "custom"})
    assert template == Template()


def test_resolve_custom_preset_malformed_file_falls_back(client, tmp_path):
    (tmp_path / "template.yaml").write_text("fonts: [broken", encoding="utf-8")
    from resume_builder.web import _resolve_template_and_length
    template, _ = _resolve_template_and_length({"preset": "custom"})
    assert template == Template()
