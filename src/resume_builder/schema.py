"""Pydantic schemas for master resume, tailored output, pointers, and template."""

from __future__ import annotations

from typing import List, Literal, Optional, Union

from pydantic import BaseModel, Field, field_validator


# ---------- Master resume ----------


class Link(BaseModel):
    label: str
    url: str


class Basics(BaseModel):
    name: str
    email: Optional[str] = None
    phone: Optional[str] = None
    location: Optional[str] = None
    links: List[Link] = Field(default_factory=list)


class Bullet(BaseModel):
    id: str
    text: str
    tags: List[str] = Field(default_factory=list)
    # Impact (1-5). Used by the tailor under length pressure: higher impact wins.
    # Optional; absent ≈ "unrated", treated as 3 by the tailor when comparing.
    impact_score: Optional[int] = Field(default=None, ge=1, le=5)
    # Alternate phrasings of the same accomplishment. The tailor picks whichever
    # best matches JD tone. The no-invention guard treats every variant's
    # vocabulary as legal source material (you wrote them; they're truthful).
    variants: List[str] = Field(default_factory=list)

    def all_source_texts(self) -> List[str]:
        """All text the no-invention guard treats as authoritative for this bullet."""
        return [self.text, *self.variants]


class Experience(BaseModel):
    id: str
    company: str
    role: str
    start: str
    end: str
    location: Optional[str] = None
    bullets: List[Bullet] = Field(default_factory=list)


class Project(BaseModel):
    id: str
    name: str
    url: Optional[str] = None
    bullets: List[Bullet] = Field(default_factory=list)


# Education status — covers Bock's edge cases plus normal completions.
# - ``graduated``: degree completed
# - ``in_progress``: currently enrolled
# - ``dropout``: started, did not finish (Bock: include only if relevant)
# - ``deferred_admit``: accepted but did not enroll (gap year, etc.)
# - ``rejected_admit``: applied and accepted at an elite-recognizable place;
#   the user decided not to attend (only worth listing for the brand-drop)
# - ``on_leave``: sabbatical / leave of absence mid-degree
# - ``certification_only``: not a degree — Coursera, AWS, Google Cloud, etc.
# - ``online_only``: degree completed via fully online program
EducationStatus = Literal[
    "graduated",
    "in_progress",
    "dropout",
    "deferred_admit",
    "rejected_admit",
    "on_leave",
    "certification_only",
    "online_only",
]


class Award(BaseModel):
    """An award attached to an education entry or stand-alone.

    Bock's rule: a trophy without context is noise. ``criteria`` is required
    in spirit but kept Optional to allow legacy entries to load.
    """

    name: str
    criteria: Optional[str] = None
    year: Optional[str] = None


class Education(BaseModel):
    id: str
    school: str
    degree: str
    year: str
    location: Optional[str] = None
    notes: Optional[str] = None
    status: EducationStatus = "graduated"
    # GPA as a string so "3.7 in major" or "3.8/4.0" both round-trip cleanly.
    # Bock: include only if >= 3.5 or top 10%.
    gpa: Optional[str] = None
    # Position-of-strength narrative for non-standard statuses.
    # Examples (dropout): "Left in junior year to co-found Acme — acquired 2022".
    # Examples (rejected_admit): "Declined Stanford MBA to scale a healthcare startup at INR 2 Cr ARR".
    # Free-form user voice; the renderer appends it inline.
    reason: Optional[str] = None
    awards: List[Award] = Field(default_factory=list)


class SkillGroup(BaseModel):
    category: str
    items: List[str]


CURRENT_MASTER_SCHEMA_VERSION = 1


