from resume_builder.bock_tier import classify_bullet
from resume_builder.lints import lint_bock_tier
from resume_builder.schema import TailoredBullet, TailoredItem, TailoredResume, TailoredSection


def _resume(*texts: str) -> TailoredResume:
    return TailoredResume(
        sections=[
            TailoredSection(
                name="experience",
                items=[
                    TailoredItem(
                        source_id="exp-1",
                        bullets=[
                            TailoredBullet(source_id=f"b{i}", rewritten_text=t)
                            for i, t in enumerate(texts, start=1)
                        ],
                    )
                ],
            )
        ]
    )


def test_original_missing_metric_from_bock_budget_example():
    tier, missing = classify_bullet("Managed sorority budget.")
    assert tier == "original"
    assert "y_metric" in missing
    assert "z_method" in missing


def test_better_has_metric_but_no_method():
    tier, missing = classify_bullet(
        "Negotiated 30% ($500k) reduction in costs with Salesforce for post-delivery support."
    )
    assert tier == "better"
    assert missing == ["z_method"]


def test_awesome_has_metric_and_method():
    tier, missing = classify_bullet(
        "Negotiated 30% ($500k) reduction with Salesforce for post-delivery support "
        "by designing and using results from an online auction of multiple vendors."
    )
    assert tier == "awesome"
    assert missing == []


def test_weak_opener_stays_original_even_with_metric():
    tier, missing = classify_bullet("Responsible for managing $31,000 Spring 2026 budget.")
    assert tier == "original"
    assert "x_strong_verb" in missing


def test_bock_tier_lint_silences_awesome_bullets():
    warnings = lint_bock_tier(
        _resume(
            "Negotiated 30% ($500k) reduction with Salesforce by running an online auction.",
            "Managed budget.",
        )
    )
    assert [w.source_id for w in warnings] == ["b2"]
    assert warnings[0].rule == "bock-tier"
