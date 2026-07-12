"""LinkedIn profile builder — first-class output alongside the resume + cover letter.

Each LinkedIn section gets its own crafted LLM call. LinkedIn is a different
medium from a resume — longer-form, first-person in the About block,
keyword-rich, no strict page limit — so we do not reuse the tailor's bullets
verbatim. The builder rewrites them with room for nuance.

Sections produced
-----------------
- ``headline``     — ≤220 chars. Role + value prop + 1-2 keywords.
- ``about``        — ≤2000 chars. First-person narrative: hook → 2-3 proof
                     points (drawn from highest-impact bullets) → what
                     you're looking for → CTA.
- ``experience``   — per-role. ``description`` is the longer-form LinkedIn
                     body, rewritten from each role's bullets.
- ``skills``       — deterministic. Ordered list of every master skill;
                     ``pinned_skills`` carries the top 3 for LinkedIn's
                     "Top skills" feature.
- ``featured``     — 3-5 portfolio items pulled from master.projects.
- ``education``    — pass-through from master.education with the LinkedIn shape.

Anti-fabrication guard
----------------------

The same moat as the resume tailor and cover-letter guard: every concrete
claim (numbers, proper nouns) must trace to a master bullet's text/variants.
``validate_linkedin`` enforces this section-by-section using the existing
guard helpers from ``guard.py``. Failures don't drop the section — they
surface warnings the user can act on, the same way the cover-letter guard
behaves.

Output
------

``linkedin_to_plain_text`` renders the whole profile as plain-text blocks
the user copy-pastes into LinkedIn's edit screens. No LinkedIn API
integration — copy-paste is the universal path.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

from .guard import (
    _COMMON_STARTERS,
    _PROPER_NOUN_RE,
    _build_pointer_vocab,
    _check_bullet,
)
from .llm import LLMError, LLMProvider
from .schema import (
    LINKEDIN_ABOUT_MAX,
    LINKEDIN_EXPERIENCE_DESCRIPTION_MAX,
    LINKEDIN_HEADLINE_MAX,
    LinkedInEducationEntry,
    LinkedInExperienceEntry,
    LinkedInFeaturedItem,
    LinkedInProfile,
    Master,
    Pointers,
)
from .tailor import _extract_first_json_object


DEFAULT_TIMEOUT_S = 180
DEFAULT_PINNED_SKILL_COUNT = 3
DEFAULT_FEATURED_MIN = 3
DEFAULT_FEATURED_MAX = 5


class LinkedInBuildError(LLMError):
    """Raised when an LLM section response can't be parsed into the LinkedIn shape."""


# ---------- system prompts ----------

# Each section has its own short prompt. The hard rules are the same as the
# resume tailor's — never invent numbers or proper nouns — restated up front
# because LinkedIn is a different medium and the model needs to be reminded.

_NO_INVENTION_RULES = """\
HARD RULES — VIOLATING THESE IS A BUG:
1. NEVER invent experience, employers, projects, metrics, technologies,
   or named people that are not in the master JSON provided.
2. NEVER introduce a number (year, percent, dollar amount, count, duration,
   team size) that is not present in the master's bullet text or variants.
3. NEVER introduce a proper noun (product, tool, company, framework) that
   is not present in the master.
4. Generic English vocabulary, role titles ("engineer", "manager"), and
   neutral LinkedIn idiom ("connect", "team", "passionate about ...") are fine.
5. If you cannot meet the character limit without inventing, write a shorter
   block. A short truthful block is better than a long fabricated one."""


HEADLINE_SYSTEM_PROMPT = f"""\
You write the candidate's LinkedIn HEADLINE — the one-line tagline that
appears under their name. LinkedIn's search ranking weighs the headline
heavily, so pack 1-2 relevant keywords in alongside the role and value prop.

{_NO_INVENTION_RULES}

LENGTH: at most {LINKEDIN_HEADLINE_MAX} characters. Aim for 120-180 characters
so it renders well on mobile.

STYLE:
- One line. No line breaks.
- Lead with the role or strongest positioning ("Senior Backend Engineer", "Product Manager turned Founder").
- Avoid clichés: "passionate", "results-driven", "rockstar", "ninja", "guru", "thought leader".
- Use ` | ` (space-pipe-space) or ` · ` (space-middot-space) to separate clauses.
- Do not include the candidate's name — that's already on the profile.

OUTPUT: Return ONLY valid JSON conforming to the schema:
{{"headline": "<the one-line headline>"}}
No prose before or after. No markdown fences."""


