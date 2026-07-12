"""Tests for lints.lint_typos (Phase 5 spell-check)."""

from __future__ import annotations

import pytest

from resume_builder.lints import lint_typos
from resume_builder.schema import (
    Basics,
    Bullet,
    Experience,
    Master,
    Project,
    SkillGroup,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


def _master(
    *,
    company="Acme",
    role="Engineer",
    projects=("DispatchKit",),
    skills=("Postgres", "Kubernetes"),
):
    return Master(
        basics=Basics(name="T Person"),
        experience=[Experience(
            id="exp-1", company=company, role=role,
            start="2020-01", end="2024-12", bullets=[],
        )],
        projects=[Project(id=f"p-{i}", name=n, bullets=[]) for i, n in enumerate(projects)],
        skills=[SkillGroup(category="tech", items=list(skills))],
    )


def _tailored_with(bullets, summary=None):
    return TailoredResume(
        summary=summary,
        sections=[TailoredSection(
            name="experience",
            items=[TailoredItem(
                source_id="exp-1",
                bullets=[
                    TailoredBullet(source_id=f"b-{i}", rewritten_text=t)
                    for i, t in enumerate(bullets)
                ],
            )],
        )],
    )


# ---------- pyspellchecker availability ----------


def test_lint_returns_empty_when_pyspellchecker_missing(monkeypatch):
    """Lazy import gracefully degrades when the dep is absent."""
    from resume_builder import lints
    monkeypatch.setattr(lints, "_try_spellchecker", lambda: None)
    out = lint_typos(_tailored_with(["This has a definately mispelled word"]), _master())
    assert out == []


# Skip subsequent tests if pyspellchecker isn't on the dev machine
# (manual smoke / CI run installs it via requirements.txt).
pytest.importorskip("spellchecker")


# ---------- happy path ----------


def test_known_typo_is_flagged():
    out = lint_typos(_tailored_with(["This has a definately mispelled word"]), _master())
    flagged = {w.message.lower() for w in out}
    assert any("definately" in m for m in flagged)
    assert any("mispelled" in m or "misspelled" in m for m in flagged)


def test_clean_bullet_produces_no_warnings():
    out = lint_typos(
        _tailored_with([
            "Led migration of the dispatch service and reduced latency significantly",
        ]),
        _master(),
    )
    assert out == []


def test_master_company_name_is_allowlisted():
    out = lint_typos(
        _tailored_with(["Built infrastructure at Acme that powered the dispatch service"]),
        _master(company="Acme"),
    )
    flagged = {w.message.split("'")[1].lower() for w in out}
    assert "acme" not in flagged


def test_master_project_name_is_allowlisted():
    out = lint_typos(
        _tailored_with(["Shipped DispatchKit to handle the rewrite"]),
        _master(projects=("DispatchKit",)),
    )
    flagged = {w.message.split("'")[1].lower() for w in out}
    assert "dispatchkit" not in flagged


def test_master_skill_is_allowlisted():
    out = lint_typos(
        _tailored_with(["Migrated database from MySQL to Postgres for scale"]),
        _master(skills=("Postgres", "MySQL")),
    )
    flagged = {w.message.split("'")[1].lower() for w in out}
    assert "postgres" not in flagged
    assert "mysql" not in flagged


def test_acronyms_not_flagged():
    """All-caps 2-5 char tokens (AWS, SQL, CI, K8s) skip the check."""
    out = lint_typos(_tailored_with(["Deployed to AWS using SQL and CI"]), _master())
    flagged_tokens = [w.message.split("'")[1] for w in out]
    assert "AWS" not in flagged_tokens
    assert "SQL" not in flagged_tokens
    assert "CI" not in flagged_tokens


def test_short_tokens_not_flagged():
    """Tokens shorter than 3 chars are skipped (the / a / I etc.)."""
    out = lint_typos(_tailored_with(["I am at it on the day of go"]), _master())
    # "go" is 2 chars and skipped despite being a verb.
    flagged_tokens = [w.message.split("'")[1] for w in out]
    assert all(len(t) >= 3 for t in flagged_tokens)


def test_lint_typos_carries_source_id_for_diff_view():
    out = lint_typos(
        _tailored_with(["This has a definately bad spelling"]),
        _master(),
    )
    assert out
    assert all(w.source_id is not None for w in out)
    # Source id should match the TailoredBullet's source_id ("b-0").
    assert any(w.source_id == "b-0" for w in out)


def test_lint_typos_summary_checked():
    out = lint_typos(
        _tailored_with([], summary="Senior engineer with a focus on infrastrcture and reliability"),
        _master(),
    )
    flagged_tokens = [w.message.split("'")[1].lower() for w in out]
    assert "infrastrcture" in flagged_tokens


def test_lint_typos_award_allowlist_picked_up():
    """Award names + criteria on Education entries also count as allowlist."""
    from resume_builder.schema import Award, Education
    master = _master()
    master.education = [Education(
        id="e-1", school="IIT Bombay", degree="BSc CS", year="2020",
        awards=[Award(name="ICPC", criteria="top 12 globally", year="2019")],
    )]
    out = lint_typos(_tailored_with(["Won ICPC and ranked top 12 globally"]), master)
    flagged = [w.message.split("'")[1].lower() for w in out]
    assert "icpc" not in flagged


def test_lint_typos_rule_id_is_stable():
    out = lint_typos(_tailored_with(["This has a definately bad word"]), _master())
    assert all(w.rule == "typo-suspect" for w in out)


def test_lint_typos_suggestion_proposed():
    out = lint_typos(_tailored_with(["definately broken"]), _master())
    assert out
    # Should suggest the corrected spelling.
    suggestions = " ".join(w.suggestion or "" for w in out).lower()
    assert "definitely" in suggestions
