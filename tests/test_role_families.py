"""Tests for the role-family registry."""

from __future__ import annotations

from resume_builder import role_families


def test_all_families_returns_a_list():
    families = role_families.all_families()
    assert isinstance(families, list)
    assert len(families) >= 10  # we ship a curated list, not a stub


def test_every_family_has_id_label_blurb():
    for rf in role_families.all_families():
        assert rf.id and isinstance(rf.id, str)
        assert rf.label and isinstance(rf.label, str)
        assert rf.blurb and isinstance(rf.blurb, str)


def test_role_family_ids_are_unique():
    ids = [rf.id for rf in role_families.all_families()]
    assert len(ids) == len(set(ids))


def test_other_is_always_present_as_escape_hatch():
    assert role_families.by_id("other") is not None


def test_india_critical_families_are_present():
    """The wizard targets the Indian subcontinent — these families must exist."""
    ids = {rf.id for rf in role_families.all_families()}
    for required in (
        "software-engineering",
        "finance-accounting",  # CA / CFA / IB
        "civil-services-government",  # IAS / IPS / Bank PO
        "healthcare-clinical",
        "consulting-strategy",
        "education-teaching",
        "creative-media",
        "non-profit-social",
        "devrel-community",  # emerging
        "data-and-ai",  # emerging
    ):
        assert required in ids, f"missing role family: {required}"


def test_by_id_unknown_returns_none():
    assert role_families.by_id("not-a-real-family") is None


def test_is_known():
    assert role_families.is_known("software-engineering") is True
    assert role_families.is_known("other") is True
    assert role_families.is_known("not-a-real-family") is False
    assert role_families.is_known(None) is False
