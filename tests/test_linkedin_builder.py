"""Tests for linkedin_builder.py — section builders, anti-fab guard,
character limits, first-person register, plain-text rendering.

Live LLM access isn't reachable from pytest, so each LLM-driven section is
exercised via a fake provider that returns scripted JSON keyed by which
system prompt it sees. This keeps tests deterministic + cheap while still
covering parse → validate → render end-to-end.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from resume_builder.linkedin_builder import (
    ABOUT_SYSTEM_PROMPT,
    DEFAULT_FEATURED_MIN,
    EXPERIENCE_SYSTEM_PROMPT,
    FEATURED_SYSTEM_PROMPT,
    HEADLINE_SYSTEM_PROMPT,
    LinkedInBuildError,
    _build_skills,
    _looks_first_person,
    _truncate_to,
    build_linkedin,
    linkedin_to_plain_text,
    validate_linkedin,
)
from resume_builder.llm import LLMProvider
from resume_builder.loaders import load_master
from resume_builder.schema import (
    LINKEDIN_ABOUT_MAX,
    LINKEDIN_EXPERIENCE_DESCRIPTION_MAX,
    LINKEDIN_HEADLINE_MAX,
    Basics,
    Bullet,
    Experience,
    LinkedInEducationEntry,
    LinkedInExperienceEntry,
    LinkedInFeaturedItem,
    LinkedInProfile,
    Master,
    Pointers,
    Project,
    SkillGroup,
)


FIXTURES = Path(__file__).parent / "fixtures"


# ---------- fakes ----------


class SectionedFakeProvider(LLMProvider):
    """Returns a different scripted response for each system prompt.

    The orchestrator calls 4 prompts (headline / about / experience /
    featured) — we key responses to a substring of each so the fake can
    serve them all from one provider instance.
    """

    name = "fake-sectioned"

    @classmethod
    def is_available(cls) -> bool:
        return True

    def __init__(self, *, headline=None, about=None, experience=None, featured=None):
        self.headline = headline
        self.about = about
        self.experience = experience
        self.featured = featured
        self.calls: list[tuple[str, str]] = []

    def complete(self, system_prompt, user_message, *, model=None, timeout_s=180):
        self.calls.append((system_prompt, user_message))
        if system_prompt is HEADLINE_SYSTEM_PROMPT:
            return self.headline
        if system_prompt is ABOUT_SYSTEM_PROMPT:
            return self.about
        if system_prompt is EXPERIENCE_SYSTEM_PROMPT:
            return self.experience
        if system_prompt is FEATURED_SYSTEM_PROMPT:
            return self.featured
        raise AssertionError(
            f"Unexpected system prompt: {system_prompt[:80]!r}"
        )


@pytest.fixture
def master():
    return load_master(FIXTURES / "sample-master.yaml")


# ---------- truncate helper ----------


def test_truncate_short_text_passthrough():
    assert _truncate_to("hello", 20) == "hello"


def test_truncate_at_word_boundary():
    text = "The quick brown fox jumps over the lazy dog and runs away fast"
    out = _truncate_to(text, 25)
    assert len(out) <= 25
    assert out.endswith("…")
    # Should not split a word — the char before … is a letter, not mid-word.
    assert " " not in out[-3:-1]  # the boundary trimming worked


def test_truncate_no_good_boundary():
    """Long unbroken text — falls back to a hard cut + ellipsis."""
    text = "x" * 100
    out = _truncate_to(text, 20)
    assert len(out) == 20
    assert out.endswith("…")


# ---------- _build_skills (deterministic, no LLM) ----------


def test_build_skills_dedupes_across_groups(master):
    """Skills are flattened across groups, deduped case-insensitively,
    in master order."""
    skills, pinned = _build_skills(master)
    # 12 unique items in the fixture (4 + 5 + 3).
    assert len(skills) == 12
    assert skills[:4] == ["Go", "Python", "TypeScript", "SQL"]
    # Pinned = first 3.
    assert pinned == ["Go", "Python", "TypeScript"]


def test_build_skills_handles_empty_master():
    m = Master(basics=Basics(name="X"))
    skills, pinned = _build_skills(m)
    assert skills == []
    assert pinned == []


def test_build_skills_dedupe_case_insensitive():
    m = Master(
        basics=Basics(name="X"),
        skills=[
            SkillGroup(category="A", items=["Python", "Go"]),
            SkillGroup(category="B", items=["python", "Rust"]),  # dup
        ],
    )
    skills, _ = _build_skills(m)
    assert skills == ["Python", "Go", "Rust"]


# ---------- first-person heuristic ----------


def test_first_person_detected():
    assert _looks_first_person("I lead a backend team.")
    assert _looks_first_person("Hello — I'm an engineer.")
    assert _looks_first_person("My favorite thing is shipping.")
    assert _looks_first_person("We built distributed systems together.")


def test_third_person_not_detected():
    assert not _looks_first_person("Jane Doe is an engineer.")
    assert not _looks_first_person(
        "A backend engineer with seven years of experience building things."
    )


# ---------- build_linkedin orchestrator ----------


def _exp_payload(master):
    """Build an honest Experience response that only uses tokens from the master."""
    entries = []
    for exp in master.experience:
        # Use the first bullet's text as-is — guaranteed to be in source vocab.
        first = exp.bullets[0].text if exp.bullets else exp.company
        entries.append({
            "source_id": exp.id,
            "headline": f"{exp.role} @ {exp.company}",
            "description": first,
        })
    return json.dumps({"entries": entries})


def _featured_payload(master):
    items = []
    for p in master.projects:
        text = p.bullets[0].text if p.bullets else p.name
        items.append({
            "source_id": p.id,
            "title": p.name,
            "description": text,
            "url": p.url,
            "suggested_visual": "Screenshot of the dashboard",
        })
    return json.dumps({"items": items})


def test_build_linkedin_end_to_end_passes_guard(master):
    """Honest LLM output (grounded in master) builds a clean profile."""
    headline = "Senior Software Engineer | Backend, distributed systems, Go"
    about_paragraphs = [
        "I'm a backend engineer with seven years building distributed systems.",
        "I led the rewrite of the dispatch service from Ruby to Go, cutting p99 latency from 480ms to 95ms.",
        "I'm interested in deep systems work and developer tools.",
        "Reach out if you'd like to connect.",
    ]
    about = "\n\n".join(about_paragraphs)
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": headline}),
        about=json.dumps({"about": about}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )

    profile = build_linkedin(master, provider)

    # Sanity: every section populated.
    assert profile.headline == headline
    assert profile.about == about
    assert len(profile.experience) == len(master.experience)
    assert len(profile.featured) == len(master.projects)
    assert profile.skills  # deterministic, non-empty
    assert profile.education  # deterministic, non-empty

    # Provider was called 4 times (headline/about/experience/featured).
    assert len(provider.calls) == 4

    # Guard finds no fabrications.
    result = validate_linkedin(master, profile)
    assert result.warnings == []


# ---------- character limits ----------


def test_headline_truncated_if_llm_overshoots(master):
    """If the LLM returns a headline > LINKEDIN_HEADLINE_MAX, the builder
    truncates BEFORE the guard sees it — so the persisted profile fits.
    """
    long_headline = "A" * (LINKEDIN_HEADLINE_MAX + 50)
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": long_headline}),
        about=json.dumps({"about": "I do things."}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )
    profile = build_linkedin(master, provider)
    assert len(profile.headline) <= LINKEDIN_HEADLINE_MAX


def test_about_truncated_if_llm_overshoots(master):
    long_about = ("Word " * 700).strip()  # ~3500 chars
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": "Engineer"}),
        about=json.dumps({"about": long_about}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )
    profile = build_linkedin(master, provider)
    assert len(profile.about) <= LINKEDIN_ABOUT_MAX


def test_experience_description_truncated_if_llm_overshoots(master):
    """A single role's description ≤ LINKEDIN_EXPERIENCE_DESCRIPTION_MAX."""
    huge_desc = "I shipped things. " * 200  # ~3600 chars
    bad_entries = [{
        "source_id": master.experience[0].id,
        "headline": "Senior Engineer @ Acme",
        "description": huge_desc,
    }]
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": "Engineer"}),
        about=json.dumps({"about": "I do things."}),
        experience=json.dumps({"entries": bad_entries}),
        featured=_featured_payload(master),
    )
    profile = build_linkedin(master, provider)
    assert profile.experience
    for entry in profile.experience:
        assert len(entry.description) <= LINKEDIN_EXPERIENCE_DESCRIPTION_MAX


