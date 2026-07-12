"""Tests for cover_letter.py — parsing + guard + plain-text rendering."""

from __future__ import annotations

import json

import pytest

from resume_builder.cover_letter import (
    cover_letter_to_plain_text,
    parse_cover_letter_response_text,
    validate_cover_letter,
)
from resume_builder.loaders import load_master
from resume_builder.schema import (
    Basics,
    Bullet,
    CoverLetter,
    CoverLetterParagraph,
    Experience,
    Master,
    Pointers,
)
from pathlib import Path

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def master():
    return load_master(FIXTURES / "sample-master.yaml")


# ---------- parsing ----------


def test_parse_clean_json():
    raw = json.dumps({
        "salutation": "Dear Acme team,",
        "paragraphs": [
            {"role": "intro", "text": "Hello.", "source_ids": []},
            {"role": "why_me", "text": "I have experience.", "source_ids": ["exp-acme-1"]},
        ],
        "closing": "Sincerely,",
        "rationale": "Direct intro plus one evidence paragraph.",
    })
    cl = parse_cover_letter_response_text(raw)
    assert cl.salutation == "Dear Acme team,"
    assert len(cl.paragraphs) == 2
    assert cl.paragraphs[1].source_ids == ["exp-acme-1"]


def test_parse_handles_prose_and_fences():
    raw = """
    Here's the cover letter:

    ```json
    {"salutation": "Hi,", "paragraphs": [{"role": "intro", "text": "Yo.", "source_ids": []}]}
    ```

    Hope this helps!
    """
    cl = parse_cover_letter_response_text(raw)
    assert cl.paragraphs[0].text == "Yo."


def test_parse_invalid_json_raises():
    # Has a brace pair so the JSON-extractor finds an object, but the content
    # is not valid JSON — json.loads fails downstream.
    with pytest.raises(ValueError, match="not valid JSON"):
        parse_cover_letter_response_text("{ this is not valid json }")


# ---------- guard ----------


def test_guard_passes_honest_letter(master):
    """A cover letter referencing real bullets, using only their vocabulary, passes."""
    cl = CoverLetter(
        salutation="Dear Acme team,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm applying for the Senior role.", source_ids=[]),
            CoverLetterParagraph(role="expand", text="My background fits the engineering work.", source_ids=[]),
            CoverLetterParagraph(
                role="why_role",
                text="Your work on Acme Logistics' platform is the kind of systems problem I enjoy.",
                source_ids=[],
            ),
            CoverLetterParagraph(
                role="close",
                text="I led the dispatch rewrite from Ruby to Go, cutting p99 latency from 480ms to 95ms.",
                source_ids=["exp-acme-1"],
            ),
        ],
        closing="Sincerely,",
    )
    result = validate_cover_letter(master, cl, jd_text="Acme Logistics platform", pointers=Pointers())
    assert result.warnings == []


def test_guard_catches_invented_metric(master):
    """A cover letter that invents a number not in the source bullet must fail."""
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[
            CoverLetterParagraph(
                role="why_me",
                text="I led the dispatch rewrite from Ruby to Go, cutting latency by 99.9%.",
                source_ids=["exp-acme-1"],
            ),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="", pointers=Pointers())
    assert any("introduced number" in w.reason for w in result.warnings)
    # Either "99" or "99.9" — either signals fabrication
    assert any("99" in w.reason for w in result.warnings)


def test_guard_catches_invented_tool(master):
    """Mentioning a tool not in source, variants, OR the JD is fabrication."""
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[
            CoverLetterParagraph(
                role="why_me",
                text="I led the rewrite using Rust to cut latency to 95ms.",
                source_ids=["exp-acme-1"],  # source mentions Ruby, Go — not Rust
            ),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="", pointers=Pointers())
    assert any("rust" in w.reason.lower() for w in result.warnings)


def test_guard_unknown_source_id_flagged(master):
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[
            CoverLetterParagraph(
                role="intro",
                text="Generic prose.",
                source_ids=["exp-nonexistent-1"],
            ),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="", pointers=Pointers())
    assert any("not in master" in w.reason for w in result.warnings)


