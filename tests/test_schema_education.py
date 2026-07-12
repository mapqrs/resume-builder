"""Tests for the Education status enum, Award, and TargetRole additions to schema."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from resume_builder.schema import (
    CURRENT_MASTER_SCHEMA_VERSION,
    Award,
    Basics,
    Education,
    Master,
    TargetRole,
)


# ---------- Education.status ----------


def test_education_defaults_to_graduated():
    e = Education(id="e1", school="MIT", degree="BSc CS", year="2018")
    assert e.status == "graduated"
    assert e.gpa is None
    assert e.awards == []


@pytest.mark.parametrize("status", [
    "graduated", "in_progress", "dropout", "deferred_admit",
    "rejected_admit", "on_leave", "certification_only", "online_only",
])
def test_education_accepts_each_status(status):
    e = Education(id="e1", school="X", degree="Y", year="2020", status=status)
    assert e.status == status


def test_education_rejects_unknown_status():
    with pytest.raises(ValidationError):
        Education(id="e1", school="X", degree="Y", year="2020", status="party-school")


def test_education_with_awards():
    e = Education(
        id="e1", school="MIT", degree="BSc CS", year="2018",
        gpa="3.9",
        awards=[
            Award(name="Dean's List", criteria="top 10%"),
            Award(name="Hackathon Winner"),
        ],
    )
    assert len(e.awards) == 2
    assert e.awards[0].criteria == "top 10%"
    assert e.awards[1].criteria is None


def test_education_round_trip_preserves_status_and_gpa():
    e = Education(
        id="e1", school="State U", degree="BSc", year="2010",
        status="dropout", gpa="3.2",
    )
    data = e.model_dump()
    e2 = Education.model_validate(data)
    assert e2.status == "dropout"
    assert e2.gpa == "3.2"


def test_education_legacy_yaml_loads_without_status():
    """Old master.yaml files predate the status field. They must still load."""
    data = {
        "id": "e1",
        "school": "OldU",
        "degree": "BA",
        "year": "1999",
    }
    e = Education.model_validate(data)
    assert e.status == "graduated"  # default fills in


# ---------- Award ----------


def test_award_requires_only_name():
    a = Award(name="Smith Prize")
    assert a.criteria is None
    assert a.year is None


def test_award_full():
    a = Award(name="Rhodes Scholarship", criteria="top 32 nationally", year="2018")
    assert a.year == "2018"


# ---------- TargetRole ----------


def test_target_role_minimal():
    t = TargetRole(role="Staff Engineer")
    assert t.role == "Staff Engineer"
    assert t.seniority is None
    assert t.must_include == []


def test_target_role_full():
    t = TargetRole(
        role="Staff Backend Engineer",
        seniority="staff",
        industry="fintech",
        must_include=["Postgres", "Kubernetes"],
        company_size="scaleup",
    )
    assert t.seniority == "staff"
    assert t.industry == "fintech"
    assert t.must_include == ["Postgres", "Kubernetes"]
    assert t.company_size == "scaleup"


def test_target_role_csv_must_include():
    """Mirrors Pointers behavior: a comma-separated string splits into a list."""
    t = TargetRole(role="Engineer", must_include="Postgres, Kubernetes, Go")
    assert t.must_include == ["Postgres", "Kubernetes", "Go"]


def test_target_role_rejects_unknown_seniority():
    with pytest.raises(ValidationError):
        TargetRole(role="Engineer", seniority="wizard")


def test_target_role_rejects_unknown_company_size():
    with pytest.raises(ValidationError):
        TargetRole(role="Engineer", company_size="megacorp")


# ---------- Master.schema_version ----------


def test_master_defaults_to_current_schema_version():
    m = Master(basics=Basics(name="Test"))
    assert m.schema_version == CURRENT_MASTER_SCHEMA_VERSION
    assert m.schema_version >= 1


def test_master_legacy_yaml_loads_without_schema_version():
    """Existing master.yaml files predate the field; default fills in."""
    data = {
        "basics": {"name": "Old User"},
        "experience": [],
        "projects": [],
        "education": [],
        "skills": [],
    }
    m = Master.model_validate(data)
    assert m.schema_version == CURRENT_MASTER_SCHEMA_VERSION


def test_master_preserves_explicit_schema_version():
    data = {"basics": {"name": "Future User"}, "schema_version": 2}
    m = Master.model_validate(data)
    assert m.schema_version == 2