ABOUT_SYSTEM_PROMPT = f"""\
You write the candidate's LinkedIn ABOUT section. This is a FIRST-PERSON
narrative — write as the candidate, in the first person ("I lead", "I built",
"I'm interested in").

{_NO_INVENTION_RULES}

LENGTH: at most {LINKEDIN_ABOUT_MAX} characters including whitespace.
Aim for 800-1500 characters — long enough to tell a story, short enough that
people read it.

STRUCTURE (4 short paragraphs, blank line between each):
1. Hook (1-2 sentences). Who the candidate is and what they care about.
2. Proof (2-4 sentences). 2-3 specific, grounded accomplishments drawn
   from the highest-impact bullets in the master. Numbers and tools allowed
   only if they appear verbatim in the master.
3. Direction (1-2 sentences). What kind of problem or role they're looking
   for next — drawn from the master summary if present, otherwise
   inferred conservatively from the bullet content.
4. Call to action (1 sentence). Invite people to connect / message.

STYLE:
- First person throughout ("I", "my", "I've"). Never third person.
- Active voice. Strong verbs.
- Avoid clichés: "passionate", "results-driven", "synergy", "team player", "rockstar".
- No bullet lists — LinkedIn renders them poorly in the About section.

OUTPUT: Return ONLY valid JSON conforming to the schema:
{{"about": "<the about-section text, with \\n\\n between paragraphs>"}}
No prose before or after. No markdown fences."""


EXPERIENCE_SYSTEM_PROMPT = f"""\
You write the candidate's LinkedIn EXPERIENCE section. Each role gets a
short headline plus a longer description that rewrites the resume bullets
with more room for context. LinkedIn rewards detail — feel free to expand
each bullet into a sentence or two, but never beyond what the source supports.

{_NO_INVENTION_RULES}

LENGTH: each description ≤ {LINKEDIN_EXPERIENCE_DESCRIPTION_MAX} characters.
Aim for 400-1200 characters per role.

STRUCTURE per role:
- ``headline``: one line, "<Role> @ <Company>".
- ``description``: the rewritten bullets. Use newlines between bullets.
  Optionally lead with one sentence of role context (scope, team, mandate).
  Each bullet starts with a strong verb and follows Bock's XYZ structure
  ("Accomplished X as measured by Y by doing Z") in a longer form than the
  resume.

STYLE:
- Active voice. Strong verbs. Vary verbs across bullets.
- Avoid clichés.
- Numbers and proper nouns only if they appear in the master bullet's
  ``text`` or ``variants``.

OUTPUT: Return ONLY valid JSON conforming to the schema:
{{"entries": [
  {{"source_id": "<master Experience.id>",
    "headline": "<one-line role headline>",
    "description": "<longer LinkedIn description>"}},
  ...
]}}
Include one entry for each Experience in the master, in the master's order.
No prose before or after. No markdown fences."""


FEATURED_SYSTEM_PROMPT = f"""\
You pick the candidate's LinkedIn FEATURED items — 3 to 5 portfolio-worthy
entries drawn from the master's projects (and, if there aren't enough strong
projects, the most portfolio-worthy bullets from experience).

{_NO_INVENTION_RULES}

For each item, write:
- ``title``: the short label (≤80 chars).
- ``description``: 1-3 sentences explaining what it is and why it matters.
- ``url``: copy from master if the project has one; otherwise null.
- ``suggested_visual``: a short free-form hint to the user about what to
   attach as the Featured tile's visual ("screenshot of the dashboard",
   "link to the OSS repo", "demo video"). Optional.

PICK: {DEFAULT_FEATURED_MIN}-{DEFAULT_FEATURED_MAX} items. Bias toward
projects with URLs and high-impact bullets. If the master has fewer than
{DEFAULT_FEATURED_MIN} projects, emit what's available (do not pad).

OUTPUT: Return ONLY valid JSON conforming to the schema:
{{"items": [
  {{"source_id": "<master Project.id or Experience.id>",
    "title": "<title>",
    "description": "<1-3 sentences>",
    "url": "<url or null>",
    "suggested_visual": "<hint or null>"}},
  ...
]}}
No prose before or after. No markdown fences."""


# ---------- master serialisation ----------


