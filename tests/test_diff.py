"""Tests for diff.py — master ↔ tailored side-by-side payload."""

from __future__ import annotations

from resume_builder.diff import build_diff
from resume_builder.guard import GuardWarning
from resume_builder.schema import (
    Basics,
    Bullet,
    Experience,
    Master,
    Project,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


def _master() -> Master:
    return Master(
        basics=Basics(name="Test User"),
        summary="Original summary.",
        experience=[
            Experience(
                id="exp-acme",
                company="Acme",
                role="Senior Engineer",
                start="2022",
                end="present",
                bullets=[
                    Bullet(id="b1", text="Led X.", tags=["backend"], impact_score=5),
                    Bullet(id="b2", text="Shipped Y.", tags=["infra"], impact_score=4),
                    Bullet(id="b3", text="Filler.", impact_score=2),
                ],
            ),
        ],
        projects=[
            Project(
                id="proj-foo",
                name="Foo",
                bullets=[Bullet(id="p1", text="Open-source thing.")],
            ),
        ],
    )


def _tailored(*, summary=None, kept_pairs=None) -> TailoredResume:
    """kept_pairs is list of (container_id, bullet_id, rewritten_text)."""
    kept_pairs = kept_pairs or []
    items_by_container: dict[str, list[TailoredBullet]] = {}
    for cid, bid, text in kept_pairs:
        items_by_container.setdefault(cid, []).append(
            TailoredBullet(source_id=bid, rewritten_text=text)
        )
    return TailoredResume(
        summary=summary,
        sections=[
            TailoredSection(
                name="experience",
                items=[
                    TailoredItem(source_id=cid, bullets=bullets)
                    for cid, bullets in items_by_container.items()
                ],
            )
        ],
    )


# ---------- core classification ----------


def test_kept_bullets_marked_kept_with_rewrite():
    m = _master()
    t = _tailored(kept_pairs=[("exp-acme", "b1", "Drove X to outcome.")])
    d = build_diff(m, t)
    bullets = d.sections[0].bullets
    by_id = {b.source_id: b for b in bullets}
    assert by_id["b1"].kind == "kept"
    assert by_id["b1"].rewritten == "Drove X to outcome."
    assert by_id["b1"].master_impact == 5
    assert by_id["b1"].master_tags == ["backend"]


def test_unsurfaced_bullets_marked_dropped():
    m = _master()
    t = _tailored(kept_pairs=[("exp-acme", "b1", "Drove X.")])
    d = build_diff(m, t)
    by_id = {b.source_id: b for b in d.sections[0].bullets}
    assert by_id["b2"].kind == "dropped"
    assert by_id["b2"].rewritten is None
    assert by_id["b3"].kind == "dropped"


def test_guard_dropped_bullets_carry_reason():
    m = _master()
    t = _tailored(kept_pairs=[("exp-acme", "b1", "Drove X.")])
    warnings = [
        GuardWarning(
            bullet_source_id="b2",
            rewritten_text="Shipped Y, saving $999K.",
            reason="introduced number(s) not in source: ['999']",
        )
    ]
    d = build_diff(m, t, guard_warnings=warnings)
    by_id = {b.source_id: b for b in d.sections[0].bullets}
    assert by_id["b2"].kind == "guard-dropped"
    assert by_id["b2"].rewritten == "Shipped Y, saving $999K."
    assert "999" in (by_id["b2"].guard_reason or "")
    # b3 not in warnings AND not in tailored → plain dropped
    assert by_id["b3"].kind == "dropped"


def test_summary_unchanged_flag():
    m = _master()
    t = _tailored(summary="Original summary.")
    d = build_diff(m, t)
    assert d.summary_master == "Original summary."
    assert d.summary_tailored == "Original summary."
    assert d.summary_changed is False


def test_summary_rewritten_flag():
    m = _master()
    t = _tailored(summary="A sharper, JD-aligned summary.")
    d = build_diff(m, t)
    assert d.summary_changed is True


def test_summary_dropped_flag():
    """Tailored summary missing while master had one — counts as changed."""
    m = _master()
    t = _tailored(summary=None)
    d = build_diff(m, t)
    assert d.summary_changed is True


# ---------- container shape ----------


def test_experience_and_projects_both_included():
    m = _master()
    t = _tailored(kept_pairs=[("exp-acme", "b1", "x")])
    d = build_diff(m, t)
    kinds = {(s.container_id, s.container_kind) for s in d.sections}
    assert ("exp-acme", "experience") in kinds
    assert ("proj-foo", "project") in kinds


def test_container_labels_formatted():
    m = _master()
    t = _tailored(kept_pairs=[])
    d = build_diff(m, t)
    labels = {s.container_id: s.container_label for s in d.sections}
    assert labels["exp-acme"] == "Senior Engineer @ Acme"
    assert labels["proj-foo"] == "Foo"


# ---------- stats ----------


def test_stats_counts():
    m = _master()
    t = _tailored(kept_pairs=[
        ("exp-acme", "b1", "rewrite of b1"),
        ("exp-acme", "b2", "rewrite of b2"),
    ])
    warnings = [
        GuardWarning(
            bullet_source_id="b3",
            rewritten_text="Filler with $999K invented.",
            reason="introduced number(s) not in source: ['999']",
        )
    ]
    d = build_diff(m, t, guard_warnings=warnings)
    # 4 master bullets total (b1, b2, b3, p1)
    assert d.stats.total == 4
    assert d.stats.kept == 2
    assert d.stats.guard_dropped == 1
    assert d.stats.dropped == 1


def test_summary_guard_warning_does_not_pollute_bullet_classification():
    """A <summary> guard warning shouldn't appear as a bullet-level guard-dropped."""
    m = _master()
    t = _tailored(kept_pairs=[("exp-acme", "b1", "x")])
    warnings = [
        GuardWarning(
            bullet_source_id="<summary>",
            rewritten_text="bad summary",
            reason="summary introduced number(s) not anywhere in master: ['42']",
        )
    ]
    d = build_diff(m, t, guard_warnings=warnings)
    # No bullet should have kind "guard-dropped"
    for s in d.sections:
        for b in s.bullets:
            assert b.kind != "guard-dropped"
    assert d.stats.guard_dropped == 0


def test_rationale_passed_through():
    m = _master()
    t = TailoredResume(
        sections=[],
        rationale="Surfaced backend bullets, dropped early-career filler.",
    )
    d = build_diff(m, t)
    assert d.rationale and "backend" in d.rationale