# ---------- guard: invented numbers / proper nouns ----------


def test_guard_catches_invented_number_in_headline(master):
    """A headline that invents a percentage not in any master bullet must fail."""
    profile = LinkedInProfile(
        headline="Senior Engineer who cut latency by 99.9% at FantasyCorp",
        about="I do things.",
    )
    result = validate_linkedin(master, profile)
    headline_warnings = [w for w in result.warnings if w.section == "headline"]
    assert any("99" in w.reason for w in headline_warnings)


def test_guard_catches_invented_tool_in_about(master):
    """An About section that mentions a tool not in the master fails."""
    profile = LinkedInProfile(
        headline="Engineer",
        about="I'm a backend engineer who built things with Snowflake and Datadog.",
    )
    result = validate_linkedin(master, profile)
    about_warnings = [w for w in result.warnings if w.section == "about"]
    assert any(
        "proper noun" in w.reason and ("snowflake" in w.reason.lower() or "datadog" in w.reason.lower())
        for w in about_warnings
    )


def test_guard_catches_invented_number_in_experience(master):
    """A role description that claims a number not in that role's bullets fails."""
    target_exp = master.experience[0]
    profile = LinkedInProfile(
        headline="Engineer",
        about="I'm a backend engineer.",
        experience=[
            LinkedInExperienceEntry(
                source_id=target_exp.id,
                headline=f"{target_exp.role} @ {target_exp.company}",
                description="I scaled the system from 0 to 50 billion daily requests.",
            ),
        ],
    )
    result = validate_linkedin(master, profile)
    matching = [
        w for w in result.warnings
        if w.section == f"experience:{target_exp.id}"
    ]
    assert any("50" in w.reason for w in matching)