def _master_for_prompt(master: Master) -> dict:
    """Compact master representation tuned for LinkedIn builder prompts."""
    return {
        "summary": master.summary,
        "experience": [
            {
                "id": exp.id,
                "company": exp.company,
                "role": exp.role,
                "start": exp.start,
                "end": exp.end,
                "location": exp.location,
                "bullets": [
                    {
                        "id": b.id,
                        "text": b.text,
                        "tags": b.tags,
                        "impact_score": b.impact_score,
                        "variants": b.variants,
                    }
                    for b in exp.bullets
                ],
            }
            for exp in master.experience
        ],
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "url": p.url,
                "bullets": [
                    {
                        "id": b.id,
                        "text": b.text,
                        "tags": b.tags,
                        "variants": b.variants,
                    }
                    for b in p.bullets
                ],
            }
            for p in master.projects
        ],
        "skills": [
            {"category": g.category, "items": g.items}
            for g in master.skills
        ],
    }


def _master_blob(master: Master) -> str:
    """JSON blob the section prompts share."""
    return json.dumps(_master_for_prompt(master), indent=2)


# ---------- JSON parsing helper ----------


def _parse_section_json(raw: str) -> dict:
    """Pull the first JSON object out of an LLM reply. Tolerates fences/prose."""
    payload_text = _extract_first_json_object(raw)
    try:
        data = json.loads(payload_text)
    except json.JSONDecodeError as e:
        raise LinkedInBuildError(
            f"LinkedIn section: response was not valid JSON ({e}). "
            f"First 500 chars:\n{raw[:500]}"
        ) from e
    if not isinstance(data, dict):
        raise LinkedInBuildError(
            f"LinkedIn section: expected JSON object, got {type(data).__name__}"
        )
    return data


# ---------- section builders ----------