class Master(BaseModel):
    # Bumped when the YAML shape changes incompatibly. Old files without
    # this field load as v1 (the implicit version before this was added).
    # Future migration hook: a top-level migrate_from_v1 helper in loaders.py
    # when the first breaking change ships.
    schema_version: int = CURRENT_MASTER_SCHEMA_VERSION
    basics: Basics
    summary: Optional[str] = None
    experience: List[Experience] = Field(default_factory=list)
    projects: List[Project] = Field(default_factory=list)
    education: List[Education] = Field(default_factory=list)
    skills: List[SkillGroup] = Field(default_factory=list)
    # Standalone awards (not attached to a specific education entry).
    # Added in Phase 6 — populated by the wizard's `awards` bucket.
    # Backwards-compatible: legacy master.yaml files without this field
    # load with an empty list.
    awards: List[Award] = Field(default_factory=list)
    # Clubs, volunteering, sports, community organising, etc.
    # Reuses the Project shape (name + bullets) since the structure is
    # identical — an activity has a name and a list of accomplishments.
    extracurricular: List[Project] = Field(default_factory=list)

    def all_bullet_ids(self) -> set[str]:
        ids: set[str] = set()
        for exp in self.experience:
            for b in exp.bullets:
                ids.add(b.id)
        for proj in self.projects:
            for b in proj.bullets:
                ids.add(b.id)
        for activity in self.extracurricular:
            for b in activity.bullets:
                ids.add(b.id)
        return ids

    def bullet_by_id(self, bullet_id: str) -> Optional[Bullet]:
        for exp in self.experience:
            for b in exp.bullets:
                if b.id == bullet_id:
                    return b
        for proj in self.projects:
            for b in proj.bullets:
                if b.id == bullet_id:
                    return b
        for activity in self.extracurricular:
            for b in activity.bullets:
                if b.id == bullet_id:
                    return b
        return None

    def container_by_id(self, container_id: str) -> Optional[Union[Experience, Project]]:
        for exp in self.experience:
            if exp.id == container_id:
                return exp
        for proj in self.projects:
            if proj.id == container_id:
                return proj
        for activity in self.extracurricular:
            if activity.id == container_id:
                return activity
        return None


# ---------- Tailored output ----------


class TailoredBullet(BaseModel):
    source_id: str
    rewritten_text: str


class TailoredItem(BaseModel):
    source_id: str  # references an Experience.id or Project.id
    bullets: List[TailoredBullet] = Field(default_factory=list)


class TailoredSection(BaseModel):
    name: Literal["summary", "experience", "projects", "education", "skills"]
    items: List[TailoredItem] = Field(default_factory=list)


class TailoredResume(BaseModel):
    summary: Optional[str] = None
    sections: List[TailoredSection]
    dropped_source_ids: List[str] = Field(default_factory=list)
    rationale: Optional[str] = None


# ---------- Cover letter ----------


class CoverLetterParagraph(BaseModel):
    role: Literal["intro", "expand", "why_role", "why_me", "close"]
    text: str
    # Bullet IDs from the master that ground concrete claims in this paragraph.
    # The cover-letter guard validates numbers/proper-nouns against the union of
    # these bullets' text + variants.
    source_ids: List[str] = Field(default_factory=list)


class CoverLetter(BaseModel):
    salutation: str = "Dear hiring team,"
    paragraphs: List[CoverLetterParagraph]
    closing: str = "Sincerely,"
    rationale: Optional[str] = None


# ---------- LinkedIn profile ----------

# LinkedIn's hard limits on each section's content. These are enforced by
# the builder (truncation + warning) so the user never pastes something
# LinkedIn would silently chop.
LINKEDIN_HEADLINE_MAX = 220
LINKEDIN_ABOUT_MAX = 2000
LINKEDIN_EXPERIENCE_DESCRIPTION_MAX = 2000


class LinkedInExperienceEntry(BaseModel):
    """One role on LinkedIn. ``source_id`` references a master Experience.id;
    ``description`` is the longer-form LinkedIn body (resume bullets rewritten
    with more nuance, room for context).
    """

    source_id: str
    headline: str  # one-line role headline (e.g. "Senior Backend Engineer @ Acme")
    description: str  # ≤ LINKEDIN_EXPERIENCE_DESCRIPTION_MAX chars


class LinkedInFeaturedItem(BaseModel):
    """A portfolio-worthy entry under the Featured section. ``source_id``
    references a master Project.id (or Experience.id for portfolio-worthy
    work shipped inside a job).
    """

    source_id: str
    title: str
    description: str
    url: Optional[str] = None
    # What the user should attach as the Featured tile's visual ("screenshot of
    # the dashboard", "link to the OSS repo", "demo video"). Free-form hint.
    suggested_visual: Optional[str] = None


class LinkedInEducationEntry(BaseModel):
    source_id: str
    school: str
    degree: str
    year: str
    notes: Optional[str] = None


