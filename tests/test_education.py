"""Tests for the Phase 4 education edge cases.

Two layers of coverage:

1. `_education_line()` produces visibly distinct phrasing for each of the
   8 EducationStatus values. This is the unit-level "no graduate phrasing
   for a dropout" check.
2. The full render pipeline emits each status, gpa, and awards into the
   .docx XML without crashing or swallowing the data.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

import pytest

from resume_builder.render import _education_line, render_docx
from resume_builder.schema import (
    Award,
    Basics,
    Education,
    Master,
    Template,
)


# ---------- unit: _education_line ----------


def _edu(status, **kw):
    return Education(
        id="e1",
        school=kw.pop("school", "IIT Bombay"),
        degree=kw.pop("degree", "BSc Computer Science"),
        year=kw.pop("year", "2020"),
        status=status,
        **kw,
    )


def test_graduated_default_phrasing():
    line = _education_line(_edu("graduated"))
    assert "BSc Computer Science, IIT Bombay (2020)" in line


def test_in_progress_phrasing_signals_expected_year():
    line = _education_line(_edu("in_progress", year="2026"))
    assert "in progress" in line
    assert "expected 2026" in line


def test_dropout_phrasing_does_not_imply_graduation():
    line = _education_line(_edu("dropout", year="2018-2020"))
    assert "Attended IIT Bombay" in line
    # The naive "BSc Computer Science, IIT Bombay (year)" graduate form
    # MUST NOT appear, or it would read as a graduate.
    assert "BSc Computer Science, IIT Bombay (2018-2020)" not in line


def test_deferred_admit_phrasing():
    line = _education_line(_edu("deferred_admit", year="2020"))
    assert "Admitted to IIT Bombay" in line
    assert "deferred from 2020" in line


def test_rejected_admit_phrasing():
    line = _education_line(_edu("rejected_admit", school="Harvard College",
                                degree="AB Economics", year="2018"))
    assert "Offered admission to Harvard College" in line
    assert "declined" in line.lower()


def test_on_leave_phrasing():
    line = _education_line(_edu("on_leave", year="2018-present"))
    assert "on leave" in line


def test_certification_only_phrasing():
    line = _education_line(_edu("certification_only",
                                school="AWS",
                                degree="Solutions Architect — Associate",
                                year="2024"))
    assert "Solutions Architect — Associate, AWS (2024)" in line


def test_online_only_phrasing():
    line = _education_line(_edu("online_only", school="IIT Madras Online",
                                year="2023"))
    assert "(online)" in line
    assert "IIT Madras Online" in line


def test_gpa_appended_when_present():
    line = _education_line(_edu("graduated", gpa="CGPA 9.2/10"))
    assert "CGPA 9.2/10" in line


def test_gpa_not_appended_when_absent():
    line = _education_line(_edu("graduated"))
    # No trailing GPA marker shows up.
    assert "·" not in line  # the gpa separator


def test_location_appended_when_present():
    line = _education_line(_edu("graduated", location="Mumbai"))
    assert "Mumbai" in line


# ---------- full render: each status reaches the .docx ----------


def _master_with_education(eduset):
    return Master(
        basics=Basics(name="Test Person"),
        experience=[],
        projects=[],
        skills=[],
        education=eduset,
    )


def _doc_text(path: Path) -> str:
    """Return the inner XML text of word/document.xml for substring checks."""
    with zipfile.ZipFile(path) as z:
        return z.read("word/document.xml").decode("utf-8")


@pytest.mark.parametrize("status,marker", [
    ("graduated",        "BSc Computer Science, IIT Bombay"),
    ("in_progress",      "in progress"),
    ("dropout",          "Attended IIT Bombay"),
    ("deferred_admit",   "Admitted to IIT Bombay"),
    ("rejected_admit",   "Offered admission"),
    ("on_leave",         "on leave"),
    ("certification_only", "AWS Cert"),  # we'll pass distinct school
    ("online_only",      "(online)"),
])
def test_each_status_renders_into_docx(tmp_path, status, marker):
    if status == "certification_only":
        edu = Education(
            id="e1", school="AWS Cert", degree="Solutions Architect",
            year="2024", status=status,
        )
    else:
        edu = Education(
            id="e1", school="IIT Bombay", degree="BSc Computer Science",
            year="2020", status=status,
        )
    master = _master_with_education([edu])
    out = tmp_path / f"{status}.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert marker in text, f"status {status!r} missing marker {marker!r} in docx"


def test_awards_render_with_criteria(tmp_path):
    edu = Education(
        id="e1", school="MIT", degree="BSc CS", year="2020",
        awards=[
            Award(name="Dean's List", criteria="top 10% of cohort of 240", year="2020"),
            Award(name="Smith Prize", criteria="best thesis"),
        ],
    )
    master = _master_with_education([edu])
    out = tmp_path / "awards.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "Dean's List" in text
    assert "top 10% of cohort of 240" in text
    assert "Smith Prize" in text
    assert "best thesis" in text


def test_award_without_criteria_renders_with_warning_tag(tmp_path):
    """A trophy without context is noise — render it but mark it so the
    user notices and adds criteria during polish."""
    edu = Education(
        id="e1", school="MIT", degree="BSc CS", year="2020",
        awards=[Award(name="Some Prize")],
    )
    master = _master_with_education([edu])
    out = tmp_path / "noctx.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "Some Prize" in text
    assert "no criteria" in text  # the marker we add in render.py


def test_gpa_renders_in_docx(tmp_path):
    edu = Education(
        id="e1", school="IIT Bombay", degree="BSc CS", year="2020",
        gpa="CGPA 9.2/10",
    )
    master = _master_with_education([edu])
    out = tmp_path / "gpa.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "CGPA 9.2/10" in text


def test_legacy_education_without_status_still_renders(tmp_path):
    """Backwards-compat: master.yaml files predating the status field
    must continue to render (default = graduated)."""
    edu = Education.model_validate({
        "id": "e1",
        "school": "Old College",
        "degree": "BA",
        "year": "1999",
    })
    master = _master_with_education([edu])
    out = tmp_path / "legacy.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "BA, Old College (1999)" in text


def test_indian_percentage_gpa_round_trips(tmp_path):
    edu = Education(
        id="e1", school="St. Stephen's", degree="BA Economics", year="2018",
        gpa="78% (First class with distinction)",
    )
    master = _master_with_education([edu])
    out = tmp_path / "indian.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "78%" in text
    assert "First class with distinction" in text


# ---------- Phase 4.5: position-of-strength `reason` ----------


def test_reason_renders_inline_for_dropout():
    edu = _edu("dropout", year="2018-2020",
              reason="Left in junior year to co-found Acme — acquired 2022")
    line = _education_line(edu)
    assert "Attended IIT Bombay" in line
    assert "Left in junior year to co-found Acme" in line
    # Joiner used between metadata and the reason narrative.
    assert ";" in line


def test_reason_renders_for_rejected_admit():
    edu = _edu("rejected_admit", school="Stanford GSB", degree="MBA",
               year="2018",
               reason="Declined to scale a healthtech startup at INR 2 Cr ARR")
    line = _education_line(edu)
    assert "Offered admission to Stanford GSB" in line
    assert "Declined to scale a healthtech startup" in line


def test_reason_renders_for_on_leave():
    edu = _edu("on_leave", year="2019-present",
               reason="Founding team at Beta Labs, raised Series A")
    line = _education_line(edu)
    assert "on leave" in line
    assert "Founding team at Beta Labs" in line


def test_reason_renders_for_deferred_admit():
    edu = _edu("deferred_admit", year="2020",
               reason="Took a year at TIFR researching X")
    line = _education_line(edu)
    assert "deferred from 2020" in line
    assert "Took a year at TIFR" in line


def test_reason_optional_no_marker_when_absent():
    line = _education_line(_edu("dropout", year="2018-2020"))
    # No ";" added when reason isn't set.
    assert "Attended IIT Bombay" in line
    assert ";" not in line


def test_reason_renders_for_graduated_when_present():
    """Rare but allowed: a graduated entry with extra context."""
    edu = _edu("graduated", reason="thesis with honors, joint programme with EPFL")
    line = _education_line(edu)
    assert "BSc Computer Science, IIT Bombay (2020)" in line
    assert "thesis with honors" in line


def test_reason_legacy_yaml_loads_without_field():
    """Backwards-compat: Education entries predating Phase 4.5 still load."""
    edu = Education.model_validate({
        "id": "e1", "school": "X", "degree": "Y", "year": "2020",
    })
    assert edu.reason is None


def test_reason_renders_into_docx(tmp_path):
    edu = Education(
        id="e1", school="IIT Bombay", degree="BTech CS", year="2016-2019",
        status="dropout",
        reason="Left to co-found Acme — acquired 2022",
    )
    master = _master_with_education([edu])
    out = tmp_path / "with-reason.docx"
    render_docx(master, Template(), out)
    text = _doc_text(out)
    assert "Attended IIT Bombay" in text
    assert "Left to co-found Acme" in text