def _build_headline(master: Master, provider: LLMProvider,
                    *, model: Optional[str], timeout_s: int) -> str:
    user_msg = (
        f"# Master resume (JSON; source of truth)\n\n"
        f"```json\n{_master_blob(master)}\n```\n\n"
        f"Write the headline. Return JSON only."
    )
    raw = provider.complete(
        HEADLINE_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    data = _parse_section_json(raw)
    headline = (data.get("headline") or "").strip()
    if not headline:
        raise LinkedInBuildError("LLM returned an empty headline")
    return _truncate_to(headline, LINKEDIN_HEADLINE_MAX)


def _build_about(master: Master, provider: LLMProvider,
                 *, model: Optional[str], timeout_s: int) -> str:
    user_msg = (
        f"# Master resume (JSON; source of truth)\n\n"
        f"```json\n{_master_blob(master)}\n```\n\n"
        f"Write the About section. Return JSON only."
    )
    raw = provider.complete(
        ABOUT_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    data = _parse_section_json(raw)
    about = (data.get("about") or "").strip()
    if not about:
        raise LinkedInBuildError("LLM returned an empty About section")
    return _truncate_to(about, LINKEDIN_ABOUT_MAX)


def _build_experience(master: Master, provider: LLMProvider,
                      *, model: Optional[str], timeout_s: int,
                      ) -> List[LinkedInExperienceEntry]:
    if not master.experience:
        return []
    user_msg = (
        f"# Master resume (JSON; source of truth)\n\n"
        f"```json\n{_master_blob(master)}\n```\n\n"
        f"Write one Experience entry for each role above. Return JSON only."
    )
    raw = provider.complete(
        EXPERIENCE_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    data = _parse_section_json(raw)
    entries_raw = data.get("entries")
    if not isinstance(entries_raw, list):
        raise LinkedInBuildError("Experience response missing `entries` array")

    valid_ids = {exp.id for exp in master.experience}
    out: List[LinkedInExperienceEntry] = []
    for item in entries_raw:
        if not isinstance(item, dict):
            continue
        sid = (item.get("source_id") or "").strip()
        if sid not in valid_ids:
            continue
        headline = (item.get("headline") or "").strip()
        description = (item.get("description") or "").strip()
        if not headline or not description:
            continue
        out.append(LinkedInExperienceEntry(
            source_id=sid,
            headline=headline,
            description=_truncate_to(description, LINKEDIN_EXPERIENCE_DESCRIPTION_MAX),
        ))
    return out


def _build_featured(master: Master, provider: LLMProvider,
                    *, model: Optional[str], timeout_s: int,
                    ) -> List[LinkedInFeaturedItem]:
    if not master.projects and not master.experience:
        return []
    user_msg = (
        f"# Master resume (JSON; source of truth)\n\n"
        f"```json\n{_master_blob(master)}\n```\n\n"
        f"Pick {DEFAULT_FEATURED_MIN}-{DEFAULT_FEATURED_MAX} Featured items. "
        f"Return JSON only."
    )
    raw = provider.complete(
        FEATURED_SYSTEM_PROMPT, user_msg, model=model, timeout_s=timeout_s,
    )
    data = _parse_section_json(raw)
    items_raw = data.get("items")
    if not isinstance(items_raw, list):
        raise LinkedInBuildError("Featured response missing `items` array")

    valid_project_ids = {p.id for p in master.projects}
    valid_exp_ids = {e.id for e in master.experience}
    valid_ids = valid_project_ids | valid_exp_ids
    project_urls = {p.id: p.url for p in master.projects if p.url}

    out: List[LinkedInFeaturedItem] = []
    for item in items_raw:
        if not isinstance(item, dict):
            continue
        sid = (item.get("source_id") or "").strip()
        if sid not in valid_ids:
            continue
        title = (item.get("title") or "").strip()
        description = (item.get("description") or "").strip()
        if not title or not description:
            continue
        url_raw = item.get("url")
        url = url_raw.strip() if isinstance(url_raw, str) and url_raw.strip() else None
        # If the master has a URL and the LLM omitted one, fall back to master.
        if not url and sid in project_urls:
            url = project_urls[sid]
        suggested_raw = item.get("suggested_visual")
        suggested = (
            suggested_raw.strip()
            if isinstance(suggested_raw, str) and suggested_raw.strip()
            else None
        )
        out.append(LinkedInFeaturedItem(
            source_id=sid,
            title=title,
            description=description,
            url=url,
            suggested_visual=suggested,
        ))
        if len(out) >= DEFAULT_FEATURED_MAX:
            break
    return out


def _build_skills(master: Master) -> Tuple[List[str], List[str]]:
    """Deterministic — no LLM call. Skills come from master.skills, deduped,
    preserving the user's authored order. Top ``DEFAULT_PINNED_SKILL_COUNT``
    items are the pinned set.
    """
    seen: set[str] = set()
    ordered: List[str] = []
    for group in master.skills:
        for item in group.items:
            cleaned = item.strip()
            if not cleaned:
                continue
            key = cleaned.lower()
            if key in seen:
                continue
            seen.add(key)
            ordered.append(cleaned)
    pinned = ordered[:DEFAULT_PINNED_SKILL_COUNT]
    return ordered, pinned


def _build_education(master: Master) -> List[LinkedInEducationEntry]:
    """Pass-through from master.education to the LinkedIn shape."""
    out: List[LinkedInEducationEntry] = []
    for edu in master.education:
        out.append(LinkedInEducationEntry(
            source_id=edu.id,
            school=edu.school,
            degree=edu.degree,
            year=edu.year,
            notes=edu.notes,
        ))
    return out


def _truncate_to(text: str, limit: int) -> str:
    """Truncate text to ``limit`` characters at a word boundary if possible.

    LinkedIn cuts at the character limit silently, which produces ugly mid-word
    cuts. We pre-truncate at the last whitespace before the cap so the user's
    block fits cleanly. Adds a `…` if we trimmed.
    """
    if len(text) <= limit:
        return text
    # Try to cut at the last whitespace before the cap minus 1 for the ellipsis.
    cap = limit - 1
    cut = text[:cap]
    last_ws = max(cut.rfind(" "), cut.rfind("\n"))
    if last_ws > limit * 0.6:  # only if the boundary is "close enough"
        cut = cut[:last_ws].rstrip()
    return cut.rstrip() + "…"


# ---------- public orchestrator ----------


def build_linkedin(
    master: Master,
    provider: LLMProvider,
    *,
    model: Optional[str] = None,
    timeout_s: int = DEFAULT_TIMEOUT_S,
) -> LinkedInProfile:
    """Assemble a complete LinkedIn profile from a master.

    Calls the LLM once per section that needs it (headline, about, experience,
    featured) and produces skills + education deterministically. Section-level
    failures surface as ``LinkedInBuildError``. ``CopyPasteRequired`` from the
    provider propagates so the wizard layer can render the paste UI.
    """
    headline = _build_headline(master, provider, model=model, timeout_s=timeout_s)
    about = _build_about(master, provider, model=model, timeout_s=timeout_s)
    experience = _build_experience(master, provider, model=model, timeout_s=timeout_s)
    featured = _build_featured(master, provider, model=model, timeout_s=timeout_s)
    skills, pinned = _build_skills(master)
    education = _build_education(master)
    return LinkedInProfile(
        headline=headline,
        about=about,
        experience=experience,
        skills=skills,
        pinned_skills=pinned,
        featured=featured,
        education=education,
    )


# ---------- anti-fabrication guard ----------

# Generic LinkedIn / English idiom that the proper-noun extractor surfaces
# but that don't represent fabrication. Mirrors cover_letter._PROSE_ALLOWLIST
# but tuned for LinkedIn vocabulary.
_LINKEDIN_ALLOWLIST = {
    # LinkedIn idiom
    "linkedin", "connect", "connection", "connections", "network",
    "message", "dm", "reach", "looking", "open", "interested",
    # universal English in profiles
    "team", "teams", "company", "companies", "engineer", "engineers",
    "engineering", "software", "developer", "developers", "manager",
    "managers", "founder", "founders", "co-founder", "cofounder",
    "leader", "leadership", "product", "products", "design", "designer",
    "designers", "researcher", "research", "analyst", "consultant",
    "consulting", "intern", "internship",
    # seniority levels
    "senior", "staff", "principal", "junior", "lead", "founding",
    "associate", "director", "vp", "head", "chief", "executive",
    # role descriptors
    "backend", "frontend", "fullstack", "full-stack", "infrastructure",
    "infra", "platform", "mobile", "security", "data", "ml", "ai",
    "devops", "sre", "qa",
    # locations vocab that often shows up (country names handled below)
    "remote", "hybrid", "onsite",
    # social-graph verbs
    "shipping", "shipped", "building", "built", "leading", "led",
    "growing", "scaling", "scaled", "mentoring", "mentored",
    # CTA-ish words
    "hello", "hi", "hey", "thanks", "thank",
}


@dataclass
class LinkedInWarning:
    """One fabrication / guideline issue found in a profile section."""

    section: str  # "headline" | "about" | "experience:<source_id>" | "featured:<source_id>" | "education:<source_id>" | "skills"
    reason: str
    text: str


@dataclass
class LinkedInGuardResult:
    profile: LinkedInProfile
    warnings: List[LinkedInWarning] = field(default_factory=list)


def _master_metadata_vocab(master: Master) -> set[str]:
    """Proper-noun-like tokens from master metadata (company/role/project/school).

    Same idea as cover_letter._master_metadata_vocab — the bullet-text guard
    can't see these but LinkedIn prose legitimately references them.
    """
    blob_parts: List[str] = []
    for exp in master.experience:
        blob_parts.append(exp.company)
        blob_parts.append(exp.role)
        if exp.location:
            blob_parts.append(exp.location)
    for proj in master.projects:
        blob_parts.append(proj.name)
    for edu in master.education:
        blob_parts.append(edu.school)
        if edu.location:
            blob_parts.append(edu.location)
    for grp in master.skills:
        blob_parts.append(grp.category)
        blob_parts.extend(grp.items)
    for award in master.awards:
        blob_parts.append(award.name)
    blob = " ".join(p for p in blob_parts if p)

    out: set[str] = set()
    for m in _PROPER_NOUN_RE.finditer(blob):
        token = m.group(1)
        if len(token) < 2 or token.lower() in _COMMON_STARTERS:
            continue
        out.add(token.lower())
    return out


def _all_bullet_source_vocab(master: Master) -> str:
    """Concatenated text + variants of every bullet in the master — the
    full authoritative vocabulary for headline / about (which span the
    whole career).
    """
    parts: List[str] = []
    if master.summary:
        parts.append(master.summary)
    for exp in master.experience:
        for b in exp.bullets:
            parts.extend(b.all_source_texts())
    for proj in master.projects:
        for b in proj.bullets:
            parts.extend(b.all_source_texts())
    for activity in master.extracurricular:
        for b in activity.bullets:
            parts.extend(b.all_source_texts())
    for edu in master.education:
        if edu.notes:
            parts.append(edu.notes)
        if edu.reason:
            parts.append(edu.reason)
    for award in master.awards:
        if award.criteria:
            parts.append(award.criteria)
    return "\n".join(parts)


def _container_bullet_text(master: Master, source_id: str) -> str:
    """All authoritative text for one container (Experience or Project)."""
    parts: List[str] = []
    for exp in master.experience:
        if exp.id == source_id:
            for b in exp.bullets:
                parts.extend(b.all_source_texts())
            break
    for proj in master.projects:
        if proj.id == source_id:
            for b in proj.bullets:
                parts.extend(b.all_source_texts())
            break
    return "\n".join(parts)


_FIRST_PERSON_RE = re.compile(r"\b(i|i'm|i've|i'll|i'd|my|me|mine|we|our|us)\b")


def _looks_first_person(text: str) -> bool:
    """Heuristic: at least one first-person pronoun in the first 200 chars.

    LinkedIn About sections must be written in the candidate's voice. Pure
    third-person bios read as a bot wrote them.
    """
    return bool(_FIRST_PERSON_RE.search(text[:200].lower()))


def validate_linkedin(
    master: Master,
    profile: LinkedInProfile,
    *,
    pointers: Optional[Pointers] = None,
) -> LinkedInGuardResult:
    """Validate each section against fabrication + LinkedIn-specific rules.

    Sections are not dropped on failure — warnings are surfaced so the user
    can re-prompt or accept the risk (matching the cover-letter guard's UX).
    """
    pointers = pointers or Pointers()
    pointer_vocab = _build_pointer_vocab(pointers)
    metadata_vocab = _master_metadata_vocab(master)
    base_allowed_vocab = _LINKEDIN_ALLOWLIST | metadata_vocab

    full_master_text = _all_bullet_source_vocab(master)
    warnings: List[LinkedInWarning] = []

    # -- headline + about: span the whole career --
    reason = _check_bullet(
        profile.headline, full_master_text, base_allowed_vocab, pointer_vocab,
    )
    if reason:
        warnings.append(LinkedInWarning(
            section="headline", reason=reason, text=profile.headline,
        ))
    if len(profile.headline) > LINKEDIN_HEADLINE_MAX:
        warnings.append(LinkedInWarning(
            section="headline",
            reason=f"exceeds LinkedIn's {LINKEDIN_HEADLINE_MAX}-char limit "
                   f"({len(profile.headline)} chars)",
            text=profile.headline,
        ))

    reason = _check_bullet(
        profile.about, full_master_text, base_allowed_vocab, pointer_vocab,
    )
    if reason:
        warnings.append(LinkedInWarning(
            section="about", reason=reason, text=profile.about,
        ))
    if len(profile.about) > LINKEDIN_ABOUT_MAX:
        warnings.append(LinkedInWarning(
            section="about",
            reason=f"exceeds LinkedIn's {LINKEDIN_ABOUT_MAX}-char limit "
                   f"({len(profile.about)} chars)",
            text=profile.about,
        ))
    if not _looks_first_person(profile.about):
        warnings.append(LinkedInWarning(
            section="about",
            reason="About section reads as third-person; LinkedIn rewards "
                   "first-person ('I', 'my', 'I've') in the opening.",
            text=profile.about,
        ))

    # -- experience: each role's description must trace to that role's bullets --
    valid_exp_ids = {exp.id for exp in master.experience}
    for entry in profile.experience:
        section_key = f"experience:{entry.source_id}"
        if entry.source_id not in valid_exp_ids:
            warnings.append(LinkedInWarning(
                section=section_key,
                reason=f"source_id {entry.source_id!r} not in master.experience",
                text=entry.headline,
            ))
            continue
        legal = _container_bullet_text(master, entry.source_id)
        reason = _check_bullet(
            entry.description, legal, base_allowed_vocab, pointer_vocab,
        )
        if reason:
            warnings.append(LinkedInWarning(
                section=section_key, reason=reason, text=entry.description,
            ))
        if len(entry.description) > LINKEDIN_EXPERIENCE_DESCRIPTION_MAX:
            warnings.append(LinkedInWarning(
                section=section_key,
                reason=f"description exceeds LinkedIn's "
                       f"{LINKEDIN_EXPERIENCE_DESCRIPTION_MAX}-char limit "
                       f"({len(entry.description)} chars)",
                text=entry.description,
            ))

    # -- featured: each item's description must trace to its container --
    valid_container_ids = valid_exp_ids | {p.id for p in master.projects}
    for item in profile.featured:
        section_key = f"featured:{item.source_id}"
        if item.source_id not in valid_container_ids:
            warnings.append(LinkedInWarning(
                section=section_key,
                reason=f"source_id {item.source_id!r} not in master.projects or master.experience",
                text=item.title,
            ))
            continue
        legal = _container_bullet_text(master, item.source_id)
        # Title + description are both candidate-authored prose — check both.
        combined = f"{item.title}\n{item.description}"
        reason = _check_bullet(
            combined, legal, base_allowed_vocab, pointer_vocab,
        )
        if reason:
            warnings.append(LinkedInWarning(
                section=section_key, reason=reason, text=combined,
            ))

    if profile.featured and len(profile.featured) < DEFAULT_FEATURED_MIN:
        # Only warn if the master had enough projects to fill the floor.
        if len(master.projects) >= DEFAULT_FEATURED_MIN:
            warnings.append(LinkedInWarning(
                section="featured",
                reason=f"only {len(profile.featured)} Featured items; "
                       f"LinkedIn renders best with at least {DEFAULT_FEATURED_MIN}.",
                text="",
            ))

    # -- skills: every emitted skill must appear in master.skills.items --
    master_skill_items = {
        item.strip().lower()
        for grp in master.skills
        for item in grp.items
        if item.strip()
    }
    for skill in profile.skills:
        if skill.strip().lower() not in master_skill_items:
            warnings.append(LinkedInWarning(
                section="skills",
                reason=f"skill {skill!r} not present in master.skills",
                text=skill,
            ))
    for pinned in profile.pinned_skills:
        if pinned not in profile.skills:
            warnings.append(LinkedInWarning(
                section="skills",
                reason=f"pinned skill {pinned!r} not in the full skills list",
                text=pinned,
            ))

    # -- education: source_id must reference a real master.education entry --
    valid_edu_ids = {e.id for e in master.education}
    for edu in profile.education:
        if edu.source_id not in valid_edu_ids:
            warnings.append(LinkedInWarning(
                section=f"education:{edu.source_id}",
                reason=f"source_id {edu.source_id!r} not in master.education",
                text=edu.school,
            ))

    return LinkedInGuardResult(profile=profile, warnings=warnings)


# ---------- plain-text rendering ----------


def linkedin_to_plain_text(profile: LinkedInProfile, master: Master) -> str:
    """Render the profile as the copy-paste blocks the user pastes into LinkedIn.

    Each section is preceded by a `## <SECTION>` header so the user can scan
    the file and grab whichever block they're updating. The content under
    each header is exactly what LinkedIn expects in that edit screen.
    """
    out: List[str] = []
    out.append(f"# LinkedIn profile for {master.basics.name}\n")
    out.append("Paste each block below into the matching LinkedIn edit screen. "
               "No section is shipped to LinkedIn automatically.\n")

    out.append("\n## Headline\n")
    out.append(profile.headline.strip())

    out.append("\n\n## About\n")
    out.append(profile.about.strip())

    if profile.experience:
        out.append("\n\n## Experience\n")
        for entry in profile.experience:
            out.append(f"\n### {entry.headline.strip()}\n")
            out.append(entry.description.strip())
            out.append("")

    if profile.featured:
        out.append("\n## Featured\n")
        for item in profile.featured:
            out.append(f"\n### {item.title.strip()}\n")
            if item.url:
                out.append(f"Link: {item.url}")
            if item.suggested_visual:
                out.append(f"Suggested visual: {item.suggested_visual}")
            out.append("")
            out.append(item.description.strip())
            out.append("")

    if profile.skills:
        out.append("\n## Skills\n")
        if profile.pinned_skills:
            out.append("**Pin these top 3** (LinkedIn's Top Skills):")
            for s in profile.pinned_skills:
                out.append(f"- {s}")
            out.append("")
        out.append("Full skills list (paste each one separately):")
        for s in profile.skills:
            out.append(f"- {s}")

    if profile.education:
        out.append("\n\n## Education\n")
        for edu in profile.education:
            out.append(f"\n### {edu.school}\n")
            out.append(f"{edu.degree} · {edu.year}")
            if edu.notes:
                out.append(edu.notes.strip())
            out.append("")

    return "\n".join(out).rstrip() + "\n"
