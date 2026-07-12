"""Tests for Phase 9: first-run wizard redirect, delete-my-data endpoint,
sample fixtures, privacy UI surface.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from resume_builder.loaders import load_master


SAMPLES_DIR = Path(__file__).parent.parent / "samples"


# ---------- sample fixtures ----------


def test_sample_master_is_schema_valid():
    """The committed example master.yaml must load without errors —
    otherwise the README's quick-start doesn't work for fresh clones."""
    sample = SAMPLES_DIR / "master.example.yaml"
    assert sample.exists(), f"missing {sample}"
    master = load_master(sample)
    assert master.basics.name
    assert len(master.experience) >= 1
    assert len(master.all_bullet_ids()) >= 3


def test_sample_jd_file_exists_and_has_content():
    """The example JD must be present + non-trivial so users can
    actually tailor against it as the README suggests."""
    jd = SAMPLES_DIR / "jd.example.txt"
    assert jd.exists()
    text = jd.read_text(encoding="utf-8")
    assert len(text) > 200
    # Mentions of the typical JD-shape headers.
    assert "What" in text or "Responsibilities" in text or "Requirements" in text


def test_samples_readme_present():
    assert (SAMPLES_DIR / "README.md").exists()


# ---------- first-run redirect ----------


@pytest.fixture
def web_client_no_master(tmp_path, monkeypatch):
    """Test client where cwd has no master.yaml — exercises the
    first-run redirect path."""
    monkeypatch.chdir(tmp_path)
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config.pop("DISABLE_FIRSTRUN_REDIRECT", None)
    with app.test_client() as client:
        yield client


@pytest.fixture
def web_client_with_master(tmp_path, monkeypatch):
    """Test client where cwd HAS a master.yaml — the redirect should NOT fire."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master.yaml").write_text(
        "schema_version: 1\nbasics:\n  name: Existing User\n",
        encoding="utf-8",
    )
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config.pop("DISABLE_FIRSTRUN_REDIRECT", None)
    with app.test_client() as client:
        yield client


def test_index_redirects_to_wizard_when_no_master(web_client_no_master):
    res = web_client_no_master.get("/")
    assert res.status_code == 302
    assert "/wizard" in res.headers["Location"]


def test_index_does_not_redirect_when_master_exists(web_client_with_master):
    res = web_client_with_master.get("/")
    assert res.status_code == 200
    body = res.get_data(as_text=True)
    # The pre-fill should carry the user's name into the master textarea.
    assert "Existing User" in body


def test_skip_wizard_query_param_bypasses_redirect(web_client_no_master):
    """A power user with no master.yaml can still hit / directly via ?skip-wizard=1."""
    res = web_client_no_master.get("/?skip-wizard=1")
    assert res.status_code == 200


def test_disable_firstrun_config_bypasses_redirect(web_client_no_master):
    from resume_builder.web import app
    app.config["DISABLE_FIRSTRUN_REDIRECT"] = True
    try:
        res = web_client_no_master.get("/")
        assert res.status_code == 200
    finally:
        app.config.pop("DISABLE_FIRSTRUN_REDIRECT", None)


# ---------- delete-my-data ----------


@pytest.fixture
def web_client_with_wipe_root(tmp_path):
    """Test client pinned at a temp dir for the delete-my-data wipe to operate on."""
    from resume_builder.web import app
    app.config["TESTING"] = True
    app.config["DELETE_MY_DATA_ROOT"] = str(tmp_path)
    app.config["WIZARD_SESSIONS_ROOT"] = str(tmp_path / "sessions")
    with app.test_client() as client:
        yield client, tmp_path
    app.config.pop("DELETE_MY_DATA_ROOT", None)
    app.config.pop("WIZARD_SESSIONS_ROOT", None)


def test_delete_my_data_wipes_sessions_directory(web_client_with_wipe_root):
    client, root = web_client_with_wipe_root
    sessions = root / "sessions"
    sessions.mkdir()
    (sessions / "abc").mkdir()
    (sessions / "abc" / "state.yaml").write_text("x: 1", encoding="utf-8")
    (sessions / "def").mkdir()
    assert sessions.exists()

    res = client.post("/api/delete-my-data")
    assert res.status_code == 200
    body = res.get_json()
    assert str(sessions) in body["removed_paths"]
    assert not sessions.exists()


def test_delete_my_data_wipes_master_yaml_backups(web_client_with_wipe_root):
    client, root = web_client_with_wipe_root
    bak_a = root / "master.yaml.bak.20240101T120000Z"
    bak_b = root / "master.yaml.bak.20240102T120000Z"
    bak_a.write_text("# old backup A")
    bak_b.write_text("# old backup B")

    res = client.post("/api/delete-my-data")
    assert res.status_code == 200
    body = res.get_json()
    assert not bak_a.exists()
    assert not bak_b.exists()
    # Both backups appear in the removed list.
    assert any("master.yaml.bak.20240101" in p for p in body["removed_paths"])
    assert any("master.yaml.bak.20240102" in p for p in body["removed_paths"])


def test_delete_my_data_wipes_drafts_directory(web_client_with_wipe_root):
    client, root = web_client_with_wipe_root
    drafts = root / "drafts"
    drafts.mkdir()
    (drafts / "x.json").write_text("{}", encoding="utf-8")

    res = client.post("/api/delete-my-data")
    assert res.status_code == 200
    assert not drafts.exists()


def test_delete_my_data_preserves_current_master_yaml(web_client_with_wipe_root):
    """The user's CURRENT master.yaml stays put — only backups are wiped."""
    client, root = web_client_with_wipe_root
    current = root / "master.yaml"
    current.write_text("# current master\n", encoding="utf-8")
    (root / "master.yaml.bak.20240101T120000Z").write_text("# backup")

    res = client.post("/api/delete-my-data")
    assert res.status_code == 200
    assert current.exists(), "delete-my-data must not touch the current master.yaml"
    assert current.read_text(encoding="utf-8") == "# current master\n"