def test_guard_passes_when_numbers_are_in_source(master):
    """Numbers that DO appear in the role's bullets pass."""
    target_exp = master.experience[0]  # exp-acme
    profile = LinkedInProfile(
        headline="Engineer",
        about="I'm a backend engineer.",
        experience=[
            LinkedInExperienceEntry(
                source_id=target_exp.id,
                headline=f"{target_exp.role} @ {target_exp.company}",
                # 480ms, 95ms, 12M all appear in exp-acme-1's bullet text.
                description="Cut p99 latency from 480ms to 95ms across 12M daily requests.",
            ),
        ],
    )
    result = validate_linkedin(master, profile)
    role_warnings = [
        w for w in result.warnings
        if w.section == f"experience:{target_exp.id}"
    ]
    assert role_warnings == []


def test_guard_catches_unknown_source_id_in_experience(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about="I do things.",
        experience=[
            LinkedInExperienceEntry(
                source_id="exp-does-not-exist",
                headline="Foo",
                description="Bar",
            ),
        ],
    )
    result = validate_linkedin(master, profile)
    assert any(
        "not in master.experience" in w.reason
        for w in result.warnings
    )


def test_guard_catches_unknown_source_id_in_featured(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about="I do things.",
        featured=[
            LinkedInFeaturedItem(
                source_id="proj-not-real",
                title="Foo",
                description="Bar",
            ),
        ],
    )
    result = validate_linkedin(master, profile)
    assert any(
        "not in master.projects or master.experience" in w.reason
        for w in result.warnings
    )