def test_guard_generic_intro_no_concrete_claims_passes(master):
    """Salutation + generic enthusiasm shouldn't trigger anything — no numbers / proper nouns."""
    cl = CoverLetter(
        salutation="Dear Acme team,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm excited to apply.", source_ids=[]),
            CoverLetterParagraph(role="expand", text="I would love to discuss this opportunity.", source_ids=[]),
            CoverLetterParagraph(role="why_role", text="Acme is solving relevant logistics problems.", source_ids=[]),
            CoverLetterParagraph(role="close", text="I will follow up next week.", source_ids=[]),
        ],
        closing="Sincerely,",
    )
    result = validate_cover_letter(master, cl, jd_text="Acme logistics problems", pointers=Pointers())
    assert result.warnings == []


def test_guard_paragraphs_kept_even_when_failing(master):
    """The cover-letter guard surfaces warnings but doesn't drop paragraphs.

    (Unlike the resume guard, which drops bullets — cover letters need to stay whole.)
    """
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[
            CoverLetterParagraph(
                role="why_me",
                text="I cut latency 99.9% (invented).",
                source_ids=["exp-acme-1"],
            ),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="", pointers=Pointers())
    assert any("introduced number" in w.reason for w in result.warnings)
    assert len(result.cleaned.paragraphs) == 1  # not dropped


def test_guard_jd_vocab_allows_jd_only_tool(master):
    """A tool mentioned in the JD (not in master) is legal vocabulary."""
    jd = "We use Datadog extensively for observability."
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[
            CoverLetterParagraph(
                role="why_role",
                text="I'm experienced with observability tools like Datadog.",
                source_ids=[],
            ),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text=jd, pointers=Pointers())
    assert not any("Datadog" in w.reason for w in result.warnings)


def test_guard_warns_on_generic_salutation(master):
    cl = CoverLetter(
        salutation="Dear Hiring Manager,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm applying.", source_ids=[]),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="", pointers=Pointers())
    assert any(w.paragraph_role == "salutation" for w in result.warnings)


def test_guard_warns_when_why_role_has_no_company_specific_fact(master):
    cl = CoverLetter(
        salutation="Dear Acme team,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm applying.", source_ids=[]),
            CoverLetterParagraph(role="expand", text="My background is relevant.", source_ids=[]),
            CoverLetterParagraph(role="why_role", text="This role sounds exciting.", source_ids=[]),
            CoverLetterParagraph(role="close", text="I will follow up.", source_ids=[]),
        ],
        closing="Best,",
    )
    jd = "Acme recently launched a logistics planning product for warehouses."
    result = validate_cover_letter(master, cl, jd_text=jd, pointers=Pointers())
    assert any("company-specific" in w.reason for w in result.warnings)


def test_guard_warns_on_wrong_paragraph_order(master):
    cl = CoverLetter(
        salutation="Dear Acme team,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm applying.", source_ids=[]),
            CoverLetterParagraph(role="why_role", text="Acme is interesting.", source_ids=[]),
        ],
        closing="Best,",
    )
    result = validate_cover_letter(master, cl, jd_text="Acme", pointers=Pointers())
    assert any(w.paragraph_role == "structure" for w in result.warnings)


# ---------- plain text rendering ----------


def test_plain_text_format(master):
    cl = CoverLetter(
        salutation="Dear hiring team,",
        paragraphs=[
            CoverLetterParagraph(role="intro", text="I'm applying.", source_ids=[]),
            CoverLetterParagraph(role="why_me", text="I have experience.", source_ids=["exp-acme-1"]),
        ],
        closing="Sincerely,",
    )
    text = cover_letter_to_plain_text(cl, master)
    assert "Jane Doe" in text  # name from master.basics
    assert "Dear hiring team," in text
    assert "I'm applying." in text
    assert "I have experience." in text
    assert "Sincerely," in text
    # Name appears twice (header + signature)
    assert text.count("Jane Doe") == 2


def test_plain_text_includes_contact_info(master):
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[CoverLetterParagraph(role="intro", text="Hi.", source_ids=[])],
        closing="Best,",
    )
    text = cover_letter_to_plain_text(cl, master)
    assert "jane@example.com" in text
    assert "Brooklyn, NY" in text
    assert "LinkedIn:" in text


def test_plain_text_handles_missing_optional_basics():
    """Master with only required fields should still render."""
    minimal_master = Master(basics=Basics(name="Test User"))
    cl = CoverLetter(
        salutation="Hi,",
        paragraphs=[CoverLetterParagraph(role="intro", text="Hello.", source_ids=[])],
        closing="Best,",
    )
    text = cover_letter_to_plain_text(cl, minimal_master)
    assert "Test User" in text
    assert "Hello." in text