def test_delete_my_data_safe_when_nothing_to_delete(web_client_with_wipe_root):
    """Wipe must succeed (200) even when none of the target paths exist."""
    client, _root = web_client_with_wipe_root
    res = client.post("/api/delete-my-data")
    assert res.status_code == 200
    body = res.get_json()
    assert body["removed_count"] == 0
    assert body["errors"] == []


def test_delete_my_data_reports_count_and_paths(web_client_with_wipe_root):
    client, root = web_client_with_wipe_root
    (root / "sessions").mkdir()
    (root / "sessions" / "x").mkdir()
    (root / "master.yaml.bak.20240101T120000Z").write_text("x")
    (root / "drafts").mkdir()

    res = client.post("/api/delete-my-data")
    body = res.get_json()
    assert body["removed_count"] == 3
    # All three paths get reported back.
    assert len(body["removed_paths"]) == 3


# ---------- privacy UI surface ----------


def test_index_carries_privacy_footer(web_client_with_master):
    """The footer + delete-my-data button must render on the home page."""
    res = web_client_with_master.get("/")
    body = res.get_data(as_text=True)
    assert "privacy-footer" in body
    assert "Delete my data" in body
    assert "Your data stays on this machine" in body


def test_wizard_carries_privacy_footer():
    """Same footer surfaces on the wizard page — first-run users see it too."""
    from resume_builder.web import app
    with app.test_client() as client:
        res = client.get("/wizard")
        body = res.get_data(as_text=True)
        assert "privacy-footer" in body
        assert "Delete my data" in body


def test_index_carries_privacy_banner(web_client_with_master):
    """The first-load banner exists in the DOM (JS shows/hides it)."""
    res = web_client_with_master.get("/")
    body = res.get_data(as_text=True)
    assert "privacy-banner" in body


def test_local_only_pages_do_not_load_external_font_hosts(web_client_with_master):
    """The privacy promise is literal: no Google Fonts preconnects on load."""
    pages = ["/", "/about", "/reflect", "/wizard"]
    from resume_builder.web import app

    for path in pages:
        client = web_client_with_master if path == "/" else app.test_client()
        res = client.get(path)
        body = res.get_data(as_text=True)
        assert "fonts.googleapis.com" not in body
        assert "fonts.gstatic.com" not in body


# ---------- AI-connection status (adoption #4) ----------


def _force_providers(monkeypatch, *, claude=False, api=False):
    """Pin which providers report available so provider_status() is
    deterministic regardless of the test machine's claude CLI / API key."""
    from resume_builder.llm import AnthropicAPIProvider, ClaudeCodeProvider
    monkeypatch.setattr(ClaudeCodeProvider, "is_available",
                        classmethod(lambda cls: claude))
    monkeypatch.setattr(AnthropicAPIProvider, "is_available",
                        classmethod(lambda cls: api))


def test_provider_status_claude_code(monkeypatch):
    from resume_builder.llm import provider_status
    _force_providers(monkeypatch, claude=True)
    s = provider_status()
    assert s["level"] == "ok"
    assert s["name"] == "claude-code"
    assert "Claude Code" in s["label"]


def test_provider_status_api_key(monkeypatch):
    from resume_builder.llm import provider_status
    _force_providers(monkeypatch, claude=False, api=True)
    s = provider_status()
    assert s["level"] == "ok"
    assert s["name"] == "anthropic-api"


def test_provider_status_copy_paste(monkeypatch):
    from resume_builder.llm import provider_status
    _force_providers(monkeypatch, claude=False, api=False)
    s = provider_status()
    assert s["level"] == "warn"
    assert s["name"] == "copy-paste"
    assert "Copy-paste" in s["label"]


def test_index_renders_ai_status_banner(monkeypatch, tmp_path):
    """Home page shows a live connection status, replacing the old static pill."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master.yaml").write_text(
        "schema_version: 1\nbasics:\n  name: X\n", encoding="utf-8")
    _force_providers(monkeypatch, claude=False, api=False)  # → copy-paste
    from resume_builder.web import app
    app.config["TESTING"] = True
    with app.test_client() as c:
        body = c.get("/").get_data(as_text=True)
    assert "ai-status" in body
    assert "Copy-paste mode" in body
    assert "claude CLI" not in body  # the old hardcoded pill is gone


# ---------- load sample data (adoption #4) ----------


def test_load_sample_creates_master(web_client_no_master, tmp_path):
    res = web_client_no_master.post("/api/load-sample")
    assert res.status_code == 200
    body = res.get_json()
    assert body["ok"] is True
    assert body["backup_path"] is None
    master = tmp_path / "master.yaml"
    assert master.exists()
    sample = SAMPLES_DIR / "master.example.yaml"
    assert master.read_text(encoding="utf-8") == sample.read_text(encoding="utf-8")
    load_master(master)  # must be schema-valid


def test_load_sample_backs_up_existing_master(web_client_with_master, tmp_path):
    master = tmp_path / "master.yaml"
    original = master.read_text(encoding="utf-8")
    res = web_client_with_master.post("/api/load-sample")
    assert res.status_code == 200
    assert res.get_json()["backup_path"] is not None
    backups = list(tmp_path.glob("master.yaml.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == original
    sample = SAMPLES_DIR / "master.example.yaml"
    assert master.read_text(encoding="utf-8") == sample.read_text(encoding="utf-8")


def test_wizard_page_has_sample_cta(web_client_no_master):
    body = web_client_no_master.get("/wizard").get_data(as_text=True)
    assert "btn-load-sample" in body
    assert "ai-status" in body