def test_guard_catches_skill_not_in_master(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about="I do things.",
        skills=["Go", "Cobol"],  # Cobol not in master
        pinned_skills=["Go"],
    )
    result = validate_linkedin(master, profile)
    assert any(
        w.section == "skills" and "Cobol" in w.reason
        for w in result.warnings
    )


def test_guard_catches_pinned_not_in_skills(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about="I do things.",
        skills=["Go"],
        pinned_skills=["Rust"],
    )
    result = validate_linkedin(master, profile)
    assert any(
        w.section == "skills" and "not in the full skills list" in w.reason
        for w in result.warnings
    )


# ---------- guard: first-person register ----------


def test_guard_flags_third_person_about(master):
    """An About section without first-person markers gets flagged."""
    profile = LinkedInProfile(
        headline="Engineer",
        about=(
            "A backend engineer with seven years of experience building "
            "distributed systems and developer tools. Comfortable across "
            "the stack but happiest deep in the runtime."
        ),
    )
    result = validate_linkedin(master, profile)
    assert any(
        w.section == "about" and "first-person" in w.reason
        for w in result.warnings
    )


def test_guard_accepts_first_person_about(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about=(
            "I'm a backend engineer with seven years of experience building "
            "distributed systems and developer tools. I'm happiest deep in the runtime."
        ),
    )
    result = validate_linkedin(master, profile)
    about_warnings = [w for w in result.warnings if w.section == "about"]
    # Should not have the first-person warning (might have others — but not this).
    assert not any("first-person" in w.reason for w in about_warnings)


# ---------- guard: character limits surface as warnings ----------


def test_guard_warns_on_oversize_headline(master):
    profile = LinkedInProfile(
        headline="x" * (LINKEDIN_HEADLINE_MAX + 5),
        about="I do things.",
    )
    result = validate_linkedin(master, profile)
    assert any(
        w.section == "headline" and "exceeds LinkedIn's" in w.reason
        for w in result.warnings
    )


# ---------- parsing: malformed responses ----------


def test_build_raises_on_missing_headline_key(master):
    provider = SectionedFakeProvider(
        headline=json.dumps({"not_headline": "oops"}),
        about=json.dumps({"about": "I do things."}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )
    with pytest.raises(LinkedInBuildError, match="empty headline"):
        build_linkedin(master, provider)


def test_build_raises_on_garbage_response(master):
    provider = SectionedFakeProvider(
        headline="this is not json at all, no braces anywhere",
        about=json.dumps({"about": "x"}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )
    with pytest.raises(Exception):  # ValueError from _extract_first_json_object
        build_linkedin(master, provider)


def test_build_tolerates_fences_and_prose(master):
    """LLM wrapped JSON in markdown fences with prose — parser still works."""
    headline_resp = (
        "Sure, here's the headline:\n\n```json\n"
        + json.dumps({"headline": "Senior Backend Engineer"})
        + "\n```\n\nLet me know if you'd like tweaks!"
    )
    provider = SectionedFakeProvider(
        headline=headline_resp,
        about=json.dumps({"about": "I do things."}),
        experience=_exp_payload(master),
        featured=_featured_payload(master),
    )
    profile = build_linkedin(master, provider)
    assert profile.headline == "Senior Backend Engineer"


# ---------- experience: ignores unknown source_ids ----------


def test_build_drops_experience_with_unknown_source_id(master):
    bad_entries = [
        {
            "source_id": "exp-does-not-exist",
            "headline": "Fake @ Nowhere",
            "description": "Did fake things.",
        },
        {
            "source_id": master.experience[0].id,
            "headline": "Real",
            "description": "Real work.",
        },
    ]
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": "Engineer"}),
        about=json.dumps({"about": "I do things."}),
        experience=json.dumps({"entries": bad_entries}),
        featured=_featured_payload(master),
    )
    profile = build_linkedin(master, provider)
    assert [e.source_id for e in profile.experience] == [master.experience[0].id]


# ---------- featured: URL fallback to master ----------


def test_featured_falls_back_to_master_url_when_llm_omits(master):
    """If master has a URL for the project but the LLM returned null, use master's."""
    items = [{
        "source_id": master.projects[0].id,
        "title": master.projects[0].name,
        "description": master.projects[0].bullets[0].text,
        "url": None,
        "suggested_visual": None,
    }]
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": "Engineer"}),
        about=json.dumps({"about": "I do things."}),
        experience=_exp_payload(master),
        featured=json.dumps({"items": items}),
    )
    profile = build_linkedin(master, provider)
    assert profile.featured
    assert profile.featured[0].url == master.projects[0].url


