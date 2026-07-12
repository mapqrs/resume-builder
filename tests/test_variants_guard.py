"""The no-invention guard must accept vocabulary from `variants` as legitimate
source material — variants are alternate phrasings the candidate authored.
"""

from __future__ import annotations

from resume_builder.guard import validate
from resume_builder.schema import (
    Basics,
    Bullet,
    Experience,
    Master,
    Pointers,
    TailoredBullet,
    TailoredItem,
    TailoredResume,
    TailoredSection,
)


def _master_with_variants() -> Master:
    return Master(
        basics=Basics(name="Test User"),
        experience=[
            Experience(
                id="exp-1",
                company="Acme",
                role="Engineer",
                start="2020",
                end="2024",
                bullets=[
                    Bullet(
                        id="b1",
                        text="Led migration to Postgres, cutting query p99 from 480ms to 95ms.",
                        variants=[
                            "Drove the Postgres migration that took p99 from half a second to under 100ms.",
                            "Architected the Acme to Postgres migration, eliminating Cassandra read amplification.",
                        ],
                    ),
                ],
            )
        ],
    )


def _tailored(text: str) -> TailoredResume:
    return TailoredResume(
        sections=[
            TailoredSection(
                name="experience",
                items=[
                    TailoredItem(
                        source_id="exp-1",
                        bullets=[TailoredBullet(source_id="b1", rewritten_text=text)],
                    )
                ],
            )
        ]
    )


def test_variant_proper_noun_accepted():
    """`Cassandra` appears only in a variant, not in `text`. Guard should accept it."""
    master = _master_with_variants()
    tailored = _tailored(
        "Drove the Postgres migration off Cassandra; cut p99 from 480ms to 95ms."
    )
    result = validate(master, tailored, jd_text="", pointers=Pointers())
    assert result.warnings == []
    assert len(result.cleaned.sections[0].items[0].bullets) == 1


def test_invented_proper_noun_still_caught():
    """Guard must still catch a tool that's in NEITHER text NOR variants NOR JD."""
    master = _master_with_variants()
    tailored = _tailored(
        "Drove the Postgres migration off DynamoDB; cut p99 from 480ms to 95ms."
    )
    result = validate(master, tailored, jd_text="", pointers=Pointers())
    assert len(result.warnings) == 1
    assert "dynamodb" in result.warnings[0].reason.lower()


def test_invented_number_still_caught_even_with_variants():
    master = _master_with_variants()
    # 99 isn't in text or any variant
    tailored = _tailored("Drove migration that cut p99 by 99% to 5ms.")
    result = validate(master, tailored, jd_text="", pointers=Pointers())
    assert len(result.warnings) == 1
    assert "99" in result.warnings[0].reason