class LinkedInProfile(BaseModel):
    headline: str  # ≤ LINKEDIN_HEADLINE_MAX chars
    about: str  # ≤ LINKEDIN_ABOUT_MAX chars, first-person narrative
    experience: List[LinkedInExperienceEntry] = Field(default_factory=list)
    # `skills` is the full ordered skills list. `pinned_skills` is the top 3
    # the user should pin (LinkedIn's "Top skills" feature). pinned ⊂ skills.
    skills: List[str] = Field(default_factory=list)
    pinned_skills: List[str] = Field(default_factory=list)
    featured: List[LinkedInFeaturedItem] = Field(default_factory=list)
    education: List[LinkedInEducationEntry] = Field(default_factory=list)
    rationale: Optional[str] = None


# ---------- Per-run pointers ----------


class Pointers(BaseModel):
    length: Optional[str] = None  # "1page" | "2page" | int word count as string
    seniority: Optional[
        Literal["ic", "senior", "staff", "manager", "founding-eng"]
    ] = None
    must_include: List[str] = Field(default_factory=list)
    context: Optional[
        Literal["startup", "faang", "consulting", "nonprofit", "research"]
    ] = None
    # Freeform style/emphasis guidance passed to the tailor + cover-letter
    # prompts (e.g. "emphasize leadership", "British English"). Style only —
    # it never extends the no-invention guard's legal vocabulary, so an
    # instruction can't smuggle a new number or tool name into the output.
    extra_instructions: Optional[str] = None

    @field_validator("must_include", mode="before")
    @classmethod
    def split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v

    @field_validator("extra_instructions", mode="before")
    @classmethod
    def clean_extra_instructions(cls, v):
        if v is None:
            return None
        v = str(v).strip()
        if not v:
            return None
        return v[:2000]  # keep prompts sane if someone pastes a novel


class TargetRole(BaseModel):
    """JD-less tailoring input — describes the role the user is targeting
    when no actual job description is on hand. Phase 7 synthesizes JDSignals
    from this so the existing tailor + ATS code consumes it unchanged.
    """

    role: str  # e.g. "Staff Backend Engineer"
    seniority: Optional[
        Literal["ic", "senior", "staff", "manager", "founding-eng"]
    ] = None
    industry: Optional[str] = None  # "fintech", "healthtech", "consumer", etc.
    must_include: List[str] = Field(default_factory=list)
    company_size: Optional[
        Literal["startup", "scaleup", "enterprise", "faang"]
    ] = None

    @field_validator("must_include", mode="before")
    @classmethod
    def split_csv(cls, v):
        if isinstance(v, str):
            return [s.strip() for s in v.split(",") if s.strip()]
        return v


# ---------- Formatting template ----------


class FontSpec(BaseModel):
    name: str = "Calibri"
    size: float = 11.0
    bold: bool = False
    italic: bool = False


class PageSpec(BaseModel):
    size: Literal["letter", "a4"] = "letter"
    margin_top: str = "0.5in"
    margin_bottom: str = "0.5in"
    margin_left: str = "0.7in"
    margin_right: str = "0.7in"


class FontsSpec(BaseModel):
    body: FontSpec = Field(default_factory=lambda: FontSpec(size=10.5))
    heading: FontSpec = Field(default_factory=lambda: FontSpec(size=12, bold=True))
    name: FontSpec = Field(default_factory=lambda: FontSpec(size=18, bold=True))
    role: FontSpec = Field(default_factory=lambda: FontSpec(size=11, bold=True))


class SpacingSpec(BaseModel):
    line: float = 1.15
    paragraph_after: float = 4.0


class ColorsSpec(BaseModel):
    heading: str = "#000000"
    accent: str = "#2E5C8A"
    body: str = "#000000"


class Template(BaseModel):
    page: PageSpec = Field(default_factory=PageSpec)
    fonts: FontsSpec = Field(default_factory=FontsSpec)
    spacing: SpacingSpec = Field(default_factory=SpacingSpec)
    colors: ColorsSpec = Field(default_factory=ColorsSpec)
    section_order: List[str] = Field(
        default_factory=lambda: ["summary", "experience", "projects", "education", "skills"]
    )