# ---------- featured: caps at DEFAULT_FEATURED_MAX ----------


def test_featured_caps_at_max(master):
    """If LLM returns more than DEFAULT_FEATURED_MAX items, we trim."""
    # Use a master with many projects.
    big_master = Master(
        basics=Basics(name="X"),
        projects=[
            Project(id=f"proj-{i}", name=f"Project {i}",
                    bullets=[Bullet(id=f"proj-{i}-b1", text=f"Did thing {i}")])
            for i in range(10)
        ],
    )
    items = [
        {
            "source_id": p.id,
            "title": p.name,
            "description": p.bullets[0].text,
            "url": None,
            "suggested_visual": None,
        }
        for p in big_master.projects
    ]
    provider = SectionedFakeProvider(
        headline=json.dumps({"headline": "Engineer"}),
        about=json.dumps({"about": "I do things."}),
        experience=json.dumps({"entries": []}),
        featured=json.dumps({"items": items}),
    )
    profile = build_linkedin(big_master, provider)
    assert len(profile.featured) <= 5


# ---------- plain-text rendering ----------


def test_plain_text_contains_all_section_headers(master):
    profile = LinkedInProfile(
        headline="Engineer",
        about="I do things.",
        experience=[
            LinkedInExperienceEntry(
                source_id=master.experience[0].id,
                headline="Senior Engineer @ Acme",
                description="Did backend work.",
            ),
        ],
        skills=["Go", "Python"],
        pinned_skills=["Go"],
        featured=[
            LinkedInFeaturedItem(
                source_id=master.projects[0].id,
                title="pgwatch-lite",
                description="Open source dashboard.",
                url="https://github.com/janedoe/pgwatch-lite",
                suggested_visual="Screenshot",
            ),
        ],
        education=[
            LinkedInEducationEntry(
                source_id=master.education[0].id,
                school="University of Michigan",
                degree="B.S. Computer Science",
                year="2017",
            ),
        ],
    )
    text = linkedin_to_plain_text(profile, master)
    assert "## Headline" in text
    assert "## About" in text
    assert "## Experience" in text
    assert "## Featured" in text
    assert "## Skills" in text
    assert "## Education" in text
    # Pinned skills surfaced separately.
    assert "Pin these top 3" in text
    # URL + visual hint included for featured items.
    assert "https://github.com/janedoe/pgwatch-lite" in text
    assert "Screenshot" in text


def test_plain_text_renders_minimal_profile():
    """A profile with only headline + about (no experience, etc.) renders cleanly."""
    m = Master(basics=Basics(name="Bare User"))
    profile = LinkedInProfile(headline="x", about="I'm new here.")
    text = linkedin_to_plain_text(profile, m)
    assert "Bare User" in text
    assert "## Headline" in text
    assert "## About" in text
    # No experience / featured / skills / education sections present.
    assert "## Experience" not in text
    assert "## Featured" not in text


# ---------- no LinkedIn API integration ----------


def test_module_does_not_import_linkedin_api():
    """Phase 6.5 is explicitly copy-paste only — no LinkedIn API client.
    Guard against accidentally pulling in a linkedin/oauth dependency."""
    import resume_builder.linkedin_builder as mod
    text = Path(mod.__file__).read_text()
    # No `import linkedin`, no `requests.post` to LinkedIn endpoints, etc.
    assert "linkedin.com/v" not in text.lower()
    assert "import linkedin" not in text.lower()
    assert "oauth" not in text.lower()
