"""Pointer merging and CLI argument plumbing."""

from __future__ import annotations

from pathlib import Path

import pytest

from resume_builder.cli import _build_arg_parser, _merge_pointers
from resume_builder.loaders import apply_auto_length, load_pointers, years_of_experience
from resume_builder.schema import Basics, Experience, Master, Pointers


FIXTURES = Path(__file__).parent / "fixtures"


def test_pointers_default_empty():
    p = Pointers()
    assert p.length is None
    assert p.must_include == []


def test_must_include_csv_split_via_validator():
    p = Pointers.model_validate({"must_include": "Kubernetes, Postgres ,observability"})
    assert p.must_include == ["Kubernetes", "Postgres", "observability"]


def test_cli_flags_override_file_pointers(tmp_path):
    # File pointers
    pointers_yaml = tmp_path / "pointers.yaml"
    pointers_yaml.write_text(
        "length: 2page\nseniority: senior\nmust_include: [Foo, Bar]\ncontext: faang\n"
    )
    file_pointers = load_pointers(pointers_yaml)
    assert file_pointers.length == "2page"
    assert file_pointers.seniority == "senior"

    # Simulate CLI args overriding everything
    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--master",
            "/dev/null",
            "--out",
            "/tmp/x.docx",
            "--length",
            "1page",
            "--seniority",
            "staff",
            "--must-include",
            "Kubernetes,Postgres",
            "--context",
            "startup",
            "--no-tailor",
        ]
    )
    merged = _merge_pointers(file_pointers, args)
    assert merged.length == "1page"
    assert merged.seniority == "staff"
    assert merged.must_include == ["Kubernetes", "Postgres"]
    assert merged.context == "startup"


def test_cli_flags_partial_override(tmp_path):
    pointers_yaml = tmp_path / "pointers.yaml"
    pointers_yaml.write_text("length: 2page\nseniority: senior\n")
    file_pointers = load_pointers(pointers_yaml)

    parser = _build_arg_parser()
    args = parser.parse_args(
        [
            "--master",
            "/dev/null",
            "--out",
            "/tmp/x.docx",
            "--seniority",
            "staff",
            "--no-tailor",
        ]
    )
    merged = _merge_pointers(file_pointers, args)
    assert merged.length == "2page"  # from file, not overridden
    assert merged.seniority == "staff"  # overridden by CLI


def test_pointers_yaml_loader_handles_empty_file(tmp_path):
    pointers_yaml = tmp_path / "empty.yaml"
    pointers_yaml.write_text("")
    p = load_pointers(pointers_yaml)
    assert p == Pointers()


def test_auto_length_uses_one_page_under_a_decade():
    master = Master(
        basics=Basics(name="Test"),
        experience=[Experience(id="e1", company="A", role="Eng", start="2022-01", end="2024-01")],
    )
    assert years_of_experience(master) == 2
    assert apply_auto_length(master, Pointers()).length == "1page"


def test_auto_length_uses_two_pages_at_decade():
    master = Master(
        basics=Basics(name="Test"),
        experience=[Experience(id="e1", company="A", role="Eng", start="2010-01", end="2020-01")],
    )
    assert apply_auto_length(master, Pointers(length="auto")).length == "2page"


# ---------- extra_instructions (adoption A2) ----------


def test_extra_instructions_trims_and_empties_to_none():
    assert Pointers.model_validate({"extra_instructions": "  "}).extra_instructions is None
    assert Pointers.model_validate(
        {"extra_instructions": "  British English  "}
    ).extra_instructions == "British English"


def test_extra_instructions_caps_length():
    p = Pointers.model_validate({"extra_instructions": "x" * 5000})
    assert len(p.extra_instructions) == 2000


def test_extra_instructions_merges_like_other_pointers(tmp_path):
    from resume_builder.loaders import pointers_from_dict

    base = Pointers(extra_instructions="from file")
    # None/missing → base wins
    assert pointers_from_dict(base, {}).extra_instructions == "from file"
    # Override wins
    assert pointers_from_dict(
        base, {"extra_instructions": "from CLI"}
    ).extra_instructions == "from CLI"


def test_cli_extra_instructions_flag_merges():
    parser = _build_arg_parser()
    args = parser.parse_args(
        ["--master", "/dev/null", "--out", "/tmp/x.docx", "--no-tailor",
         "--extra-instructions", "emphasize leadership"]
    )
    merged = _merge_pointers(Pointers(), args)
    assert merged.extra_instructions == "emphasize leadership"


def test_extra_instructions_lands_in_both_prompts():
    from resume_builder.loaders import load_master
    from resume_builder.prompts import (
        build_cover_letter_user_message,
        build_user_message,
    )

    master = load_master(FIXTURES / "sample-master.yaml")
    pointers = Pointers(extra_instructions="British English; formal tone")
    resume_msg = build_user_message(master, "some JD text", pointers)
    cover_msg = build_cover_letter_user_message(master, "some JD text", pointers)
    for msg in (resume_msg, cover_msg):
        assert "British English; formal tone" in msg
        # The honesty fence rides along with the instruction.
        assert "HARD RULES above always win" in msg

    # Absent → the fence line doesn't appear at all.
    clean = build_user_message(master, "some JD text", Pointers())
    assert "Extra instructions from the candidate" not in clean


def test_web_prompt_endpoint_carries_extra_instructions():
    from resume_builder.web import app

    app.config["TESTING"] = True
    master_yaml = (FIXTURES / "sample-master.yaml").read_text()
    with app.test_client() as client:
        res = client.post("/api/prompt", data={
            "master_yaml": master_yaml,
            "jd_text": "We need a backend engineer who knows Postgres.",
            "extra_instructions": "more formal tone",
        })
        assert res.status_code == 200
        assert "more formal tone" in res.get_data(as_text=True)


# ---------- save-defaults round-trip (adoption A3) ----------


@pytest.fixture
def defaults_client(tmp_path, monkeypatch):
    """Test client chdir'd to a tmp dir that has a master.yaml (so `/` renders)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "master.yaml").write_text(
        "schema_version: 1\nbasics:\n  name: X\n", encoding="utf-8"
    )
    from resume_builder.web import app
    app.config["TESTING"] = True
    with app.test_client() as client:
        yield client


def test_save_defaults_writes_pointers_yaml(defaults_client, tmp_path):
    res = defaults_client.post("/api/save-defaults", data={
        "length": "2page",
        "seniority": "staff",
        "context": "startup",
        "must_include": "Kubernetes, Postgres",
        "extra_instructions": "British English",
    })
    assert res.status_code == 200
    saved = load_pointers(tmp_path / "pointers.yaml")
    assert saved.length == "2page"
    assert saved.seniority == "staff"
    assert saved.context == "startup"
    assert saved.must_include == ["Kubernetes", "Postgres"]
    assert saved.extra_instructions == "British English"


def test_save_defaults_rejects_bad_enum(defaults_client, tmp_path):
    res = defaults_client.post("/api/save-defaults", data={"seniority": "wizard-king"})
    assert res.status_code == 400
    assert not (tmp_path / "pointers.yaml").exists()


def test_index_prefills_saved_defaults(defaults_client, tmp_path):
    defaults_client.post("/api/save-defaults", data={
        "length": "1page", "extra_instructions": "formal tone",
    })
    body = defaults_client.get("/").get_data(as_text=True)
    assert "pointerDefaults" in body
    assert "formal tone" in body


def test_index_survives_malformed_pointers_yaml(defaults_client, tmp_path):
    (tmp_path / "pointers.yaml").write_text("seniority: [not, valid", encoding="utf-8")
    res = defaults_client.get("/")
    assert res.status_code == 200
